from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from app.models.db import Finding

MAX_LINK_CANDIDATES = 20
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
LINK_KEY_HINTS = ("url", "link", "website", "homepage", "bio")

PLATFORM_HOSTS: tuple[tuple[str, str], ...] = (
    ("instagram.com", "Instagram"),
    ("tiktok.com", "TikTok"),
    ("twitter.com", "X"),
    ("x.com", "X"),
    ("facebook.com", "Facebook"),
    ("youtube.com", "YouTube"),
    ("youtu.be", "YouTube"),
    ("twitch.tv", "Twitch"),
    ("reddit.com", "Reddit"),
    ("github.com", "GitHub"),
    ("linkedin.com", "LinkedIn"),
    ("snapchat.com", "Snapchat"),
    ("t.me", "Telegram"),
    ("telegram.me", "Telegram"),
    ("linktr.ee", "Linktree"),
    ("beacons.ai", "Beacons"),
    ("carrd.co", "Carrd"),
)


@dataclass(frozen=True, slots=True)
class ProfileLinkCandidate:
    url: str
    host: str
    platform: str | None
    account_identifier: str | None
    provenance: str


def _safe_public_url(value: str) -> str | None:
    candidate = value.strip().rstrip(".,;:!?)]}")
    if len(candidate) > 2048:
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    host = parsed.hostname.casefold().rstrip(".")
    if host == "localhost" or "." not in host or host.endswith((".local", ".internal", ".lan")):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            return None
    return candidate


def _platform_for_host(host: str) -> str | None:
    normalised = host.removeprefix("www.")
    for suffix, platform in PLATFORM_HOSTS:
        if normalised == suffix or normalised.endswith(f".{suffix}"):
            return platform
    return None


def _account_identifier(url: str, platform: str | None) -> str | None:
    parsed = urlsplit(url)
    parts = [unquote(part).strip() for part in parsed.path.split("/") if part.strip()]
    if not parts:
        return None
    if platform == "Instagram":
        return parts[0].removeprefix("@").casefold() or None
    if platform == "TikTok":
        return parts[0].removeprefix("@").casefold() or None
    if platform == "X":
        return parts[0].removeprefix("@").casefold() or None
    if platform == "Reddit" and len(parts) >= 2 and parts[0].casefold() in {"u", "user"}:
        return parts[1].casefold() or None
    if platform == "GitHub":
        return parts[0].casefold() or None
    if platform == "Telegram":
        if parts[0].startswith("+") or parts[0].casefold() in {"joinchat", "share"}:
            return None
        return parts[0].removeprefix("@").casefold() or None
    if platform in {"Linktree", "Beacons", "Carrd"}:
        return parts[0].casefold() or None
    return None


_HANDLE_RE = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_.\-]{1,62}[A-Za-z0-9_])?")
# First path segments that are site sections rather than accounts.
_RESERVED_SEGMENTS = frozenset(
    {
        "about", "accounts", "ads", "business", "collections", "developer", "developers",
        "direct", "directory", "discover", "downloads", "events", "explore", "features",
        "feed", "groups", "hashtag", "help", "home", "i", "intent", "jobs", "join", "legal",
        "login", "marketplace", "messages", "notifications", "orgs", "p", "pages", "people",
        "photo", "photos", "policies", "pricing", "privacy", "public", "reel", "reels",
        "search", "settings", "share", "sharer", "signup", "sponsors", "stories", "story",
        "tag", "tags", "topics", "tos", "trending", "tv", "videos", "watch", "web",
    }
)
_PROFILE_PLATFORMS: tuple[tuple[str, str], ...] = (
    *PLATFORM_HOSTS,
    ("threads.net", "Threads"),
    ("threads.com", "Threads"),
    ("pinterest.com", "Pinterest"),
)
# Sites where a page mentioning a word says nothing about who holds that word as a handle.
_SOCIAL_HOSTS: tuple[tuple[str, str], ...] = (
    *_PROFILE_PLATFORMS,
    ("pinterest.com", "Pinterest"),
    ("tumblr.com", "Tumblr"),
    ("vk.com", "VK"),
    ("medium.com", "Medium"),
    ("soundcloud.com", "SoundCloud"),
)


def social_platform_for_url(url: str) -> str | None:
    """Name the social platform a URL belongs to, or None for any other site."""
    host = (urlsplit(url).hostname or "").casefold().removeprefix("www.").removeprefix("m.")
    return next(
        (name for suffix, name in _SOCIAL_HOSTS if host == suffix or host.endswith(f".{suffix}")),
        None,
    )


def profile_account_from_url(url: str) -> tuple[str, str] | None:
    """Return (platform, handle) only when the URL is shaped like an account's own page.

    Post, search, group and other section URLs return None: a handle taken from them would
    be a guess, and these handles are offered as follow-up searches.
    """
    safe_url = _safe_public_url(url)
    if not safe_url:
        return None
    parsed = urlsplit(safe_url)
    host = (parsed.hostname or "").casefold().removeprefix("www.").removeprefix("m.")
    platform = next(
        (name for suffix, name in _PROFILE_PLATFORMS if host == suffix or host.endswith(f".{suffix}")),
        None,
    )
    parts = [unquote(part).strip() for part in parsed.path.split("/") if part.strip()]
    if platform is None or not parts:
        return None
    first = parts[0]
    handle: str | None = None
    if platform in {"TikTok", "YouTube", "Threads"}:
        handle = first[1:] if first.startswith("@") and len(parts) == 1 else None
    elif platform == "Reddit":
        handle = parts[1] if len(parts) == 2 and first.casefold() in {"u", "user"} else None
    elif platform == "LinkedIn":
        handle = parts[1] if len(parts) == 2 and first.casefold() == "in" else None
    elif platform == "Snapchat":
        if len(parts) == 2 and first.casefold() == "add":
            handle = parts[1]
        elif len(parts) == 1 and first.startswith("@"):
            handle = first[1:]
    elif platform in {"Instagram", "Pinterest", "X", "GitHub", "Twitch", "Telegram", "Facebook", "Linktree"}:
        if len(parts) == 1 and not first.startswith("+") and "." + first.rsplit(".", 1)[-1] != ".php":
            handle = first.removeprefix("@")
    if not handle or handle.casefold() in _RESERVED_SEGMENTS or not _HANDLE_RE.fullmatch(handle):
        return None
    if platform == "Telegram" and handle.casefold() in {"s", "joinchat", "addstickers", "proxy"}:
        return None
    return platform, handle


def _candidate_strings(finding: Finding) -> list[tuple[str, str]]:
    profile = finding.extra.get("profile", {})
    profile = profile if isinstance(profile, dict) else {}
    values: list[tuple[str, str]] = []
    direct = (
        (profile.get("biography"), "profile.biography"),
        (finding.extra.get("external_url"), "finding.external_url"),
        (finding.extra.get("website"), "finding.website"),
        (finding.description, "finding.description"),
    )
    values.extend((value, source) for value, source in direct if isinstance(value, str))

    platform_details = finding.extra.get("platform_details", [])
    if isinstance(platform_details, list):
        for index, detail in enumerate(platform_details[:50]):
            if not isinstance(detail, dict):
                continue
            label = str(detail.get("label") or "").casefold()
            value = detail.get("value")
            if isinstance(value, str) and any(hint in label for hint in LINK_KEY_HINTS):
                values.append((value, f"platform_details[{index}].{label or 'value'}"))

    # Some native collectors retain bounded structured metadata. Walk only key/value pairs
    # whose key advertises a link-like field; never crawl arbitrary raw output recursively.
    for key, value in list(finding.extra.items())[:100]:
        if not isinstance(key, str) or not any(hint in key.casefold() for hint in LINK_KEY_HINTS):
            continue
        if isinstance(value, str):
            values.append((value, f"finding.{key}"))
        elif isinstance(value, list):
            for index, item in enumerate(value[:20]):
                if isinstance(item, str):
                    values.append((item, f"finding.{key}[{index}]"))
                elif isinstance(item, dict):
                    for nested_key, nested_value in list(item.items())[:20]:
                        if isinstance(nested_value, str) and any(
                            hint in str(nested_key).casefold() for hint in LINK_KEY_HINTS
                        ):
                            values.append(
                                (nested_value, f"finding.{key}[{index}].{nested_key}")
                            )
    return values


def extract_profile_link_candidates(
    finding: Finding,
    *,
    limit: int = MAX_LINK_CANDIDATES,
) -> list[ProfileLinkCandidate]:
    if not 1 <= limit <= MAX_LINK_CANDIDATES:
        raise ValueError(f"profile-link limit must be 1..{MAX_LINK_CANDIDATES}")
    results: list[ProfileLinkCandidate] = []
    seen: set[str] = set()
    for text, provenance in _candidate_strings(finding):
        candidates = [text] if text.strip().lower().startswith(("http://", "https://")) else []
        candidates.extend(URL_RE.findall(text))
        for raw_url in candidates:
            safe_url = _safe_public_url(raw_url)
            if not safe_url:
                continue
            key = safe_url.casefold()
            if key in seen:
                continue
            seen.add(key)
            host = (urlsplit(safe_url).hostname or "").casefold().removeprefix("www.")
            platform = _platform_for_host(host)
            results.append(
                ProfileLinkCandidate(
                    url=safe_url,
                    host=host,
                    platform=platform,
                    account_identifier=_account_identifier(safe_url, platform),
                    provenance=provenance,
                )
            )
            if len(results) >= limit:
                return results
    return results
