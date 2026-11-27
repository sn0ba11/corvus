#!/usr/bin/env python3
"""Bounded Instaloader profile probe for OSINT Workbench.

The wrapper retrieves one public profile and one profile picture only. It never
iterates posts, stories, followers, followees, highlights, tagged media or reels.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlsplit

import instaloader
from instaloader.exceptions import (
    BadResponseException,
    ConnectionException,
    LoginRequiredException,
    ProfileNotExistsException,
    QueryReturnedBadRequestException,
    TooManyRequestsException,
)
from instaloader.instaloadercontext import RateController

USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
OUTPUT_PATH = Path("profile.json")
SESSION_DIR = Path("/sessions")
BROWSER_SESSION_ENV = "OSINT_SESSION_COOKIE_FILE"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_USER_AGENT_LENGTH = 512
MAX_ACCEPT_LANGUAGE_LENGTH = 256
PROXY_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}

# Instagram's unauthenticated account-recovery lookup endpoint. Unlike the profile fetch
# above, this call never carries a session cookie, so there is no browser identity to stay
# consistent with; it uses one fixed, internally-consistent app identity (the paired
# User-Agent and X-IG-App-ID below), the same approach documented by megadose/toutatis
# (GPLv3) — see THIRD_PARTY_NOTICES.md. The implementation here is independent and routed
# through the already-proxied, already-`trust_env=False` Instaloader session rather than a
# bare `requests.post`, so it inherits the same network controls as the rest of this probe.
RECOVERY_LOOKUP_URL = "https://i.instagram.com/api/v1/users/lookup/"
RECOVERY_LOOKUP_APP_ID = "124024574287414"
RECOVERY_LOOKUP_USER_AGENT = "Instagram 101.0.0.15.120"


# Observed 2026-11-13: Instagram answers the profile API with HTTP 429 on the very first
# logged-out request, so without a session this is a refusal, not a quota that recovers.
LOGGED_OUT_REFUSAL = (
    "Instagram refused the logged-out profile lookup (HTTP 429). It does this on the first "
    "request without a session, so waiting will not help: sign in to Instagram in the sock "
    "browser and keep Share authenticated browser sessions on, then retry Instaloader"
)


class FastFailRateController(RateController):
    """Return a structured rate-limit result instead of sleeping for minutes."""

    def handle_429(self, query_type: str) -> None:
        raise TooManyRequestsException(
            f"Instagram rate limited query type {query_type}; automatic retry disabled"
        )


def write_result(payload: dict[str, Any]) -> None:
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"status": payload.get("status"), "site": "Instagram"}
    if isinstance(payload.get("error"), str):
        summary["error"] = payload["error"]
    print(json.dumps(summary))


def _cookie_priority(item: dict[str, Any]) -> tuple[int, int, int, float]:
    raw_domain = str(item.get("domain") or "").lower().strip()
    domain = raw_domain.lstrip(".").rstrip(".")
    path = str(item.get("path") or "/")
    try:
        expires = float(item.get("expires", -1))
    except (TypeError, ValueError):
        expires = -1
    return (
        2 if domain == "instagram.com" else 1,
        1 if path == "/" else 0,
        1 if raw_domain.startswith(".") else 0,
        expires,
    )


def _select_browser_cookies(payload: object) -> dict[str, str]:
    if not isinstance(payload, list):
        raise ValueError("invalid cookie bundle")
    candidates: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("invalid cookie")
        domain = str(item.get("domain") or "").lower().strip().lstrip(".").rstrip(".")
        if domain != "instagram.com" and not domain.endswith(".instagram.com"):
            raise ValueError("cookie outside Instagram scope")
        if str(item.get("name") or "") and str(item.get("value") or ""):
            candidates.append(item)

    grouped: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for item in candidates:
        key = (
            str(item.get("domain") or "").lower().strip().lstrip(".").rstrip("."),
            str(item.get("path") or "/"),
        )
        name = str(item["name"])
        existing = grouped.setdefault(key, {}).get(name)
        if existing is None or _cookie_priority(item) > _cookie_priority(existing):
            grouped[key][name] = item

    required = {"sessionid", "csrftoken"}
    matching_groups = [items for items in grouped.values() if required.issubset(items)]
    session: dict[str, str] = {}
    if matching_groups:
        selected_group = max(
            matching_groups,
            key=lambda items: max(_cookie_priority(item) for item in items.values()),
        )
        session.update({name: str(item["value"]) for name, item in selected_group.items()})

    for item in sorted(candidates, key=_cookie_priority, reverse=True):
        session.setdefault(str(item["name"]), str(item["value"]))
    return session


def validated_proxy(raw_proxy: str | None) -> str | None:
    raw_candidate = str(raw_proxy or "")
    if any(char in raw_candidate for char in "\r\n\x00"):
        raise ValueError("invalid proxy URL")
    candidate = raw_candidate.strip()
    if not candidate:
        return None
    if len(candidate) > 500:
        raise ValueError("invalid proxy URL")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme.casefold() not in PROXY_SCHEMES
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("proxy must be an HTTP(S) or SOCKS proxy URL")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("invalid proxy port") from exc
    return candidate


def _validated_header(value: object, *, maximum: int, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid {field}")
    candidate = " ".join(value.split())
    if not candidate or len(candidate) > maximum or any(char in candidate for char in "\r\n\x00"):
        raise ValueError(f"invalid {field}")
    return candidate


def read_browser_session_context() -> tuple[dict[str, Any] | None, str | None]:
    cookie_path = os.environ.get(BROWSER_SESSION_ENV, "").strip()
    if not cookie_path:
        return None, None
    try:
        payload = json.loads(Path(cookie_path).read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return None, "managed browser session is missing its originating browser identity"
        if not isinstance(payload, dict):
            raise ValueError("invalid browser session bundle")
        cookies = payload.get("cookies")
        user_agent = _validated_header(
            payload.get("user_agent"), maximum=MAX_USER_AGENT_LENGTH, field="browser user agent"
        )
        accept_language = (
            _validated_header(
                payload.get("accept_language"),
                maximum=MAX_ACCEPT_LANGUAGE_LENGTH,
                field="browser language",
            )
            or "en-US,en;q=0.9"
        )
        if not user_agent:
            return None, "managed browser session is missing its originating browser identity"
        return {
            "cookies": cookies,
            "user_agent": user_agent,
            "accept_language": accept_language,
        }, None
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        return None, f"managed browser session could not be read: {type(exc).__name__}"


def configure_session(
    loader: instaloader.Instaloader,
    *,
    proxy: str | None,
    accept_language: str | None,
) -> None:
    session = loader.context._session
    session.trust_env = False
    if accept_language:
        session.headers["Accept-Language"] = accept_language
    session.proxies.clear()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})


def _reset_anonymous_session(
    loader: instaloader.Instaloader,
    *,
    proxy: str | None,
    accept_language: str | None,
) -> None:
    loader.context._session = loader.context.get_anonymous_session()
    loader.context.username = None
    configure_session(loader, proxy=proxy, accept_language=accept_language)


def load_browser_session(
    loader: instaloader.Instaloader,
    browser_context: dict[str, Any] | None,
    *,
    proxy: str | None,
) -> tuple[bool, str | None]:
    if browser_context is None:
        return False, None
    accept_language = str(browser_context.get("accept_language") or "") or None
    try:
        session = _select_browser_cookies(browser_context.get("cookies"))
        if not {"sessionid", "csrftoken"}.issubset(session):
            return False, "managed browser is not logged in to Instagram"
        loader.load_session("managed-browser", session)
        configure_session(loader, proxy=proxy, accept_language=accept_language)
        # Instaloader.test_login() queries Instagram's legacy GraphQL hash
        # d6f4427fbe92d846298cf93df0b937d3, which frequently answers
        # feedback_required even for a valid signed-in browser. The browser
        # agent already scoped the cookies; the test adds no useful evidence.
        # Do not call it. The subsequent profile lookup establishes what, if
        # anything, the Instagram API will allow this collector to retrieve.
        # A transferred cookie is not independently verified authentication.
    except (
        BadResponseException,
        ConnectionException,
        QueryReturnedBadRequestException,
        TooManyRequestsException,
        LoginRequiredException,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        _reset_anonymous_session(loader, proxy=proxy, accept_language=accept_language)
        if is_rate_limit_error(exc):
            return False, (
                "Instagram challenged Instaloader's login verification (feedback_required or "
                "rate limit). The managed browser may still be logged in; API access was "
                "not verified. Corvus will not silently retry as logged out."
            )
        return False, f"managed browser session could not be loaded: {type(exc).__name__}"
    return True, "Managed browser cookies loaded; Instagram API session not independently verified"


def load_optional_session(
    loader: instaloader.Instaloader,
    browser_context: dict[str, Any] | None,
    *,
    proxy: str | None,
    browser_warning: str | None = None,
) -> tuple[bool, str | None]:
    browser_accept_language = (
        str(browser_context.get("accept_language") or "") or None
        if browser_context is not None
        else None
    )
    browser_loaded, load_warning = load_browser_session(loader, browser_context, proxy=proxy)
    browser_warning = load_warning or browser_warning
    if browser_loaded:
        return True, load_warning

    session_username = os.environ.get("INSTALOADER_SESSION_USERNAME", "").strip()
    if not session_username:
        configure_session(loader, proxy=proxy, accept_language=browser_accept_language)
        return False, browser_warning
    session_file = SESSION_DIR / f"session-{session_username}"
    if not session_file.is_file():
        configure_session(loader, proxy=proxy, accept_language=browser_accept_language)
        return False, browser_warning or "configured session file is unavailable"
    try:
        loader.load_session_from_file(session_username, filename=str(session_file))
        configure_session(loader, proxy=proxy, accept_language=browser_accept_language)
    except (ConnectionException, OSError, ValueError) as exc:
        return False, browser_warning or f"session could not be loaded: {type(exc).__name__}"
    return True, browser_warning


def configure_instagram_api_identity(
    loader: instaloader.Instaloader, browser_user_agent: str | None
) -> None:
    """Keep Instaloader's authenticated API request aligned with the browser session."""
    if not browser_user_agent:
        return
    iphone_headers = getattr(loader.context, "iphone_headers", None)
    if isinstance(iphone_headers, dict):
        iphone_headers["User-Agent"] = browser_user_agent


def authenticated_profile_data(
    profile: instaloader.Profile, session_loaded: bool
) -> tuple[dict[str, Any], str | None]:
    if not session_loaded:
        return {}, None
    try:
        payload = profile._iphone_struct
    except (
        BadResponseException,
        ConnectionException,
        LoginRequiredException,
        QueryReturnedBadRequestException,
        TooManyRequestsException,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        return {}, f"authenticated Instagram profile enrichment failed: {type(exc).__name__}"
    if not isinstance(payload, dict):
        return {}, "authenticated Instagram profile enrichment returned invalid data"
    return payload, None


def select_avatar(
    profile: instaloader.Profile | dict[str, Any], authenticated_data: dict[str, Any]
) -> tuple[str | None, str, int | None, int | None]:
    # When a public Profile._node is supplied, never evaluate Profile.profile_pic_url:
    # that property invokes i.instagram.com and a fallback GraphQL query on missing
    # data, both of which can throw 400 feedback_required for ordinary profiles.
    node = profile if isinstance(profile, dict) else None
    hd_info = authenticated_data.get("hd_profile_pic_url_info") or (
        node.get("hd_profile_pic_url_info") if node is not None else None
    )
    if isinstance(hd_info, dict):
        hd_url = str(hd_info.get("url") or "").strip()
        if hd_url:
            width = hd_info.get("width")
            height = hd_info.get("height")
            return (
                hd_url,
                "hd",
                int(width) if isinstance(width, int | float) else None,
                int(height) if isinstance(height, int | float) else None,
            )
    if node is not None:
        value = node.get("profile_pic_url_hd") or node.get("profile_pic_url")
        return safe_http_url(value), "standard", None, None
    try:
        return safe_http_url(profile.profile_pic_url), "standard", None, None
    except Exception:  # noqa: BLE001 - property may trigger blocked GraphQL/API calls
        return None, "unavailable", None, None


def safe_http_url(value: object) -> str | None:
    candidate = str(value or "").strip()
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return candidate


def is_rate_limit_error(exc: BaseException) -> bool:
    detail = " ".join(str(exc).split()).casefold()
    return any(marker in detail for marker in (
        "429", "too many requests", "rate limit", "feedback_required",
        "please wait a few minutes",
    ))


def optional_profile_count(profile: instaloader.Profile, field: str) -> int | None:
    """Instagram may omit counts or refuse their follow-up GraphQL request.

    Never turn a partially available profile into a failed collector run.
    """
    try:
        raw = getattr(profile, field)
    except (
        BadResponseException,
        ConnectionException,
        LoginRequiredException,
        QueryReturnedBadRequestException,
        TooManyRequestsException,
        KeyError,
        TypeError,
        ValueError,
    ):
        return None
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        return None


def optional_node_count(node: dict[str, Any], edge: str, direct: str) -> int | None:
    """Use numeric values already present in the public page; never fetch more."""
    nested = node.get(edge)
    raw = nested.get("count") if isinstance(nested, dict) else nested
    if raw is None:
        raw = node.get(direct)
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        count = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return count if count >= 0 else None


def public_phone_number(authenticated_data: dict[str, Any]) -> str | None:
    number = str(authenticated_data.get("public_phone_number") or "").strip()
    country_code = str(authenticated_data.get("public_phone_country_code") or "").strip()
    if not number:
        return None
    if country_code and not number.startswith("+"):
        return f"+{country_code}{number}"
    return number


def account_recovery_hint(
    loader: instaloader.Instaloader, username: str
) -> tuple[dict[str, Any], str | None]:
    """Query Instagram's unauthenticated recovery-lookup endpoint for masked contact hints.

    This can return an obfuscated email/phone (e.g. "+00 0xx xxx xx 00") even when the
    profile sets no public contact method — a narrower but still useful lead. Failures here
    are non-fatal: the primary profile result stands on its own either way.
    """
    body = "signed_body=SIGNATURE." + quote_plus(
        json.dumps({"q": username, "skip_recovery": "1"}, separators=(",", ":"))
    )
    headers = {
        "Accept-Language": "en-US",
        "User-Agent": RECOVERY_LOOKUP_USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-IG-App-ID": RECOVERY_LOOKUP_APP_ID,
        "Host": "i.instagram.com",
    }
    try:
        response = loader.context._session.post(
            RECOVERY_LOOKUP_URL, headers=headers, data=body, timeout=20.0
        )
    except Exception as exc:  # noqa: BLE001 - network/session failures must stay non-fatal
        return {}, f"account recovery hint request failed: {type(exc).__name__}"
    if response.status_code == 429:
        return {}, "account recovery hint lookup was rate limited"
    try:
        payload = response.json()
    except ValueError:
        return {}, "account recovery hint lookup returned an unreadable response"
    if not isinstance(payload, dict):
        return {}, None
    hint: dict[str, Any] = {}
    email = str(payload.get("obfuscated_email") or "").strip()
    phone = str(payload.get("obfuscated_phone_number") or "").strip()
    if email:
        hint["obfuscated_email"] = email
    if phone:
        hint["obfuscated_phone_number"] = phone
    return hint, None


def download_avatar(loader: instaloader.Instaloader, url: str) -> tuple[str | None, str | None]:
    try:
        response = loader.context.get_raw(url)
        content = response.content
        if not content or len(content) > MAX_IMAGE_BYTES:
            return None, "profile picture exceeded the bounded size"
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        suffix = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }.get(content_type, ".img")
        target = Path(f"profile_pic{suffix}")
        target.write_bytes(content)
        return target.name, None
    except (ConnectionException, OSError) as exc:
        return None, f"profile picture retrieval failed: {type(exc).__name__}"


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--username", required=True)
    parser.add_argument("--proxy")
    args = parser.parse_args()
    username = args.username.strip()
    if not USERNAME_RE.fullmatch(username):
        write_result({"site": "Instagram", "status": "error", "error": "invalid username"})
        return 2
    try:
        proxy = validated_proxy(args.proxy)
    except ValueError as exc:
        write_result({"site": "Instagram", "status": "error", "error": str(exc)})
        return 2
    browser_context, browser_warning = read_browser_session_context()
    browser_user_agent = (
        str(browser_context.get("user_agent")) if browser_context is not None else None
    )

    loader = instaloader.Instaloader(
        user_agent=browser_user_agent,
        sleep=False,
        max_connection_attempts=1,
        request_timeout=20.0,
        rate_controller=FastFailRateController,
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )
    configure_instagram_api_identity(loader, browser_user_agent)
    session_loaded, session_warning = load_optional_session(
        loader,
        browser_context,
        proxy=proxy,
        browser_warning=browser_warning,
    )
    if browser_context is not None and not session_loaded:
        # A signed-in browser is not proof that Instagram accepted the API session.
        # If verification failed, don't make anonymous requests that can generate
        # confusing errors or be mistaken for a negative account lookup.
        challenged = bool(session_warning and (
            "challenged" in session_warning.casefold()
            or "feedback_required" in session_warning.casefold()
        ))
        write_result({
            "site": "Instagram",
            "status": "rate_limited" if challenged else "unknown",
            "username": username,
            "error": session_warning or "Instagram browser session could not be verified",
            "session_loaded": False,
            "session_warning": session_warning,
        })
        return 0
    try:
        profile = instaloader.Profile.from_username(loader.context, username)
        # Profile.from_username() has already parsed the public HTML snapshot.
        # Properties such as profile_pic_url, biography and business_category_name
        # may lazily query GraphQL or i.instagram.com when a field is absent.
        # Extract only what this actual page supplied, without a second API roundtrip.
        node = profile._node
        if not isinstance(node, dict):
            raise ValueError("Instagram profile page returned invalid metadata")
        profile_username = str(node.get("username") or username).strip()
        if profile_username.casefold() != username.casefold():
            raise ValueError("Instagram profile page did not match the requested username")
        account_id = node.get("id") or node.get("pk")
        avatar_url, avatar_quality, avatar_width, avatar_height = select_avatar(node, {})
        avatar_file, avatar_error = (
            download_avatar(loader, avatar_url) if avatar_url else (None, "no public avatar URL")
        )
        write_result(
            {
                "site": "Instagram",
                "status": "found",
                "url": f"https://www.instagram.com/{profile_username}/",
                "username": profile_username,
                "account_id": str(account_id) if account_id is not None else None,
                "display_name": node.get("full_name") or None,
                "biography": node.get("biography") or None,
                "avatar_url": avatar_url,
                "avatar_file": avatar_file,
                "avatar_quality": avatar_quality,
                "avatar_width": avatar_width,
                "avatar_height": avatar_height,
                "verified": node.get("is_verified"),
                "followers": optional_node_count(node, "edge_followed_by", "follower_count"),
                "following": optional_node_count(node, "edge_follow", "following_count"),
                "is_private": node.get("is_private"),
                "media_count": optional_node_count(
                    node, "edge_owner_to_timeline_media", "all_media_count"
                ),
                "external_url": safe_http_url(node.get("external_url")),
                "is_business": node.get("is_business_account", node.get("is_business")),
                "business_category_name": node.get("business_category_name")
                or node.get("category"),
                "public_email": node.get("public_email") or None,
                "public_phone_number": public_phone_number(node),
                "is_whatsapp_linked": node.get("is_whatsapp_linked"),
                "is_memorialized": node.get("is_memorialized"),
                "is_new_to_instagram": node.get("is_new_to_instagram"),
                "session_loaded": session_loaded,
                "session_warning": session_warning,
                "authenticated_profile_warning": (
                    "Enrichment limited to fields embedded in the public profile page"
                ),
                "avatar_error": avatar_error,
            }
        )
        return 0
    except ProfileNotExistsException as exc:
        detail = str(exc).casefold()
        access_denied = any(
            marker in detail for marker in ("403", "401", "forbidden", "unauthorized")
        )
        authenticated_absence = session_loaded and not access_denied
        write_result(
            {
                "site": "Instagram",
                "status": "not_found" if authenticated_absence else "unknown",
                "url": f"https://www.instagram.com/{username}/",
                "username": username,
                "error": (
                    "Authenticated Instagram lookup reported no profile"
                    if authenticated_absence
                    else "Anonymous Instagram lookup was inconclusive"
                ),
                "session_loaded": session_loaded,
                "session_warning": session_warning,
            }
        )
        return 0
    except TooManyRequestsException:
        write_result(
            {
                "site": "Instagram",
                "status": "rate_limited",
                "username": username,
                "error": (
                    "Instagram rate limited the profile lookup"
                    if session_loaded
                    else LOGGED_OUT_REFUSAL
                ),
                "session_loaded": session_loaded,
                "session_warning": session_warning,
            }
        )
        return 0
    except LoginRequiredException:
        write_result(
            {
                "site": "Instagram",
                "status": "unknown",
                "username": username,
                "error": "Instagram requires a valid session for this profile",
                "session_loaded": session_loaded,
                "session_warning": session_warning,
            }
        )
        return 0
    except (BadResponseException, QueryReturnedBadRequestException, ConnectionException) as exc:
        if is_rate_limit_error(exc):
            write_result(
                {
                    "site": "Instagram",
                    "status": "rate_limited",
                    "username": username,
                    "error": (
                        "Instagram rate limited the profile lookup; automatic retry was disabled "
                        "so the remaining collectors could continue"
                        if session_loaded
                        else LOGGED_OUT_REFUSAL
                    ),
                    "session_loaded": session_loaded,
                    "session_warning": session_warning,
                }
            )
            return 0
        detail = " ".join(str(exc).split())[:300]
        message = f"Instagram lookup failed: {type(exc).__name__}"
        if detail:
            message = f"{message}: {detail}"
        write_result(
            {
                "site": "Instagram",
                "status": "error",
                "username": username,
                "error": message,
                "session_loaded": session_loaded,
                "session_warning": session_warning,
            }
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
