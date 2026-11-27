from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.bing_name import name_result_is_relevant
from app.core.security import canonicalise_url
from app.models.db import Finding, Investigation, LocationObservation, ToolRun
from app.services.browser_agent import (
    BrowserAgentError,
    BrowserCaptureResult,
    resolve_evidence_path,
)
from app.services.case_identifiers import case_seeds
from app.services.location_collection import LocationCollectionError, parse_location_page
from app.services.location_intelligence import validate_source_url
from app.services.search_imports import (
    MAX_IMPORT_RESULTS,
    SearchResultItem,
    import_search_result_items,
    unwrap_search_url,
)
from app.services.search_relevance import handle_appears_in as _handle_appears_in
from app.services.search_relevance import username_result_is_relevant


@dataclass(frozen=True, slots=True)
class ManagedBrowserPolicy:
    collector_id: str
    platform: str
    kind: str
    path_markers: tuple[str, ...]


POLICIES: tuple[tuple[str, ManagedBrowserPolicy], ...] = (
    ("snapchat.com", ManagedBrowserPolicy("browser_snapchat", "Snapchat", "social_profile", ())),
    ("t.me", ManagedBrowserPolicy("browser_telegram", "Telegram", "social_profile", ())),
    ("discord.com", ManagedBrowserPolicy("browser_discord", "Discord", "social_profile", ())),
    ("web.whatsapp.com", ManagedBrowserPolicy("browser_whatsapp", "WhatsApp", "social_profile", ())),
    ("instagram.com", ManagedBrowserPolicy("browser_instagram", "Instagram", "social_profile", ())),
    ("facebook.com", ManagedBrowserPolicy("browser_facebook", "Facebook", "social_profile", ())),
    ("tiktok.com", ManagedBrowserPolicy("browser_tiktok", "TikTok", "social_profile", ())),
    ("pinterest.com", ManagedBrowserPolicy("browser_pinterest", "Pinterest", "social_profile", ())),
    ("threads.com", ManagedBrowserPolicy("browser_threads", "Threads", "social_profile", ())),
    ("threads.net", ManagedBrowserPolicy("browser_threads", "Threads", "social_profile", ())),
    (
        "google.com",
        ManagedBrowserPolicy(
            "browser_google_maps", "Google Maps", "location_history", ("/maps/", "/local/")
        ),
    ),
    (
        "google.ie",
        ManagedBrowserPolicy(
            "browser_google_maps", "Google Maps", "location_history", ("/maps/", "/local/")
        ),
    ),
    (
        "tripadvisor.com",
        ManagedBrowserPolicy(
            "browser_tripadvisor",
            "Tripadvisor",
            "location_history",
            ("/Profile/", "/members/", "/ShowUserReviews"),
        ),
    ),
    (
        "tripadvisor.ie",
        ManagedBrowserPolicy(
            "browser_tripadvisor",
            "Tripadvisor",
            "location_history",
            ("/Profile/", "/members/", "/ShowUserReviews"),
        ),
    ),
    (
        "tripadvisor.co.uk",
        ManagedBrowserPolicy(
            "browser_tripadvisor",
            "Tripadvisor",
            "location_history",
            ("/Profile/", "/members/", "/ShowUserReviews"),
        ),
    ),
    (
        "strava.com",
        ManagedBrowserPolicy(
            "browser_strava", "Strava", "location_history", ("/athletes/", "/activities/")
        ),
    ),
    (
        "alltrails.com",
        ManagedBrowserPolicy(
            "browser_alltrails", "AllTrails", "location_history", ("/members/", "/trail/")
        ),
    ),
    (
        "komoot.com",
        ManagedBrowserPolicy("browser_komoot", "Komoot", "location_history", ("/user/", "/tour/")),
    ),
    (
        "connect.garmin.com",
        ManagedBrowserPolicy(
            "browser_garmin",
            "Garmin Connect",
            "location_history",
            ("/modern/profile/", "/modern/activity/", "/app/profile/", "/app/activity/"),
        ),
    ),
    (
        "google.com",
        ManagedBrowserPolicy("browser_google_search", "Google Search", "search_results", ("/search",)),
    ),
    (
        "google.ie",
        ManagedBrowserPolicy("browser_google_search", "Google Search", "search_results", ("/search",)),
    ),
    (
        "bing.com",
        ManagedBrowserPolicy("browser_bing_search", "Bing Search", "search_results", ("/search",)),
    ),
    (
        "mojeek.com",
        ManagedBrowserPolicy("browser_mojeek_search", "Mojeek Search", "search_results", ("/search",)),
    ),
    (
        "duckduckgo.com",
        ManagedBrowserPolicy("browser_duckduckgo_search", "DuckDuckGo Search", "search_results", ("/",)),
    ),
    (
        "brave.com",
        ManagedBrowserPolicy("browser_brave_search", "Brave Search", "search_results", ("/search",)),
    ),
    (
        "qwant.com",
        ManagedBrowserPolicy("browser_qwant_search", "Qwant Search", "search_results", ("/",)),
    ),
    (
        "startpage.com",
        ManagedBrowserPolicy("browser_startpage_search", "Startpage Search", "search_results", ("/sp/search",)),
    ),
)


INSTAGRAM_RESERVED_PATHS = {
    "about",
    "accounts",
    "developer",
    "direct",
    "explore",
    "legal",
    "p",
    "reel",
    "reels",
    "stories",
    "web",
}
FACEBOOK_RESERVED_PATHS = {
    "ads",
    "business",
    "events",
    "friends",
    "gaming",
    "groups",
    "help",
    "home.php",
    "login",
    "marketplace",
    "messages",
    "notifications",
    "pages",
    "privacy",
    "reel",
    "reels",
    "settings",
    "share",
    "stories",
    "watch",
}


def _path_segments(raw_url: str) -> tuple[str, ...]:
    return tuple(part for part in unquote(urlsplit(raw_url).path).split("/") if part)


def _instagram_profile_url(raw_url: str) -> bool:
    segments = _path_segments(raw_url)
    return len(segments) == 1 and segments[0].casefold() not in INSTAGRAM_RESERVED_PATHS


def _facebook_profile_url(raw_url: str) -> bool:
    parsed = urlsplit(raw_url)
    segments = _path_segments(raw_url)
    if unquote(parsed.path).casefold() == "/profile.php":
        identifiers = parse_qs(parsed.query).get("id", [])
        return bool(identifiers and identifiers[0].isdigit())
    if len(segments) == 3 and segments[0].casefold() == "people":
        return segments[2].isdigit()
    return len(segments) == 1 and segments[0].casefold() not in FACEBOOK_RESERVED_PATHS


def _tiktok_profile_url(raw_url: str) -> bool:
    segments = _path_segments(raw_url)
    return len(segments) == 1 and segments[0].startswith("@") and len(segments[0]) > 1


def policy_for_url(raw_url: str) -> ManagedBrowserPolicy | None:
    parsed = urlsplit(raw_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    path = unquote(parsed.path)
    for suffix, policy in POLICIES:
        if host != suffix and not host.endswith(f".{suffix}"):
            continue
        if policy.platform == "Instagram":
            if _instagram_profile_url(raw_url):
                return policy
            continue
        if policy.platform == "Facebook":
            if _facebook_profile_url(raw_url):
                return policy
            continue
        if policy.platform == "TikTok":
            if _tiktok_profile_url(raw_url):
                return policy
            continue
        if policy.platform == "Pinterest":
            if re.fullmatch(r"/[A-Za-z0-9_]{3,30}/?", path):
                return policy
            continue
        if policy.platform == "Threads":
            if re.fullmatch(r"/@[A-Za-z0-9._]{1,30}/?", path):
                return policy
            continue
        patterns = {
            "Snapchat": r"/(?:add|@)/?[A-Za-z0-9._-]+/?",
            "Telegram": r"/[A-Za-z][A-Za-z0-9_]{4,31}/?",
            "Discord": r"/users/\d{17,20}/?",
            "WhatsApp": r"/(?:send)?",
        }
        if policy.platform in patterns:
            if re.fullmatch(patterns[policy.platform], path):
                return policy
            continue
        if any(marker.casefold() in path.casefold() for marker in policy.path_markers):
            return policy
    return None


def collector_for_url(raw_url: str) -> str:
    policy = policy_for_url(raw_url)
    return policy.collector_id if policy else "generic_capture"


_SEARCH_CHROME_HOSTS = frozenset(
    {
        "google.com",
        "google.ie",
        "bing.com",
        "duckduckgo.com",
        "brave.com",
        "qwant.com",
        "startpage.com",
        "webcache.googleusercontent.com",
        "googleadservices.com",
        "doubleclick.net",
        "go.microsoft.com",
    }
)


def _is_search_chrome_link(url: str) -> bool:
    """True for a search engine's own navigational/ad/cache links, not a genuine result."""
    host = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    return any(host == chrome or host.endswith(f".{chrome}") for chrome in _SEARCH_CHROME_HOSTS)


def search_results_from_capture(result: BrowserCaptureResult) -> tuple[list[SearchResultItem], int]:
    """Turn a Google/Bing SERP capture's generic link extraction into search-result items.

    Reuses the same redirect-unwrapping and URL validation as the manual paste-in import path.
    Result cards recognised by the browser agent carry a title and snippet; plain page links
    (used only when no card was recognised) carry just their visible text, so `snippet` is None.
    """
    payload = result.extracted or {}
    structured = payload.get("search_results")
    raw_links = structured if isinstance(structured, list) and structured else payload.get("links")
    if not isinstance(raw_links, list):
        return [], 0
    rejected = 0
    unique: dict[str, SearchResultItem] = {}
    for entry in raw_links:
        if len(unique) >= MAX_IMPORT_RESULTS:
            break
        if not isinstance(entry, dict):
            continue
        href = entry.get("href")
        if not isinstance(href, str) or not href:
            continue
        unwrapped = unwrap_search_url(href)
        if _is_search_chrome_link(unwrapped):
            continue
        try:
            canonical = validate_source_url(unwrapped)
        except ValueError:
            rejected += 1
            continue
        if canonical is None or canonical in unique:
            continue
        unique[canonical] = SearchResultItem(
            url=canonical,
            title=_text(entry.get("title") or entry.get("text"), 500),
            snippet=_text(entry.get("snippet"), 2000),
        )
    return list(unique.values()), rejected


def _query_from_search_url(requested_url: str) -> tuple[str, str] | None:
    """Recover the (engine, query) pair from a Corvus-generated Google/Bing search URL.

    Avoids threading extra form fields/route plumbing through the generic browser-capture
    endpoint just to carry search context - it's already encoded in the captured URL itself.
    """
    parsed = urlsplit(requested_url)
    host = (parsed.hostname or "").casefold().removeprefix("www.").rstrip(".")
    engine = {"google.com": "google", "google.ie": "google", "bing.com": "bing", "mojeek.com": "mojeek", "duckduckgo.com": "duckduckgo", "search.brave.com": "brave", "brave.com": "brave", "qwant.com": "qwant", "startpage.com": "startpage"}.get(host)
    if engine is None:
        return None
    params = parse_qs(parsed.query)
    values = params.get("query") if engine == "startpage" else params.get("q")
    query = values[0].strip() if values else ""
    return (engine, query) if query else None


def _result_honours_query_operators(query: str, url: str) -> bool:
    """Reject a result that ignores the query's own site: or filetype: restrictions.

    Engines sometimes drop operators and return their generic results for the bare term;
    live runs on 2026-11-12 saw Bing return the same ten links for three differently
    restricted queries. Such results do not answer the question the query asked.
    """
    parsed = urlsplit(url)
    # Every positive site: term counts, so a grouped (site:a OR site:b) query accepts either.
    expected_hosts = [
        # site:*.example.org means any subdomain; the suffix comparison below covers that.
        match.split("/", 1)[0].casefold().removeprefix("*.").removeprefix("www.").rstrip(".")
        for match in re.findall(r"(?<![-\w])site:([^\s)]+)", query, flags=re.IGNORECASE)
    ]
    if expected_hosts:
        host = (parsed.hostname or "").casefold().removeprefix("www.").rstrip(".")
        if not any(host == expected or host.endswith(f".{expected}") for expected in expected_hosts):
            return False
    extensions = {
        match.casefold()
        for match in re.findall(r"(?<![-\w])filetype:([a-z0-9]{1,8})", query, flags=re.IGNORECASE)
    }
    if extensions:
        path = unquote(parsed.path).casefold()
        if not any(path.endswith(f".{extension}") for extension in extensions):
            return False
    return True


def uses_search_operators(query: str) -> bool:
    return bool(re.search(r"(?<![-\w])(?:site|filetype):", query, flags=re.IGNORECASE))


def _email_parts_in(email: str, searchable: str) -> bool:
    """True when the address, or its local part together with its domain, appears."""
    address = email.strip().casefold()
    if "@" not in address:
        return False
    if address in searchable:
        return True
    local, domain = address.rsplit("@", 1)
    return len(local) >= 3 and domain in searchable and _handle_appears_in(local, searchable)


def _phone_in(phone: str, searchable: str) -> bool:
    """True when the number's digits appear, however the page spaces or punctuates them."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 7:
        return False
    # Compare the national part as well, so +353 86 123 4567 matches 086 123 4567.
    return any(part in re.sub(r"[\s().\-]", "", searchable) for part in {digits, digits[-9:]})


def _search_item_matches_investigation(
    investigation: Investigation, query: str, item: SearchResultItem
) -> bool:
    if not _result_honours_query_operators(query, item.url):
        return False
    subject_type, subject_value = investigation.query_type, investigation.query
    # A case can hold several identifiers; judge the result against the one this query quotes.
    lowered_query = query.casefold()
    for seed in case_seeds(investigation):
        quoted = '"' + " ".join(seed.value.strip().lstrip("@").split()).casefold() + '"'
        if seed.query_type.value in {"username", "name"} and quoted in lowered_query:
            subject_type, subject_value = seed.query_type.value, seed.value
            break
        if seed.query_type.value == "email" and _email_parts_in(seed.value, lowered_query):
            subject_type, subject_value = "email", seed.value
            break
        if seed.query_type.value == "phone" and _phone_in(seed.value, lowered_query):
            subject_type, subject_value = "phone", seed.value
            break
    if subject_type in {"email", "phone"}:
        if not item.snippet:
            # Without the page text there is nothing to check the value against, so only keep
            # a result when the query's own site: or filetype: restriction (verified above)
            # narrows it; an unrestricted engine fallback is otherwise indistinguishable.
            return uses_search_operators(query)
        searchable = f"{item.url} {item.title or ''} {item.snippet}".casefold()
        if subject_type == "email":
            return _email_parts_in(subject_value, searchable)
        return _phone_in(subject_value, searchable)
    if subject_type == "name":
        return name_result_is_relevant(
            subject_value,
            {"url": item.url, "title": item.title, "snippet": item.snippet},
        )
    if subject_type != "username":
        return True
    handle = subject_value.strip().lstrip("@").casefold()
    if not handle:
        return False
    return username_result_is_relevant(handle, item.url, item.title, item.snippet)


def import_managed_search_results(
    db: Session, investigation: Investigation, result: BrowserCaptureResult
) -> int:
    recovered = _query_from_search_url(result.requested_url)
    if recovered is None:
        return 0
    engine, query = recovered
    items, rejected = search_results_from_capture(result)
    relevant_items = [
        item for item in items if _search_item_matches_investigation(investigation, query, item)
    ]
    rejected += len(items) - len(relevant_items)
    if not relevant_items:
        return 0
    query_id = f"browser-{engine}-{hashlib.sha256(query.encode()).hexdigest()[:10]}"
    summary = import_search_result_items(
        db,
        investigation,
        relevant_items,
        rejected,
        engine=engine,
        query=query,
        query_id=query_id,
        query_label=f"{engine.title()} search (sock browser): {query}"[:200],
        source_tool="search_browser_capture",
        tool_display_name="Search result (managed browser)",
        event_type="search_results_captured",
    )
    return summary.imported_count


def _walk(value: Any):  # type: ignore[no-untyped-def]
    pending = [(value, 0)]
    visited = 0
    while pending and visited < 5000:
        item, depth = pending.pop()
        visited += 1
        if depth > 16:
            continue
        if isinstance(item, dict):
            yield item
            pending.extend((child, depth + 1) for child in reversed(list(item.values())[:500]))
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in reversed(item[:500]))


def _direct_value(mapping: dict[str, Any], *keys: str) -> Any:
    wanted = {key.casefold() for key in keys}
    for key, value in mapping.items():
        if key.casefold() in wanted and value not in (None, "", [], {}):
            return value
    return None


def _text(value: Any, limit: int) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        value = _direct_value(value, "url", "contentUrl", "name", "value")
    elif isinstance(value, list):
        value = next((item for item in value if item not in (None, "", [], {})), None)
    if value in (None, "") or isinstance(value, dict | list):
        return None
    cleaned = " ".join(str(value).split())
    return cleaned[:limit] or None


def _number(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, dict):
        value = _direct_value(value, "count", "value", "total")
    if value is None:
        return None
    cleaned = str(value).strip().casefold().replace(",", "").replace(" ", "")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([kmb])?", cleaned)
    if not match:
        return None
    multiplier = {None: 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[match.group(2)]
    return max(0, int(float(match.group(1)) * multiplier))


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalised = str(value).strip().casefold()
    if normalised in {"true", "1", "yes", "verified"}:
        return True
    if normalised in {"false", "0", "no", "unverified"}:
        return False
    return None


def _normalise_handle(value: Any) -> str | None:
    text = _text(value, 320)
    return text.lstrip("@").casefold() if text else None


def _candidate_mappings(payload: dict[str, Any]):  # type: ignore[no-untyped-def]
    seen: set[int] = set()
    # The walk is bounded, and page state is often larger than the bound (a TikTok profile
    # page held 6,559 values on 2026-11-13). Read the browser agent's already-filtered
    # profile objects first so a large page cannot push them past the limit.
    for mapping in (*_walk(payload.get("profile_objects")), *_walk(payload)):
        if id(mapping) not in seen:
            seen.add(id(mapping))
            yield mapping
        for key in ("user", "profile", "account", "author"):
            child = mapping.get(key)
            if not isinstance(child, dict):
                continue
            combined = dict(child)
            for stats_key in ("stats", "statsV2", "statistics", "counts"):
                stats = mapping.get(stats_key)
                if isinstance(stats, dict):
                    combined.update(stats)
            yield combined


def _identity_candidate(
    payload: dict[str, Any], fallback_username: str | None, username_keys: tuple[str, ...],
    expected_id: str | None = None,
) -> dict[str, Any]:
    target = _normalise_handle(fallback_username)
    best: dict[str, Any] = {}
    best_score = -1
    richness_keys = {
        "avatarlarger",
        "avatar_url",
        "biography",
        "bio",
        "description",
        "display_name",
        "followercount",
        "followers",
        "full_name",
        "fullname",
        "id",
        "image",
        "nickname",
        "profile_pic_url",
        "profile_pic_url_hd",
        "signature",
        "verified",
        "subscribercount",
        "profilepictureurl",
    }
    for mapping in _candidate_mappings(payload):
        candidate_username = _normalise_handle(_direct_value(mapping, *username_keys))
        candidate_id = _text(_direct_value(mapping, "id", "user_id", "account_id"), 320)
        if expected_id and candidate_id != expected_id:
            continue
        if target and candidate_username != target:
            continue
        if not target and not expected_id:
            continue
        score = 0
        if target and candidate_username == target:
            score += 100
        elif candidate_username:
            score += 20
        score += sum(1 for key in mapping if key.casefold() in richness_keys)
        if score > best_score:
            best = mapping
            best_score = score
    return best


def _meta(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("meta")
    return value if isinstance(value, dict) else {}


def _meta_value(meta: dict[str, Any], *keys: str) -> Any:
    return _direct_value(meta, *keys)


def _profile_url(platform: str, requested_url: str, extracted: dict[str, Any]) -> str:
    candidate = extracted.get("canonical_url") or extracted.get("url") or requested_url
    canonical = canonicalise_url(str(candidate))
    if not canonical:
        raise BrowserAgentError(
            "invalid_extraction", f"{platform} extraction returned no public URL"
        )
    policy = policy_for_url(canonical)
    if policy is None or policy.platform.casefold() != platform:
        raise BrowserAgentError("invalid_extraction", "Profile URL does not match the requested platform")
    expected = _path_username(platform, requested_url)
    observed = _path_username(platform, canonical)
    if expected and observed and expected.casefold() != observed.casefold():
        raise BrowserAgentError("identity_mismatch", "Captured page belongs to a different profile")
    return canonical


def _path_username(platform: str, url: str) -> str | None:
    parsed = urlsplit(url)
    segments = _path_segments(url)
    if platform == "snapchat":
        return segments[-1].removeprefix("@") if segments else None
    if platform == "telegram" and len(segments) == 1:
        return segments[0]
    if platform == "instagram" and len(segments) == 1:
        return segments[0]
    if platform == "tiktok" and len(segments) == 1 and segments[0].startswith("@"):
        return segments[0][1:]
    if platform == "threads" and len(segments) == 1 and segments[0].startswith("@"):
        return segments[0][1:]
    if platform == "pinterest" and len(segments) == 1:
        return segments[0]
    if platform == "facebook":
        if parsed.path.casefold() == "/profile.php":
            return None
        if len(segments) == 3 and segments[0].casefold() == "people":
            return segments[1]
        if len(segments) == 1:
            return segments[0]
    return None


_ABSENT_ACCOUNT_RE = re.compile(
    r"couldn['’]t find this account|could not find this account|page (?:isn['’]t|is not) available"
    r"|page not found|user not found|account (?:doesn['’]t|does not) exist|profile (?:isn['’]t|is not) available"
    r"|content (?:isn['’]t|is not) available|no longer available|this account has been (?:suspended|banned)"
    r"|sorry, this page|^404\b|\b404 not found",
    re.IGNORECASE,
)


_ABSENT_BODY_RE = re.compile(
    r"this content was not found|profile isn['’]t available|couldn['’]t find this account"
    r"|sorry, this page isn['’]t available|this account doesn['’]t exist",
    re.IGNORECASE,
)
# A title that is only the site's own name says nothing about whose page it is.
_SITE_TITLES = frozenset(
    {"snapchat", "tiktok", "instagram", "facebook", "pinterest", "threads", "telegram", "discord", "whatsapp",
     "tiktok - make your day", "log in", "login", "sign up"}
)


def _title_identity(platform: str, title: str | None) -> tuple[str | None, str | None]:
    if not title:
        return None, None
    if platform in {"instagram", "tiktok", "threads"}:
        match = re.match(r"^(.*?)\s*\(@([^\)]+)\)", title)
        if match:
            return _text(match.group(1), 500), _text(match.group(2), 320)
        # These platforms title a profile "Name (@handle)". Any other title is the site's own
        # ("TikTok - Make Your Day") or an error message, never a display name.
        return None, None
    if platform == "facebook":
        cleaned = re.sub(r"\s*[|·-]\s*Facebook.*$", "", title, flags=re.IGNORECASE).strip()
        return (None if cleaned.casefold() in _SITE_TITLES else _text(cleaned, 500)), None
    if title.strip().casefold() in _SITE_TITLES:
        return None, None
    return _text(title, 500), None


def _count_from_description(description: str | None, label: str) -> int | None:
    if not description:
        return None
    match = re.search(rf"([0-9][0-9,.]*\s*[KMB]?)\s+{re.escape(label)}\b", description, re.I)
    return _number(match.group(1)) if match else None


def _platform_label(platform: str) -> str:
    return {"instagram": "Instagram", "facebook": "Facebook", "tiktok": "TikTok",
            "whatsapp": "WhatsApp", "snapchat": "Snapchat", "telegram": "Telegram",
            "discord": "Discord"}.get(
        platform, platform.replace("_", " ").title()
    )


def normalise_social_profile(
    platform: str, requested_url: str, payload: dict[str, Any]
) -> tuple[str, str | None, dict[str, Any], list[dict[str, str]]]:
    platform_key = platform.casefold().replace(" ", "_")
    url = _profile_url(platform_key, requested_url, payload)
    if platform_key in {"whatsapp", "discord"}:
        panel = payload.get("visible_profile")
        if not isinstance(panel, dict) or panel.get("source_scope") != "operator_opened_profile_panel":
            return url, None, {}, []
        identifier = _text(panel.get("account_id"), 30)
        if not identifier or not identifier.isdigit():
            return url, None, {}, []
        if platform_key == "discord" and urlsplit(url).path.rstrip("/") != f"/users/{identifier}":
            return url, None, {}, []
        if platform_key == "whatsapp":
            expected = parse_qs(urlsplit(requested_url).query).get("phone", [""])[0]
            if expected and re.sub(r"\D", "", expected) != identifier:
                return url, None, {}, []
            url = f"https://wa.me/{identifier}"
        panel_profile = {"account_id": identifier}
        avatar = _text(panel.get("avatar_url"), 2048)
        if avatar and avatar.startswith("https://"):
            panel_profile["avatar_url"] = avatar
        panel_details = [{"label": "Acquisition", "value": "Operator-opened contact/profile panel"}]
        label = _text(panel.get("panel_label"), 300)
        if label:
            panel_details.append({"label": "Local panel label (not verified identity)", "value": label})
        return url, identifier, panel_profile, panel_details
    fallback_username = _path_username(platform_key, url)
    username_keys = {
        "instagram": ("username", "userName", "alternateName"),
        "facebook": ("username", "userName", "vanity", "alternateName"),
        "tiktok": ("uniqueId", "username", "userName", "alternateName"),
        "snapchat": ("snap_user_name", "username", "userName", "alternateName"),
    }.get(platform_key, ("username", "userName", "alternateName"))
    parsed_url = urlsplit(url)
    expected_id = None
    if platform_key == "facebook":
        expected_id = parse_qs(parsed_url.query).get("id", [None])[0]
        if parsed_url.path.startswith("/people/"):
            expected_id = parsed_url.path.rstrip("/").rsplit("/", 1)[-1]
            fallback_username = None
    candidate = _identity_candidate(payload, fallback_username, username_keys, expected_id)
    meta = _meta(payload)
    meta_title = _text(_meta_value(meta, "og:title", "twitter:title") or payload.get("title"), 500)
    page_title = _text(payload.get("title"), 500)
    # The same message also appears at the top of the page body when the title is only the
    # site's name (Snapchat's 404 page is titled "Snapchat").
    body_start = (_text(payload.get("visible_text"), 1500) or "")
    if any(_ABSENT_ACCOUNT_RE.search(text) for text in (meta_title, page_title) if text) or (
        _ABSENT_BODY_RE.search(body_start)
    ):
        # The platform answered with its "no such account" page. On 2026-11-13 TikTok served
        # one with HTTP 200 for an unused handle, and its message was being read as a name.
        return url, fallback_username, {}, []
    title_name, title_username = _title_identity(platform_key, meta_title)
    if title_username and fallback_username and title_username.casefold() != fallback_username.casefold():
        raise BrowserAgentError("identity_mismatch", "Profile title identifies a different account")
    if not candidate and (not meta_title or re.search(
        r"^(?:log\s?in|sign\s?up|telegram:\s*contact|telegram:\s*join|facebook$|instagram$|tiktok$|snapchat$)",
        meta_title, re.I,
    )):
        return url, fallback_username, {}, []
    username = (
        _text(_direct_value(candidate, *username_keys), 320) or title_username or fallback_username
    )
    if username:
        username = username.lstrip("@")[:320]

    field_keys: dict[str, tuple[str, ...]] = {
        "display_name": ("nickname", "full_name", "fullname", "display_name", "displayName", "name", "title"),
        "biography": ("signature", "biography", "bio", "description"),
        "avatar_url": (
            "avatarLarger",
            "avatarMedium",
            "profile_pic_url_hd",
            "profile_pic_url",
            "avatar_url",
            "image",
            "profilePictureUrl",
        ),
        "account_id": ("user_id", "userid", "account_id", "id"),
        "location": ("location", "city", "hometown"),
        "verified": ("verified", "is_verified", "isVerified"),
        "private": ("privateAccount", "is_private", "is_private_account", "isPrivate"),
        "followers": ("followerCount", "followers", "followers_count", "edge_followed_by", "subscriber_count", "subscriberCount"),
        "following": ("followingCount", "following", "following_count", "edge_follow"),
        "posts": (
            "videoCount",
            "post_count",
            "posts",
            "media_count",
            "edge_owner_to_timeline_media",
        ),
        "created_at": ("createTime", "created_at", "account_creation_date"),
    }
    description_meta = _text(
        _meta_value(meta, "og:description", "twitter:description", "description"), 4000
    )
    display_name = _text(_direct_value(candidate, *field_keys["display_name"]), 500) or title_name
    biography = _text(_direct_value(candidate, *field_keys["biography"]), 4000)
    if (
        not biography
        and description_meta
        and not re.search(
            r"See Instagram photos and videos from|TikTok.*followers", description_meta, re.I
        )
    ):
        biography = description_meta
    avatar = _text(_direct_value(candidate, *field_keys["avatar_url"]), 2048) or _text(
        _meta_value(meta, "og:image", "twitter:image"), 2048
    )
    account_id = _text(_direct_value(candidate, *field_keys["account_id"]), 320)
    if platform_key == "facebook" and not account_id:
        parsed = urlsplit(url)
        identifiers = parse_qs(parsed.query).get("id", [])
        account_id = identifiers[0][:320] if identifiers and identifiers[0].isdigit() else None
        app_url = _text(_meta_value(meta, "al:android:url", "al:ios:url"), 2048)
        if not account_id and app_url:
            match = re.search(r"(?:profile|user)[/:](\d+)", app_url)
            account_id = match.group(1) if match else None

    followers = _number(_direct_value(candidate, *field_keys["followers"]))
    following = _number(_direct_value(candidate, *field_keys["following"]))
    posts = _number(_direct_value(candidate, *field_keys["posts"]))
    if platform_key == "instagram":
        followers = followers if followers is not None else _count_from_description(description_meta, "followers")
        following = following if following is not None else _count_from_description(description_meta, "following")
        posts = posts if posts is not None else _count_from_description(description_meta, "posts")

    profile = {
        "account_id": account_id,
        "display_name": display_name,
        "biography": biography,
        "location": _text(_direct_value(candidate, *field_keys["location"]), 500),
        "avatar_url": avatar,
        "verified": _boolean(_direct_value(candidate, *field_keys["verified"])),
        "private": _boolean(_direct_value(candidate, *field_keys["private"])),
        "followers": followers,
        "following": following,
        "posts": posts,
        "created_at": _text(_direct_value(candidate, *field_keys["created_at"]), 100),
    }
    profile = {key: value for key, value in profile.items() if value is not None}

    details: list[dict[str, str]] = []
    detail_fields = (
        ("Secure user ID", ("secUid", "secure_uid")),
        ("Likes", ("heartCount", "likes", "like_count")),
        ("Friends", ("friendCount", "friends", "friends_count")),
        ("Category", ("category_name", "category", "business_category_name")),
        ("Website", ("external_url", "website", "websiteUrl")),
        ("Public profile ID", ("businessProfileId",)),
        ("Snapcode image", ("snapcodeImageUrl",)),
        ("Public email", ("public_email",)),
        ("Public phone", ("public_phone_number",)),
    )
    for label, keys in detail_fields:
        value = _direct_value(candidate, *keys)
        rendered = _number(value) if label in {"Likes", "Friends"} else _text(value, 500)
        if rendered is not None:
            details.append({"label": label, "value": str(rendered)})
    return url, username, profile, details


def _generic_display_name(value: Any, platform: str) -> bool:
    text = _text(value, 500)
    return not text or text.casefold() == _platform_label(platform).casefold()


def _merge_profile(existing: Any, incoming: dict[str, Any], platform: str) -> dict[str, Any]:
    merged = dict(existing) if isinstance(existing, dict) else {}
    for key, value in incoming.items():
        current = merged.get(key)
        if key == "display_name":
            if _generic_display_name(value, platform):
                continue
            if current in (None, "") or _generic_display_name(current, platform):
                merged[key] = value
        elif key in {"followers", "following", "posts"}:
            if current is None or (
                isinstance(current, int) and isinstance(value, int) and value > current
            ):
                merged[key] = value
        elif key == "verified":
            merged[key] = bool(current) or bool(value)
        elif current in (None, ""):
            merged[key] = value
    return merged


def _merge_details(existing: Any, incoming: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: dict[str, str] = {}
    for collection in (existing, incoming):
        if not isinstance(collection, list):
            continue
        for item in collection:
            if isinstance(item, dict):
                label = _text(item.get("label"), 200)
                value = _text(item.get("value"), 500)
                if label and value:
                    merged[label] = value
    return [{"label": label, "value": value} for label, value in merged.items()]


def social_profile_was_read(result: BrowserCaptureResult) -> bool:
    """True when a capture held a usable profile for the requested account.

    import_social_profile returns 0 both when it enriches an existing finding and when the
    page held nothing, so callers that must tell those apart ask this instead.
    """
    platform = result.extraction_platform or "social"
    try:
        _, _, profile, _ = normalise_social_profile(
            platform, result.requested_url, result.extracted or {}
        )
    except BrowserAgentError:
        return False
    if platform in {"whatsapp", "discord"}:
        return bool(profile.get("account_id"))
    return any(profile.get(key) for key in ("display_name", "biography", "avatar_url"))


def import_social_profile(
    db: Session,
    investigation: Investigation,
    result: BrowserCaptureResult,
    collector_id: str,
) -> int:
    payload = result.extracted or {}
    platform = result.extraction_platform or "social"
    try:
        url, username, profile, details = normalise_social_profile(
            platform, result.requested_url, payload
        )
    except BrowserAgentError:
        return 0
    contact_panel = platform in {"whatsapp", "discord"} and bool(profile.get("account_id"))
    if not profile or (not contact_panel and not any(profile.get(key) for key in ("display_name", "biography", "avatar_url"))):
        return 0
    if platform == "whatsapp" and investigation.query_type == "phone":
        if re.sub(r"\D", "", investigation.query) != username:
            return 0
    username = (username or investigation.query)[:320]
    display_name = _text(profile.get("display_name"), 500)
    description = _text(profile.get("biography"), 4000)
    observation_metadata: dict[str, Any] = {
        "managed_browser": True,
        "browser_collector": collector_id,
        "browser_manifest": result.manifest_reference,
        "source_observed_at": result.observed_at.isoformat(),
    }
    if result.authenticated_session is not None:
        observation_metadata["authenticated_observation"] = result.authenticated_session
    metadata: dict[str, Any] = {**observation_metadata, "profile": profile}
    metadata["profile_extraction"] = {
        "state": "parsed", "fields": sorted(profile),
        "method": "visible_contact_panel" if contact_panel else "rendered_profile_metadata",
    }
    evidence_status = "likely" if contact_panel else "found"
    evidence_confidence = 0.65 if contact_panel else 0.95
    if details:
        metadata["platform_details"] = details

    existing = db.scalar(
        select(Finding).where(
            Finding.investigation_id == investigation.id,
            Finding.canonical_url == url,
        )
    )
    if existing:
        combined = dict(existing.extra or {})
        combined.update(observation_metadata)
        combined["profile"] = _merge_profile(combined.get("profile"), profile, platform)
        combined["platform_details"] = _merge_details(combined.get("platform_details"), details)
        source_metadata = combined.get("source_metadata")
        source_metadata = dict(source_metadata) if isinstance(source_metadata, dict) else {}
        source_metadata[collector_id] = {
            **observation_metadata,
            "profile_fields": sorted(profile),
            "profile": profile,
            "platform_details": details,
        }
        combined["source_metadata"] = source_metadata
        existing.extra = combined
        existing.source_tools = sorted(
            set(existing.source_tools or [existing.source_tool]) | {collector_id}
        )
        existing.title = display_name or existing.title
        existing.description = description or existing.description
        if existing.status != "found":
            existing.status = evidence_status
        existing.confidence = max(existing.confidence or 0.0, evidence_confidence)
        existing.observed_at = result.observed_at
        if not existing.source_record_id and profile.get("account_id"):
            existing.source_record_id = str(profile["account_id"])
        return 0
    run = ToolRun(
        investigation_id=investigation.id,
        tool_id=collector_id,
        display_name=f"Managed browser: {_platform_label(platform)}",
        status="completed",
        tool_version=result.browser_version,
        timeout_seconds=120,
        query_value=investigation.query,
        query_type=investigation.query_type,
        started_at=result.observed_at,
        finished_at=datetime.now(UTC),
        raw_output_reference=result.manifest_reference,
    )
    db.add(run)
    db.flush()
    db.add(
        Finding(
            investigation_id=investigation.id,
            tool_run_id=run.id,
            query_type=investigation.query_type,
            queried_value=investigation.query,
            finding_type="profile",
            platform=_platform_label(platform),
            value=username,
            canonical_url=url,
            display_url=url,
            status=evidence_status,
            confidence=evidence_confidence,
            title=display_name,
            description=description,
            extra=metadata,
            source_tool=collector_id,
            source_tools=[collector_id],
            source_record_id=str(profile.get("account_id") or username),
            observed_at=result.observed_at,
            raw_output_reference=result.manifest_reference,
        )
    )
    return 1


def _location_page_content(result: BrowserCaptureResult, evidence_root: Path) -> bytes | None:
    if result.html_reference:
        path = resolve_evidence_path(result.html_reference, evidence_root)
        if path.is_file() and path.stat().st_size <= 2 * 1024 * 1024:
            return path.read_bytes()
    payload = result.extracted or {}
    raw_meta = payload.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    raw_jsonld = payload.get("jsonld")
    jsonld = raw_jsonld if isinstance(raw_jsonld, list) else []
    visible_text = _text(payload.get("visible_text"), 120_000) or ""
    if not meta and not jsonld:
        return None
    parts = ["<html><head>"]
    for key, value in list(meta.items())[:80]:
        name = _text(key, 200)
        meta_content = _text(value, 4000)
        if name and meta_content:
            parts.append(
                f'<meta name="{html.escape(name, quote=True)}" '
                f'content="{html.escape(meta_content, quote=True)}">'
            )
    for document in jsonld[:25]:
        try:
            encoded = json.dumps(document, ensure_ascii=False).replace("</", "<\\/")
        except (TypeError, ValueError):
            continue
        parts.append(f'<script type="application/ld+json">{encoded}</script>')
    parts.append(f"</head><body>{html.escape(visible_text)}</body></html>")
    encoded_content = "".join(parts).encode("utf-8")
    return encoded_content if len(encoded_content) <= 2 * 1024 * 1024 else None


def import_location_history(
    db: Session,
    investigation: Investigation,
    result: BrowserCaptureResult,
    evidence_root: Path,
) -> int:
    content = _location_page_content(result, evidence_root)
    if content is None:
        return 0
    try:
        collected = parse_location_page(content, result.final_url)
    except LocationCollectionError:
        return 0
    inserted = 0
    for item in collected[:100]:
        payload = item.payload
        duplicate = db.scalar(
            select(LocationObservation).where(
                LocationObservation.investigation_id == investigation.id,
                LocationObservation.source_url == payload.source_url,
                LocationObservation.place_name == payload.place_name,
                LocationObservation.occurred_at == payload.occurred_at,
            )
        )
        if duplicate:
            duplicate.last_observed_at = result.observed_at
            continue
        db.add(
            LocationObservation(
                investigation_id=investigation.id,
                place_name=payload.place_name,
                locality=payload.locality,
                region=payload.region,
                country=payload.country,
                latitude=payload.latitude,
                longitude=payload.longitude,
                precision_radius_m=payload.precision_radius_m,
                evidence_type=payload.evidence_type.value,
                occurred_at=payload.occurred_at,
                published_at=payload.published_at,
                first_observed_at=result.observed_at,
                last_observed_at=result.observed_at,
                source_platform=payload.source_platform,
                source_url=payload.source_url,
                account_identifier=payload.account_identifier,
                privacy_masked=payload.privacy_masked,
                confidence=payload.confidence or 0.65,
                raw_evidence={
                    **payload.raw_evidence,
                    "managed_browser": True,
                    "browser_manifest": result.manifest_reference,
                    "authenticated_observation": result.authenticated_session,
                    "schema_type": item.schema_type,
                },
            )
        )
        inserted += 1
    return inserted
