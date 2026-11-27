import csv
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from pydantic import ValidationError

from app.adapters.base import Adapter, ExecutionResult
from app.adapters.bing_name import BingNameSearchAdapter
from app.adapters.commoncrawl import CommonCrawlAdapter
from app.adapters.metasearch2 import Metasearch2Adapter
from app.adapters.searxng_name import SearXNGNameSearchAdapter
from app.core.security import canonicalise_url
from app.models.schemas import FindingRecord, FindingStatus, ProfileEvidence, QueryType
from app.services.local_image_evidence import LocalImageEvidenceError, load_local_image


def _status(value: Any) -> FindingStatus:
    text = str(value).lower().replace("_", " ")
    if any(word in text for word in ("unclaimed", "not found", "false")):
        return FindingStatus.NOT_FOUND
    if "likely" in text:
        return FindingStatus.LIKELY
    if any(word in text for word in ("claimed", "found", "exists", "true")):
        return FindingStatus.FOUND
    if "rate" in text or "429" in text:
        return FindingStatus.RATE_LIMITED
    if "error" in text:
        return FindingStatus.ERROR
    return FindingStatus.UNKNOWN


def _first(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().casefold()
    if text in {"true", "yes", "1", "verified"}:
        return True
    if text in {"false", "no", "0", "unverified"}:
        return False
    return None


LANGUAGE_NAMES = {
    "ar": "Arabic",
    "ca": "Catalan",
    "cy": "Welsh",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "eo": "Esperanto",
    "es": "Spanish",
    "fi": "Finnish",
    "fr": "French",
    "ga": "Irish",
    "he": "Hebrew",
    "hi": "Hindi",
    "hu": "Hungarian",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "la": "Latin",
    "nl": "Dutch",
    "no": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sv": "Swedish",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "vi": "Vietnamese",
    "zh": "Chinese",
}


def _normalise_image_url(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("//"):
        return f"https:{value}"
    return value


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if candidate.startswith(("http://", "https://")) else None


def _year_value(value: Any) -> int | None:
    """Return the year when value is (or is a string of) exactly four digits in a plausible range."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if len(text) != 4 or not text.isascii() or not text.isdigit():
        return None
    year = int(text)
    return year if 1990 <= year <= datetime.now(UTC).year + 1 else None


_TIMEZONE_OFFSETS = {
    "UTC": 0,
    "GMT": 0,
    "BST": 1,
    "CET": 1,
    "CEST": 2,
    "EST": -5,
    "EDT": -4,
    "CST": -6,
    "CDT": -5,
    "MST": -7,
    "MDT": -6,
    "PST": -8,
    "PDT": -7,
}


def _normalise_timestamp(value: Any) -> str | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int | float) or (isinstance(value, str) and value.strip().isdigit()):
        year = _year_value(value)
        if year is not None:
            return datetime(year, 1, 1, tzinfo=UTC).isoformat()
        try:
            epoch = int(value)
            if epoch < 100_000_000:
                return None
            if epoch > 100_000_000_000:
                epoch //= 1000
            return datetime.fromtimestamp(epoch, tz=UTC).isoformat()
        except (OSError, OverflowError, TypeError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    cleaned = re.sub(r"\s*\([^)]*ago\)\s*$", "", text, flags=re.I).strip()
    cleaned = re.sub(r"\s+UTC$", "+00:00", cleaned, flags=re.I)
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    zone_offset: timezone | None = None
    zone_match = re.search(r"\s+([A-Za-z]{3,4})$", cleaned)
    if zone_match and zone_match.group(1).upper() in _TIMEZONE_OFFSETS:
        zone_offset = timezone(timedelta(hours=_TIMEZONE_OFFSETS[zone_match.group(1).upper()]))
        cleaned = cleaned[: zone_match.start()].strip()
    cleaned = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)\b", r"\1", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+of\s+", " ", cleaned, flags=re.I)
    russian_months = {
        "января": "January",
        "февраля": "February",
        "марта": "March",
        "апреля": "April",
        "мая": "May",
        "июня": "June",
        "июля": "July",
        "августа": "August",
        "сентября": "September",
        "октября": "October",
        "ноября": "November",
        "декабря": "December",
    }
    for source, target in russian_months.items():
        cleaned = re.sub(source, target, cleaned, flags=re.I)
    cleaned = re.sub(r"^[^,]+,\s*(?=\d{1,2}\s+[A-Za-z]+\s+\d{4})", "", cleaned)
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        parsed = None
    if parsed is None:
        for pattern in (
            "%B %d, %Y",
            "%b %d, %Y",
            "%d %B %Y",
            "%d %B %Y, %H:%M",
            "%Y-%m-%d",
            "%Y-%m-%d %H:%M:%S",
            "%a, %d %b %Y %H:%M:%S %z",
            "%d %b %Y %H:%M:%S %z",
            "%A %d %B %Y %I:%M:%S %p",
        ):
            try:
                parsed = datetime.strptime(cleaned, pattern)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone_offset or UTC)
    return parsed.astimezone(UTC).isoformat()


def _timestamp_precision(raw_value: Any) -> str:
    text = str(raw_value or "").strip()
    if not text:
        return "unknown"
    if _year_value(raw_value) is not None:
        return "year"
    date_only_patterns = (
        r"\d{4}-\d{2}-\d{2}",
        r"[A-Za-z]+\s+\d{1,2},\s+\d{4}(?:\s*\([^)]*\))?",
        r"\d{1,2}\s+[A-Za-zА-Яа-яЁё]+\s+\d{4}",
    )
    return "day" if any(re.fullmatch(pattern, text) for pattern in date_only_patterns) else "second"


def _display_timestamp(raw_value: Any) -> Any:
    """Format a timestamp-like value for display; return the raw value when it cannot be parsed."""
    if raw_value in (None, "") or isinstance(raw_value, bool):
        return raw_value
    normalised = _normalise_timestamp(raw_value)
    if normalised is None:
        return raw_value
    parsed = datetime.fromisoformat(normalised)
    is_epoch = isinstance(raw_value, int | float) or (
        isinstance(raw_value, str) and raw_value.strip().isdigit()
    )
    precision = _timestamp_precision(raw_value)
    if precision == "year":
        return str(parsed.year)
    midnight = parsed.hour == parsed.minute == parsed.second == 0
    if is_epoch or precision == "day" or midnight:
        return parsed.date().isoformat()
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


_PLACEHOLDER_AVATAR_TOKENS = (
    "default_profile",
    "default_avatar",
    "avatar_default",
    "/avatar/default",
    "default-user",
    "default_user",
    "guest.png",
    "no-avatar",
    "noavatar",
    "no_avatar",
    "placeholder",
    "blank-profile",
)


def _is_placeholder_avatar(url: Any) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        path = urlsplit(url.strip()).path.lower()
    except ValueError:
        return False
    if any(token in path for token in _PLACEHOLDER_AVATAR_TOKENS):
        return True
    return re.search(r"/default\.(?:png|jpg|jpeg|gif|svg|webp)$", path) is not None


_BOILERPLATE_BIOGRAPHY = re.compile(r"\bis on (snapchat|facebook|instagram|tiktok|threads)\b", re.I)


def _is_boilerplate_biography(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    return (
        _BOILERPLATE_BIOGRAPHY.search(text[:120]) is not None
        or "join facebook to connect" in text.casefold()
    )


def _timeline_events(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Create explicitly-labelled timeline evidence without overstating date semantics."""
    extractor = str(record.get("_extractor") or "").strip()
    extractor_key = extractor.casefold()
    platform = str(record.get("site") or record.get("platform") or "").strip()
    platform_key = platform.casefold()
    source_kind = (
        "rss"
        if "rss" in extractor_key
        else "api"
        if "api" in extractor_key
        else "page_text"
        if record.get("candidate_generator") == "social_analyzer"
        else "page_or_extractor"
    )
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def first_timestamp(keys: tuple[str, ...]) -> tuple[str, Any, str] | None:
        for key in keys:
            raw_value = record.get(key)
            timestamp = _normalise_timestamp(raw_value)
            if timestamp:
                return key, raw_value, timestamp
        return None

    def add_event(
        *,
        event_type: str,
        label: str,
        semantic: str,
        category: str,
        source: tuple[str, Any, str] | None,
        confidence: str,
        include_in_timeline: bool = True,
        note: str | None = None,
    ) -> None:
        if source is None:
            return
        key, raw_value, timestamp = source
        dedupe = (event_type, timestamp)
        if dedupe in seen:
            return
        seen.add(dedupe)
        event: dict[str, Any] = {
            "type": event_type,
            "label": label,
            "semantic": semantic,
            "category": category,
            "timestamp": timestamp,
            "precision": _timestamp_precision(raw_value),
            "confidence": confidence,
            "include_in_timeline": include_in_timeline,
            "source_field": key,
            "source_kind": source_kind,
            "raw_value": str(raw_value),
        }
        if extractor:
            event["extractor"] = extractor
        if note:
            event["note"] = note
        events.append(event)

    created = first_timestamp(
        (
            "created_at",
            "creationDate",
            "creation_date",
            "account_created",
            "joined",
            "joined_at",
            "registered_at",
            "signup_date",
        )
    )
    add_event(
        event_type="registered",
        label="Account registered",
        semantic="account_creation",
        category="account",
        source=created,
        confidence="high" if source_kind == "api" else "medium",
    )

    activity = first_timestamp(
        ("latest_activity_at", "last_seen", "lastSeen", "last_activity_at", "last_activity")
    )
    if activity is not None:
        if extractor_key == "medium rss":
            add_event(
                event_type="feed_generated",
                label="RSS feed generated",
                semantic="feed_generation",
                category="observation",
                source=activity,
                confidence="low",
                include_in_timeline=False,
                note=(
                    "Medium exposes this value as RSS lastBuildDate. It can represent feed "
                    "generation time rather than user activity, so it is excluded from the "
                    "combined activity timeline."
                ),
            )
        elif extractor_key == "chess.com api":
            add_event(
                event_type="last_seen",
                label="Last online",
                semantic="platform_last_online",
                category="activity",
                source=activity,
                confidence="high",
            )
        elif extractor_key == "hive blog":
            add_event(
                event_type="public_activity",
                label="Latest public activity",
                semantic="latest_public_activity",
                category="activity",
                source=activity,
                confidence="high",
            )
        elif platform_key in {"ok.ru", "odnoklassniki"}:
            add_event(
                event_type="last_seen",
                label="Last visit",
                semantic="platform_last_visit",
                category="activity",
                source=activity,
                confidence="medium" if source_kind == "page_text" else "high",
            )
        elif extractor_key == "ucoz-like profile page":
            add_event(
                event_type="last_seen",
                label="Last seen",
                semantic="platform_last_seen",
                category="activity",
                source=activity,
                confidence="medium",
            )
        else:
            add_event(
                event_type="public_activity",
                label="Latest activity",
                semantic="latest_activity",
                category="activity",
                source=activity,
                confidence="medium",
            )

    updated = first_timestamp(("updated_at", "picture_edit_date", "profile_updated_at", "last_modified"))
    if updated is not None:
        source_field, _, updated_timestamp = updated
        created_timestamp = created[2] if created is not None else None
        same_as_creation = created_timestamp == updated_timestamp
        is_picture_update = source_field == "picture_edit_date"
        soundcloud_record = platform_key == "soundcloud" and source_field == "last_modified"
        add_event(
            event_type="profile_picture_updated" if is_picture_update else "profile_updated",
            label=(
                "Profile picture updated"
                if is_picture_update
                else "Profile record modified"
                if soundcloud_record
                else "Profile updated"
            ),
            semantic="profile_picture_change" if is_picture_update else "profile_record_update",
            category="profile",
            source=updated,
            confidence="high" if source_kind == "api" else "medium",
            include_in_timeline=not same_as_creation,
            note=(
                "The update timestamp is identical to the account creation timestamp and is "
                "treated as initial record creation rather than a later profile change."
                if same_as_creation
                else "SoundCloud exposes this as last_modified for the public user record; it is not asserted as user activity."
                if soundcloud_record
                else None
            ),
        )

    tracker_updated = first_timestamp(("tracker_updated_at",))
    if tracker_updated is not None:
        add_event(
            event_type="source_updated",
            label="TwitchTracker updated",
            semantic="source_refresh",
            category="observation",
            source=tracker_updated,
            confidence="medium",
            include_in_timeline=False,
            note="This is the TwitchTracker page/data refresh time, not a Twitch user activity timestamp.",
        )
    return events


def _social_analyzer_profile_fields(record: dict[str, Any]) -> dict[str, Any]:
    title = str(record.get("social_analyzer_title") or "").strip()
    text = str(record.get("social_analyzer_text") or "")
    raw_url = _http_url(record.get("url"))
    host = (urlsplit(raw_url).hostname or "").removeprefix("www.").casefold() if raw_url else ""
    extracted: dict[str, Any] = {}

    if host == "github.com":
        match = re.match(r"^(?P<username>[^\s(]+)\s+\((?P<name>[^)]+)\)\s+·\s+GitHub$", title)
        if match:
            extracted["username"] = match.group("username")
            extracted["display_name"] = match.group("name")
        followers = re.search(r"([\d,]+)\s+followers\b", text, re.I)
        if followers:
            extracted["follower_count"] = followers.group(1)
    elif host.endswith("pinterest.com"):
        match = re.match(r"^(?P<name>.+?)\s+\((?P<username>[^)]+)\)\s+-\s+Profile\s+\|\s+Pinterest$", title)
        if match:
            extracted["display_name"] = match.group("name")
            extracted["username"] = match.group("username")
        pins = re.search(r"([\d,]+)\s+Pins\b", text, re.I)
        if pins:
            extracted["posts_count"] = pins.group(1)
    elif host == "ok.ru":
        match = re.match(r"^(?P<name>.+?)\s+\|\s+OK\.RU$", title)
        if match:
            extracted["display_name"] = match.group("name")
        last_seen = re.search(r"Последний визит:\s*(.+?)\s+Фотографии", text, re.I)
        if last_seen:
            extracted["latest_activity_at"] = last_seen.group(1).strip()
    elif host.endswith("xvideos.com"):
        match = re.match(r"^(?P<name>.+?)\s+-\s+Profile page", title, re.I)
        if match:
            extracted["display_name"] = match.group("name")
        patterns = {
            "gender": r"Gender:\s*([^:]+?)\s+Age:",
            "age": r"Age:\s*(\d+)\s+years old",
            "country": r"Country:\s*(.+?)\s+Profile hits:",
            "city": r"City:\s*(.+?)\s+Signed up:",
            "created_at": r"Signed up:\s*(.+?)(?:\s+Remove ads|$)",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, text, re.I)
            if match:
                extracted[key] = match.group(1).strip()
    elif "discourse" in host:
        match = re.match(r"^Profile\s+-\s+(.+?)\s+-", title, re.I)
        if match:
            extracted["display_name"] = match.group(1).strip()
            extracted["username"] = match.group(1).strip()
    return extracted


def _platform_specific_record(record: dict[str, Any], query: str) -> dict[str, Any]:
    item = dict(record)
    item.update({key: value for key, value in _social_analyzer_profile_fields(item).items() if value not in (None, "")})
    platform = str(item.get("site") or item.get("platform") or "").strip().casefold()
    raw_url = _http_url(item.get("url") or item.get("profile_url"))
    if item.get("candidate_generator") == "social_analyzer" and raw_url:
        host = (urlsplit(raw_url).hostname or "").removeprefix("www.").casefold()
        social_platforms = {
            "github.com": "GitHub",
            "ok.ru": "OK.RU",
            "pinterest.com": "Pinterest",
            "xvideos.com": "XVideos",
            "cnblogs.com": "CnBlogs",
            "blog.naver.com": "Naver Blog",
            "discourse.mozilla.org": "Mozilla Discourse",
            "wordpress.com": "WordPress.com",
        }
        social_platform = social_platforms.get(host)
        if social_platform:
            item["site"] = social_platform
            platform = social_platform.casefold()

    def promote(target: str, *sources: str) -> None:
        if item.get(target) not in (None, ""):
            return
        value = _first(item, *sources)
        if value not in (None, ""):
            item[target] = value

    if platform == "stackoverflow":
        exact = _http_url(item.get("link"))
        if exact:
            if raw_url and raw_url != exact:
                item["evidence_url"] = raw_url
            item["url"] = exact
        if item.get("account_id") not in (None, ""):
            item["network_account_id"] = item.pop("account_id")
        promote("platform_id", "uid")
        promote("avatar_url", "gravatar_url", "image")
    elif platform == "youtube":
        exact = _http_url(item.get("channel_url"))
        if exact:
            if raw_url and raw_url != exact:
                item["evidence_url"] = raw_url
            item["url"] = exact
        promote("platform_id", "youtube_channel_id")
    elif platform == "tiktok":
        if raw_url and "/oembed" in raw_url:
            item["evidence_url"] = raw_url
            item["url"] = f"https://www.tiktok.com/@{query}"
        promote("platform_id", "tiktok_id")
        promote("username", "tiktok_username")
        promote("is_private", "is_secret")
    elif platform == "twitch":
        promote("platform_id", "twitch_channel_id")
        promote("username", "twitch_username")
    elif platform == "soundcloud":
        promote("platform_id", "id", "uid")
        promote("display_name", "full_name", "fullname", "name")
        promote("avatar_url", "avatar_url", "image")
        promote("followers", "followers_count", "follower_count")
        promote("following", "followings_count", "following_count")
        promote("biography", "description")
    elif platform == "duolingo":
        if raw_url and "/2017-06-30/users" in raw_url:
            item["evidence_url"] = raw_url
            item["url"] = f"https://www.duolingo.com/profile/{query}"
        promote("platform_id", "id", "uid")
    elif platform == "freelancer.com":
        if raw_url and urlsplit(raw_url).path.startswith("/api/"):
            if item.get("evidence_url") in (None, ""):
                item["evidence_url"] = raw_url
            name = item.get("username")
            handle = name.strip() if isinstance(name, str) and name.strip() else query
            item["url"] = f"https://www.freelancer.com/u/{quote(handle, safe='')}"
    elif platform == "chess":
        promote("platform_id", "chess_user_id")
    elif platform == "substack":
        promote("avatar_url", "image_cdn", "image")
    elif platform == "periscope":
        promote("twitter_username", "twitter_screen_name")
    elif platform == "bluesky":
        promote("platform_id", "uid")
        promote("banner_url", "image_bg")
    elif platform == "instagram":
        promote("platform_id", "id")
    elif platform == "buzzfeed":
        promote("platform_id", "uuid", "id")
    elif platform == "roblox":
        promote("platform_id", "roblox_user_id", "id", "uid")
        promote("display_name", "roblox_display_name", "display_name")
        promote("username", "roblox_username", "username")
        promote("verified", "roblox_verified", "verified")

    promote("avatar_url", "imgur_profile_avatar_url")
    if not item.get("location"):
        location_parts = [
            str(item[key]).strip()
            for key in ("city", "country", "profileCountry", "country_code")
            if item.get(key)
        ]
        if location_parts:
            item["location"] = ", ".join(dict.fromkeys(location_parts))
    events = _timeline_events(item)
    if events:
        item["timeline_events"] = events
    return item


def _language_label(value: Any) -> str | None:
    if value in (None, ""):
        return None
    code = str(value).strip()
    base = code.casefold().split("-", 1)[0]
    name = LANGUAGE_NAMES.get(base)
    return f"{name} ({code})" if name else code


def platform_details(record: dict[str, Any]) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(label: str, value: Any) -> None:
        if value in (None, "", [], {}):
            return
        if isinstance(value, bool):
            text = "Yes" if value else "No"
        elif isinstance(value, int | float):
            text = f"{value:,}"
        else:
            text = str(value).strip()
        if not text or label in seen:
            return
        seen.add(label)
        details.append({"label": label, "value": text[:1000]})

    created = _first(
        record,
        "created_at",
        "creationDate",
        "creation_date",
        "account_created",
        "joined",
        "joined_at",
        "registered_at",
        "signup_date",
    )
    add("Account created", _display_timestamp(created))
    # An RSS extractor's "latest activity" is the moment the feed was generated, not anything
    # the account did: on 2026-11-13 the same Medium account reported 13:41 in one run and
    # 14:17 in the next. The timeline already treats it that way; label it the same here.
    add(
        "Feed generated" if "rss" in str(record.get("_extractor") or "").casefold() else "Last activity",
        _display_timestamp(
            _first(record, "latest_activity_at", "last_seen", "lastSeen", "last_activity_at", "last_activity")
        ),
    )
    add("Profile record modified", _display_timestamp(_first(record, "last_modified", "profile_updated_at")))
    add("Source data updated", _first(record, "tracker_updated_at"))
    account_identifier = _first(
        record,
        "platform_id",
        "id",
        "uid",
        "user_id",
        "account_id",
        "twitch_channel_id",
        "youtube_channel_id",
        "weibo_id",
        "author_id",
    )
    add("Account ID", str(account_identifier) if account_identifier is not None else None)
    add("Network account ID", _first(record, "network_account_id"))
    add("Secondary ID", _first(record, "facebook_uid", "sec_uid"))
    add("Twitter ID", _first(record, "twitter_uid"))
    add("Birth date", _first(record, "birthday", "birth_date", "date_of_birth"))
    add("Gender", _first(record, "gender", "sex"))
    add("Age", _first(record, "age"))
    add("Posts", _first(record, "posts", "post_count", "posts_count", "media_count", "statuses_count"))
    add("Tracks", _first(record, "track_count", "tracks_count"))
    add("Playlists", _first(record, "playlist_count", "playlists_count"))
    add("Reposts", _first(record, "reposts_count", "repost_count"))
    add("Videos", _first(record, "video_count", "videos_count", "videos"))
    add("Photos", _first(record, "photos_count", "photos"))
    add("Cloudcasts", _first(record, "cloudcast_count"))
    add("Likes / favourites", _first(record, "favourites_count", "favorites_count", "favorite_count", "likes", "likes_count", "heart_count", "hearts_count"))
    add("Listens", _first(record, "listen_count"))
    add("Diggs", _first(record, "digg_count"))
    add("Reputation", _first(record, "reputation", "reputation_count", "karma"))
    add("Reputation level", _first(record, "reputation_label", "reputation_name"))
    add("Ranking", _first(record, "ranking", "rank_position"))
    add("Views", _first(record, "views_count", "all_views_count", "profile_views", "profile_hits"))
    add("Appreciations", _first(record, "appreciations"))
    add("Upvotes", _first(record, "upvotes_count"))
    add("Comments", _first(record, "comments_count"))
    add("Playlist likes", _first(record, "playlist_likes_count"))
    add("Forums", _first(record, "forums_count"))
    add("Forums followed", _first(record, "forums_following_count"))
    add("Public repositories", _first(record, "public_repos_count", "public_repos"))
    add("Public gists", _first(record, "public_gists_count", "public_gists"))
    add("Private account", _boolean(_first(record, "is_private", "private", "is_secret")))
    add("Protected account", _boolean(_first(record, "is_protected", "protected")))
    add("Business account", _boolean(_first(record, "is_business", "business")))
    add("Pro account", _boolean(_first(record, "is_pro")))
    add("Premium account", _boolean(_first(record, "is_premium", "is_paid")))
    add("Partner account", _boolean(_first(record, "is_partner")))
    add("Community account", _boolean(_first(record, "is_community_user", "is_community")))
    add("Power contributor", _boolean(_first(record, "is_power_contributor")))
    add("Anonymous account", _boolean(_first(record, "is_anonymous")))
    add("Deleted account", _boolean(_first(record, "is_deleted", "deleted")))
    add("Family-safe", _boolean(_first(record, "is_family_safe")))
    add("Streamer", _boolean(_first(record, "is_streamer")))
    add("Suspended account", _boolean(_first(record, "is_suspended")))
    add("Personal account", _boolean(_first(record, "is_personal")))
    add("Profile indexed", _boolean(_first(record, "is_indexed")))
    add("Profile active", _boolean(_first(record, "isProfileStatusActive", "is_profile_active")))
    add("Verified merchant", _boolean(_first(record, "is_verified_merchant")))
    add("Website verified", _boolean(_first(record, "is_website_verified")))
    add("Twitter verified", _boolean(_first(record, "is_twitter_verified")))
    add("Employee account", _boolean(_first(record, "is_employee")))
    add("Beta account", _boolean(_first(record, "is_beta_user")))
    add("Looking for work", _boolean(_first(record, "is_looking_for_job")))
    add("Working remotely", _boolean(_first(record, "is_working_remotely")))
    add("Recently joined", _boolean(_first(record, "is_joined_recently", "is_new_to_instagram")))
    add("Email verified", _boolean(_first(record, "emailVerified", "email_verified")))
    add("Phone number linked", _boolean(_first(record, "hasPhoneNumber", "has_phone_number")))
    add("Profile country", _first(record, "profileCountry", "profile_country", "country"))
    add("Country code", _first(record, "country_code"))
    add("Profile city", _first(record, "city"))
    add("Languages", _first(record, "languages", "language"))
    add("Timezone", _first(record, "timezone", "time_zone"))
    add("Account state", _first(record, "state", "account_state"))
    add("Website", _first(record, "website", "website_url", "blog_url", "external_url"))
    add("Reported email", record.get("reported_email"))
    add("Reported phone", record.get("reported_phone"))
    add("Tagline", _first(record, "tagline"))
    add("Company / employer", _first(record, "company", "employer", "organization", "organisation"))
    add("Role", _first(record, "role"))
    add("Profile type", _first(record, "profile_type", "type"))
    add("Primary currency", _first(record, "primaryCurrencyCode", "primary_currency"))
    add("GitHub username", _first(record, "github_username"))
    add("Twitter username", _first(record, "twitter_username", "twitter_screen_name"))
    add("Known usernames", _first(record, "known_usernames"))
    add("MyAnimeList username", _first(record, "mal_username"))
    add("Picsart username", _first(record, "picsart_username"))
    add("Imgur username", _first(record, "imgur_username"))
    add("Periscope username", _first(record, "periscope_username"))
    add("OK username", _first(record, "ok_user_name_id"))
    add("Nickname", _first(record, "nickname"))
    add("MyAnimeList ID", _first(record, "mal_uid"))
    add("Picsart ID", _first(record, "picsart_id"))
    add("OK ID", _first(record, "ok_id"))
    add("Organisation ID", _first(record, "organization_uuid"))
    add("Store ID", _first(record, "store_id"))
    add("Author ID", _first(record, "author_id"))
    add("Author name", _first(record, "author_name"))
    add("Alternative full name", _first(record, "alternative_fullname"))
    add("Address", _first(record, "address"))
    add("Weibo ID", _first(record, "weibo_id"))
    add("YouTube channel ID", _first(record, "youtube_channel_id"))
    add("Twitch channel ID", _first(record, "twitch_channel_id"))
    add("Owner ID", _first(record, "owner_uuid"))
    add("Paragraph user ID", _first(record, "paragraph_user_id"))
    add("Payer ID", _first(record, "payerId"))
    add("Wikimapia ID", _first(record, "wikimapia_uid"))
    add("Wallet address", _first(record, "wallet_address"))
    add("Snapcode image", _first(record, "snapcode_image"))
    add("Gravatar username", _first(record, "gravatar_username"))
    add("Gravatar hash", _first(record, "gravatar_email_md5_hash"))
    add("Streak", _first(record, "streak", "current_streak"))
    add("Total XP", _first(record, "totalXp", "total_xp", "xp"))
    add("Duolingo Plus", _boolean(_first(record, "hasPlus", "has_plus")))
    add("Facebook linked", _boolean(_first(record, "hasFacebookId")))
    add("Facebook profile identifier", _first(record, "facebook_username"))
    add("Google linked", _boolean(_first(record, "hasGoogleId")))
    add("Recent Duolingo activity", _boolean(_first(record, "hasRecentActivity15")))
    add("Moderation tools", _boolean(_first(record, "canUseModerationTools")))
    add("SSO required", _boolean(_first(record, "requiresSsoLogin")))
    add("Beta status", _first(record, "betaStatus"))
    add("Current course ID", _first(record, "currentCourseId"))
    add("Roles", _first(record, "roles"))
    add("Motivation", _first(record, "motivation", "acquisitionSurveyReason"))
    add("Keywords", _first(record, "keywords"))
    add("External links", _first(record, "social_links", "external_links"))

    from_language = _language_label(_first(record, "fromLanguage", "from_language"))
    learning_language = _language_label(
        _first(record, "learningLanguage", "learning_language", "target_language")
    )
    if from_language or learning_language:
        add(
            "Current course",
            " → ".join(value for value in (from_language, learning_language) if value),
        )

    courses = record.get("courses")
    if isinstance(courses, list):
        course_labels: list[str] = []
        for course in courses[:20]:
            if isinstance(course, str):
                if course.strip():
                    course_labels.append(course.strip())
                continue
            if not isinstance(course, dict):
                continue
            source = _language_label(course.get("fromLanguage"))
            target = (
                _language_label(course.get("learningLanguage"))
                or str(course.get("title") or "").strip()
            )
            label = " → ".join(value for value in (source, target) if value)
            xp = _integer(course.get("xp"))
            if xp is not None:
                label = f"{label} · {xp:,} XP" if label else f"{xp:,} XP"
            if label:
                course_labels.append(label)
        if course_labels:
            add("Courses", "; ".join(course_labels))
    return details


_MAIGRET_GENERIC_FIELDS = {
    "site",
    "url",
    "status",
    "username",
    "tags",
    "http_status",
    "rank",
    "_extractor",
    "session_loaded",
    "authenticated_session",
    "maigret_upstream_status",
}


_GENERIC_LABEL_WORDS = frozenset(
    {
        "log", "in", "login", "sign", "up", "signup", "profile", "home",
        "welcome", "page", "user", "account", "on",
    }
)


def _fold_name(text: str) -> str:
    return "".join(re.findall(r"[^\W_]+", text)).casefold()


def _is_site_generic_label(value: str, site: str) -> bool:
    """True when a value is only the site's own name plus generic page words."""
    tokens = re.findall(r"[^\W_]+", value.casefold())
    if not tokens:
        return False
    site_tokens = set(re.findall(r"[^\W_]+", str(site or "").casefold()))
    return all(token in site_tokens or token in _GENERIC_LABEL_WORDS for token in tokens)


def _maigret_account_evidence_fields(record: dict[str, Any]) -> tuple[str, ...]:
    """Return account-specific fields that materially strengthen a Maigret claim."""
    useful: list[str] = []
    for key, value in record.items():
        if key in _MAIGRET_GENERIC_FIELDS or key.startswith("maigret_"):
            continue
        if value in (None, "", [], {}):
            continue
        folded = key.casefold()
        if folded in {"image", "avatar_url"} and _is_placeholder_avatar(value):
            continue
        if (
            folded in {
                "id",
                "uid",
                "name",
                "fullname",
                "bio",
                "description",
                "image",
                "avatar_url",
                "location",
                "city",
                "country",
                "country_code",
                "created_at",
                "latest_activity_at",
                "last_seen",
                "last_modified",
                "website",
                "blog_url",
                "external_url",
            }
            or folded.endswith("_id")
            or folded.endswith("_count")
            or folded.startswith("is_")
            or folded in {"payerid", "primarycurrencycode", "company", "role", "tagline"}
        ):
            useful.append(key)
    return tuple(sorted(useful))


BLACKBIRD_METADATA_TARGETS = {
    "avatar": "avatar_url",
    "profile image": "avatar_url",
    "profile picture": "avatar_url",
    "image": "avatar_url",
    "full name": "display_name",
    "fullname": "display_name",
    "display name": "display_name",
    "name": "display_name",
    "biography": "biography",
    "bio": "biography",
    "about": "biography",
    "location": "location",
    "city": "location",
    "country": "location",
    "username": "username",
    "account id": "platform_id",
    "user id": "platform_id",
    "verified": "verified",
    "followers": "followers",
    "follower count": "followers",
    "following": "following",
    "following count": "following",
    "courses": "courses",
    "joined": "created_at",
    "creation date": "created_at",
    "created": "created_at",
    "last seen": "latest_activity_at",
    "last activity": "latest_activity_at",
    "email verified": "email_verified",
    "has phone number": "has_phone_number",
    "phone number linked": "has_phone_number",
    "posts": "posts_count",
    "post count": "posts_count",
    "videos": "video_count",
    "video count": "video_count",
    "reputation": "reputation",
}

_NO_VALUE_TEXT = frozenset({"n/a", "na", "none", "null", "unknown", "-", "--", "undefined"})
_BLACKBIRD_INTEGER_TARGETS = frozenset({"followers", "following", "posts_count", "video_count", "reputation"})
_BLACKBIRD_BOOLEAN_TARGETS = frozenset({"verified", "email_verified", "has_phone_number"})
_BLACKBIRD_TIMESTAMP_TARGETS = frozenset({"created_at", "latest_activity_at"})


def blackbird_metadata_value_is_evidence(label: str, value: Any, site: str = "") -> bool:
    """True when a Blackbird metadata label carries a usable, meaningful value."""
    target = BLACKBIRD_METADATA_TARGETS.get(str(label).strip().casefold())
    if target is None:
        return False
    if target == "avatar_url":
        candidate = str(value).strip() if isinstance(value, str) else ""
        return candidate.startswith(("http://", "https://")) and not _is_placeholder_avatar(candidate)
    if target in _BLACKBIRD_INTEGER_TARGETS:
        return _integer(value) is not None
    if target in _BLACKBIRD_BOOLEAN_TARGETS:
        return _boolean(value) is not None
    if target in _BLACKBIRD_TIMESTAMP_TARGETS:
        return _normalise_timestamp(value) is not None
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float) and target == "platform_id":
        return True
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or text.casefold() in _NO_VALUE_TEXT:
        return False
    return not (target == "display_name" and _is_site_generic_label(text, site))


def _blackbird_metadata_evidence(value: Any, site: str = "") -> tuple[dict[str, Any], tuple[str, ...]]:
    if not isinstance(value, list):
        return {}, ()
    extracted: dict[str, Any] = {}
    evidence_fields: list[str] = []
    # Retain useful details without making them independent existence evidence.
    # Contact values may be masked hints; never turn them into complete identifiers.
    detail_aliases = {
        "email": "reported_email",
        "email address": "reported_email",
        "public email": "reported_email",
        "phone": "reported_phone",
        "phone number": "reported_phone",
        "website": "website",
        "website url": "website",
        "company": "company",
        "employer": "company",
        "occupation": "role",
        "role": "role",
    }
    for entry in value:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("name") or entry.get("type") or "").strip()
        raw_value = entry.get("value")
        if not label or raw_value in (None, ""):
            continue
        detail_target = detail_aliases.get(label.casefold())
        if detail_target:
            if isinstance(raw_value, str) and raw_value.strip():
                extracted.setdefault(detail_target, raw_value.strip())
            continue
        target = BLACKBIRD_METADATA_TARGETS.get(label.casefold())
        if not target or not blackbird_metadata_value_is_evidence(label, raw_value, site):
            continue
        evidence_fields.append(label)
        extracted[target] = raw_value.strip() if isinstance(raw_value, str) else raw_value
    return extracted, tuple(dict.fromkeys(evidence_fields))


def _text_value(value: Any) -> str | None:
    """Coerce a scalar to text: str stays, int/float (not bool) becomes str, anything else is None."""
    if isinstance(value, str):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return None


def _profile_evidence_with_notes(record: dict[str, Any]) -> tuple[ProfileEvidence, dict[str, Any]]:
    """Build profile evidence and report what was rejected or set aside.

    The notes may hold ``rejected_fields``, ``avatar_placeholder_url`` and ``page_description``.
    """
    rejected: list[dict[str, str]] = []
    notes: dict[str, Any] = {}

    def reject(field: str, value: Any) -> None:
        rejected.append({"field": field, "reason": f"expected text, got {type(value).__name__}"})

    def single(field: str, *keys: str) -> str | None:
        value = _first(record, *keys)
        if value is None:
            return None
        text = _text_value(value)
        if text is None:
            reject(field, value)
        return text

    avatar = single(
        "avatar_url",
        "avatar_url",
        "avatar",
        "profile_image_url",
        "profile_pic_url",
        "picture",
        "image",
        "photo",
        "imgur_profile_avatar_url",
    )
    avatar = _normalise_image_url(avatar)
    if _is_placeholder_avatar(avatar):
        notes["avatar_placeholder_url"] = avatar
        avatar = None

    biography: str | None = None
    for key in ("biography", "bio", "about", "description", "tagline"):
        value = record.get(key)
        if value in (None, ""):
            continue
        text = _text_value(value)
        if text is None:
            reject("biography", value)
            continue
        if _is_boilerplate_biography(text):
            notes.setdefault("page_description", text)
            continue
        biography = text
        break

    location: str | None = None
    for key in ("location", "profileCountry", "profile_country", "city", "country", "country_code"):
        value = record.get(key)
        if value in (None, ""):
            continue
        text = _text_value(value)
        if text is None:
            reject("location", value)
            continue
        if re.fullmatch(r"[A-Za-z]{2}", text.strip()):
            continue
        location = text
        break

    profile = ProfileEvidence(
        avatar_url=avatar,
        display_name=single("display_name", "display_name", "fullname", "full_name", "name", "nickname"),
        biography=biography,
        location=location,
        account_id=single(
            "account_id",
            "account_id",
            "username",
            "user_name",
            "login",
            "handle",
            "screen_name",
            "user_id",
            "uid",
            "id",
        ),
        verified=_boolean(_first(record, "verified", "is_verified")),
        followers=_integer(_first(record, "followers", "followers_count", "follower_count")),
        following=_integer(_first(record, "following", "following_count")),
    )
    if rejected:
        notes["rejected_fields"] = rejected
    return profile, notes


def _profile_evidence(record: dict[str, Any]) -> ProfileEvidence:
    return _profile_evidence_with_notes(record)[0]


def _unparsed_record_finding(
    tool: str,
    record: Any,
    index: int,
    query: str,
    query_type: QueryType,
    raw: str | None,
    error: Exception,
) -> FindingRecord:
    platform: str | None = None
    if isinstance(record, dict):
        for key in ("site", "platform"):
            if isinstance(record.get(key), str) and record[key]:
                platform = record[key]
                break
    raw_record: Any
    try:
        raw_record = json.loads(json.dumps(record, default=str))
        serialised = json.dumps(raw_record)
        if len(serialised) > 4000:
            raw_record = serialised[:4000]
    except (TypeError, ValueError, RecursionError):
        raw_record = repr(record)[:4000]
    return FindingRecord(
        query_type=query_type,
        queried_value=query,
        finding_type="unparsed_record",
        platform=platform,
        value=query,
        status=FindingStatus.ERROR,
        confidence=0.0,
        metadata={"parse_error": str(error)[:300], "raw_record": raw_record},
        source_tool=tool,
        source_record_id=str(index),
        raw_output_reference=raw,
    )


def _normalise(
    tool: str, records: list[dict[str, Any]], query: str, query_type: QueryType, raw: str | None
) -> list[FindingRecord]:
    findings = []
    for index, record in enumerate(records):
        try:
            item = _platform_specific_record(record, query)
            url = item.get("url") or item.get("profile_url")
            platform = item.get("site") or item.get("platform")
            details = platform_details(item)
            if details:
                item["platform_details"] = details
            if not platform and url:
                platform = (urlsplit(str(url)).hostname or "").removeprefix("www.")
            status = _status(item.get("status", item.get("found", item.get("exists", "unknown"))))
            profile, notes = _profile_evidence_with_notes(item)
            if "avatar_placeholder_url" in notes:
                item["avatar_placeholder_url"] = notes["avatar_placeholder_url"]
            if "page_description" in notes and item.get("page_description") in (None, ""):
                item["page_description"] = notes["page_description"]
            if notes.get("rejected_fields"):
                item["rejected_fields"] = notes["rejected_fields"]
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="profile"
                    if query_type != QueryType.DOMAIN
                    else str(record.get("type", "host")),
                    platform=str(platform) if platform else None,
                    value=str(item.get("value", query)),
                    canonical_url=canonicalise_url(str(url)) if url else None,
                    display_url=str(url) if url else None,
                    status=status,
                    confidence=None,
                    title=item.get("title"),
                    metadata=item,
                    profile=profile,
                    source_tool=tool,
                    source_record_id=str(item.get("id", index)),
                    raw_output_reference=raw,
                )
            )
        except (ValidationError, TypeError, ValueError, KeyError) as exc:
            findings.append(_unparsed_record_finding(tool, record, index, query, query_type, raw, exc))
    return findings


class MaigretAdapter(Adapter):
    tool_id, display_name, executable = "maigret", "Maigret", "maigret"
    capabilities = frozenset({"username_search", "json_output"})
    local_session_format = "netscape_cookie_jar"
    supported_query_types = frozenset({QueryType.USERNAME})
    default_timeout, concurrency_limit = 240, 1

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        command = [
            self.executable,
            query,
            "--json",
            "simple",
            "--folderoutput",
            str(output_dir),
            "--db",
            str(output_dir / "maigret-data.json"),
            "--timeout",
            str(min(timeout, 15)),
            "--no-recursion",
            "--no-progressbar",
            "--no-autoupdate",
        ]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        paths = sorted(output_dir.glob("report_*_simple.json"))
        if not paths:
            paths = sorted(
                path for path in output_dir.glob("*.json") if path.name != "maigret-data.json"
            )
        if not paths:
            return []
        # Maigret may emit additional small reports for identifiers extracted
        # during a search. The primary username report contains the complete
        # site result set and is consistently the largest simple JSON file.
        report = max(paths, key=lambda path: path.stat().st_size)
        data = json.loads(report.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        enrichment_path = output_dir / "maigret-public-enrichment.json"
        enrichment: dict[str, Any] = {}
        if enrichment_path.is_file():
            candidate = json.loads(enrichment_path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                enrichment = candidate
        records = []
        for site_name, value in data.items():
            if not isinstance(value, dict):
                continue
            status_payload = value.get("status")
            if isinstance(status_payload, dict):
                record = {
                    "site": status_payload.get("site_name") or site_name,
                    "url": status_payload.get("url")
                    or value.get("url_user")
                    or value.get("url_probe"),
                    "status": status_payload.get("status", "unknown"),
                    "username": status_payload.get("username") or value.get("username"),
                    "tags": status_payload.get("tags", []),
                    "http_status": value.get("http_status"),
                    "rank": value.get("rank"),
                }
                extracted = status_payload.get("ids", {})
                if isinstance(extracted, dict):
                    record.update(extracted)
                supplemental = enrichment.get(site_name)
                if isinstance(supplemental, dict):
                    record.update(supplemental)
                    record["public_enrichment"] = site_name
                if result.session_loaded is not None:
                    record.setdefault("session_loaded", result.session_loaded)
                records.append(record)
            else:
                record = {**value, "site": site_name}
                if result.session_loaded is not None:
                    record.setdefault("session_loaded", result.session_loaded)
                records.append(record)
        return records

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        cleaned: list[dict[str, Any]] = []
        for record in records:
            item = dict(record)
            upstream_status = str(item.get("status", "unknown"))
            item["maigret_upstream_status"] = upstream_status
            if upstream_status.casefold() in {"claimed", "found", "exists", "true"}:
                discarded = sorted(
                    key
                    for key in ("fullname", "name", "display_name")
                    if isinstance(item.get(key), str)
                    and _fold_name(item[key]) != _fold_name(query)
                    and _is_site_generic_label(item[key], str(item.get("site") or ""))
                )
                for key in discarded:
                    item.pop(key, None)
                if discarded:
                    item["maigret_discarded_fields"] = discarded
                reported = item.get("username")
                reported_text = str(reported).strip() if reported else ""
                if reported_text and _fold_name(reported_text) != _fold_name(query):
                    # Maigret's extracted username replaced the queried one: a
                    # different account name is a lead, not a match.
                    item["status"] = "unknown"
                    item["maigret_unverified_claim"] = True
                    item["maigret_evidence_strength"] = "different_account_name"
                    item["maigret_reported_username"] = reported_text
                    cleaned.append(item)
                    continue
                evidence_fields = _maigret_account_evidence_fields(item)
                if evidence_fields:
                    # Maigret's site checks vary in quality, but account-specific
                    # extracted values (UIDs, avatars, dates, counts, names, etc.)
                    # are materially stronger than a bare existence heuristic.
                    # Preserve those records as positive leads and retain exactly
                    # which fields justified that treatment.
                    item["maigret_evidence_fields"] = list(evidence_fields)
                    item["maigret_evidence_strength"] = "extracted_account_metadata"
                else:
                    # A bare broad-site-checker claim remains a technical lead.
                    item["status"] = "unknown"
                    item["maigret_unverified_claim"] = True
                    item["maigret_evidence_strength"] = "bare_site_assertion"
            cleaned.append(item)
        findings = _normalise(self.tool_id, cleaned, query, query_type, raw_reference)
        for finding in findings:
            if finding.metadata.get("maigret_unverified_claim"):
                finding.confidence = 0.15
                finding.finding_type = "unverified_profile_lead"
            elif finding.metadata.get("maigret_evidence_fields"):
                finding.confidence = 0.70
                finding.finding_type = "profile"
        return findings

    def coverage_warning(self, result: ExecutionResult) -> str | None:
        if self.tool_id != "maigret":
            return Adapter.coverage_warning(self, result)
        text = f"{result.stdout or ''}\n{result.stderr or ''}"
        total = 0.0
        names: list[str] = []
        for name, pct in re.findall(r'Too many errors of type "([^"]+)" \((\d+(?:\.\d+)?)%\)', text):
            lowered = name.casefold()
            if any(word in lowered for word in ("connecting failure", "dns", "timeout")):
                total += float(pct)
                if name not in names:
                    names.append(name)
        if total < 20:
            return Adapter.coverage_warning(self, result)
        return (
            f"Maigret could not reach about {round(total)}% of the sites it checks "
            f"({', '.join(names)}). Accounts on those sites are not ruled out; check the "
            "network or DNS resolver (a resolver rate limit is a common cause) and run it again."
        )


class SherlockAdapter(MaigretAdapter):
    tool_id, display_name, executable = "sherlock", "Sherlock", "sherlock"
    capabilities = frozenset({"username_search", "csv_output"})
    default_timeout = 360

    def coverage_warning(self, result: ExecutionResult) -> str | None:
        base = Adapter.coverage_warning(self, result)
        if base:
            return base
        text = f"{result.stdout or ''}\n{result.stderr or ''}".casefold()
        markers = ("nameresolutionerror", "failed to resolve", "temporary failure in name resolution")
        if any(marker in text for marker in markers):
            return (
                "Sherlock reported a hostname lookup failure during its run, so the sites it "
                "reported nothing for may not have been checked. Accounts on those sites are not "
                "ruled out; check the network or DNS resolver and run it again."
            )
        return None

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        command = [
            self.executable,
            query,
            "--csv",
            "--folderoutput",
            str(output_dir),
            "--no-color",
            "--timeout",
            str(min(timeout, 15)),
            "--local",
        ]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        paths = sorted(output_dir.glob("*.csv"))
        if not paths:
            return []
        with paths[0].open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        return [
            {
                "site": r.get("name") or r.get("site_name"),
                "url": r.get("url_user") or r.get("url_main"),
                "status": r.get("exists") or r.get("status") or "unknown",
                "http_status": r.get("http_status"),
                "response_time_s": r.get("response_time_s"),
            }
            for r in rows
        ]

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        cleaned: list[dict[str, Any]] = []
        for record in records:
            item = dict(record)
            upstream_status = str(item.get("status", "unknown"))
            item["sherlock_upstream_status"] = upstream_status
            if upstream_status.casefold() in {"claimed", "found", "exists", "true"}:
                # Sherlock is a broad username-existence probe and can produce
                # substantial false positives. Treat an uncorroborated claim as
                # unknown until another collector confirms the same canonical URL.
                item["status"] = "unknown"
                item["sherlock_unverified_claim"] = True
            cleaned.append(item)
        findings = _normalise(self.tool_id, cleaned, query, query_type, raw_reference)
        for finding in findings:
            if finding.metadata.get("sherlock_unverified_claim"):
                finding.confidence = 0.15
                finding.finding_type = "unverified_profile_lead"
        return findings


class BlackbirdAdapter(MaigretAdapter):
    tool_id, display_name, executable = "blackbird", "Blackbird", "blackbird"
    capabilities = frozenset({"username_search", "email_search"})
    supported_query_types = frozenset({QueryType.USERNAME, QueryType.EMAIL})

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        cleaned: list[dict[str, Any]] = []
        for record in records:
            item = dict(record)
            item.pop("category", None)
            platform_name = item.pop("name", None)
            if not item.get("site") and not item.get("platform") and platform_name:
                item["site"] = platform_name
            upstream_status = str(item.get("status", "unknown"))
            profile_fields, evidence_fields = _blackbird_metadata_evidence(
                item.get("metadata"), str(item.get("site") or item.get("platform") or "")
            )
            item.update(profile_fields)
            item["blackbird_upstream_status"] = upstream_status
            if upstream_status.casefold() in {"claimed", "found", "exists", "true"}:
                if evidence_fields:
                    item["blackbird_evidence_fields"] = list(evidence_fields)
                    item["blackbird_evidence_strength"] = "extracted_metadata"
                elif item.get("blackbird_public_verification"):
                    item["blackbird_evidence_strength"] = "first_party_api_verification"
                else:
                    # A constructed URL or generic response is not proof that an
                    # account exists. Preserve it as a technical lead until the
                    # runner retained account-specific evidence or another source
                    # corroborates it.
                    item["status"] = "unknown"
                    item["blackbird_unverified_claim"] = True
                    item["blackbird_evidence_strength"] = "bare_url_assertion"
            cleaned.append(item)
        findings = _normalise(self.tool_id, cleaned, query, query_type, raw_reference)
        for finding in findings:
            if finding.metadata.get("blackbird_unverified_claim"):
                finding.confidence = 0.10
                finding.finding_type = "unverified_profile_lead"
            elif finding.metadata.get("blackbird_public_verification"):
                finding.confidence = 0.85
            elif finding.metadata.get("blackbird_evidence_fields"):
                finding.confidence = 0.80
        return findings

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        command = [
            self.executable,
            "--username" if query_type == QueryType.USERNAME else "--email",
            query,
            "--json",
            "--no-update",
            "--timeout",
            str(min(timeout, 15)),
        ]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        enrichment: dict[str, Any] = {}
        enrichment_path = output_dir / "blackbird-public-enrichment.json"
        if enrichment_path.is_file():
            candidate = json.loads(enrichment_path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                enrichment = candidate
        paths = sorted(
            path
            for path in output_dir.rglob("*.json")
            if path.name != "blackbird-public-enrichment.json"
        )
        if paths:
            data = json.loads(paths[-1].read_text(encoding="utf-8"))
            records: list[dict[str, Any]] = []
            if isinstance(data, list):
                records = [x for x in data if isinstance(x, dict)]
            elif isinstance(data, dict):
                values = data.get("results", data)
                if isinstance(values, list):
                    records = [x for x in values if isinstance(x, dict)]
                elif isinstance(values, dict):
                    records = [
                        {"site": key, **value}
                        for key, value in values.items()
                        if isinstance(value, dict)
                    ]
            for record in records:
                platform = str(
                    record.get("site") or record.get("platform") or record.get("name") or ""
                )
                supplemental = enrichment.get(platform)
                if isinstance(supplemental, dict):
                    record.update(supplemental)
            if records:
                return records
        records = []
        for line in result.stdout.splitlines():
            match = re.search(r"(?P<url>https?://\S+)", line)
            if match:
                records.append(
                    {
                        "url": match.group("url").rstrip(".,)]"),
                        "status": "found"
                        if any(x in line.lower() for x in ("found", "[+]", "exists"))
                        else "unknown",
                    }
                )
        return records


class NaminterAdapter(MaigretAdapter):
    tool_id, display_name, executable = "naminter", "Naminter", "naminter"
    capabilities = frozenset({"username_search", "json_output", "browser_impersonation"})
    supported_query_types = frozenset({QueryType.USERNAME})
    default_selected = False
    default_timeout, concurrency_limit = 300, 1

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        command = [
            self.executable,
            "--username",
            query,
            "--json",
            "--json-path",
            "naminter.json",
            "--local-data",
            "/opt/tool/wmn-data.json",
            "--skip-validation",
            "--no-color",
            "--no-progressbar",
            "--filter-all",
            "--impersonate",
            "chrome",
            "--max-tasks",
            "25",
            "--timeout",
            str(min(timeout, 15)),
        ]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "naminter.json"
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("results", "data", "items"):
                values = data.get(key)
                if isinstance(values, list):
                    return [item for item in values if isinstance(item, dict)]
            return [value for value in data.values() if isinstance(value, dict)]
        return []

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        cleaned: list[dict[str, Any]] = []
        for record in records:
            item = dict(record)
            item["site"] = item.get("site") or item.get("site_name") or item.get("name")
            # Naminter's "name" is the site's name; left in place it would be read as the
            # account's display name.
            item.pop("name", None)
            item["url"] = (
                item.get("url")
                or item.get("result_url")
                or item.get("profile_url")
                or item.get("uri_pretty")
                or item.get("uri_check")
            )
            item.pop("headers", None)
            upstream_status = str(
                item.get("status") or item.get("result_status") or item.get("result") or "unknown"
            )
            item["naminter_upstream_status"] = upstream_status
            status_key = upstream_status.casefold().replace("_", " ")
            item["status"] = {
                "exists": "found",
                "missing": "not found",
                "not valid": "unknown",
                "conflicting": "unknown",
            }.get(status_key, upstream_status)
            if status_key == "partial":
                # Only some detection criteria matched, which is what a soft-404
                # looks like; treat it like a bare claim.
                item["status"] = "unknown"
                item["naminter_unverified_claim"] = True
                item["naminter_partial_match"] = True
            if status_key in {"claimed", "found", "exists", "true"}:
                # Naminter is valuable here because its TLS/browser impersonation can
                # reach WMN sites that reject plain HTTP clients. A bare existence
                # result is still only a lead until another collector corroborates it.
                item["status"] = "unknown"
                item["naminter_unverified_claim"] = True
            cleaned.append(item)
        findings = _normalise(self.tool_id, cleaned, query, query_type, raw_reference)
        for finding in findings:
            if finding.metadata.get("naminter_unverified_claim"):
                finding.confidence = 0.20
                finding.finding_type = "unverified_profile_lead"
        return findings


class InstaloaderAdapter(MaigretAdapter):
    tool_id, display_name, executable = "instaloader", "Instaloader", "instaloader-osint"
    local_session_format = "json_context"
    capabilities = frozenset({"username_search", "instagram_profile", "profile_image"})
    supported_query_types = frozenset({QueryType.USERNAME})
    default_timeout, concurrency_limit = 90, 1

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        command = [self.executable, "--username", query]
        if proxy:
            command.extend(("--proxy", proxy))
        return command

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "profile.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return []
        avatar_file = data.get("avatar_file")
        if isinstance(avatar_file, str):
            candidate = output_dir / avatar_file
            if candidate.is_file():
                data["avatar_local_path"] = str(candidate)
                raw_root = output_dir.parent.parent
                if raw_root.name == "raw":
                    try:
                        local = load_local_image(
                            str(candidate),
                            data_dir=raw_root.parent,
                            max_bytes=10 * 1024 * 1024,
                        )
                    except LocalImageEvidenceError as exc:
                        data["avatar_error"] = str(exc)
                    else:
                        source_url = data.get("avatar_url")
                        data["image_cache"] = local.metadata(
                            source_url if isinstance(source_url, str) else None
                        )
        return [data]

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        findings = _normalise(self.tool_id, records, query, query_type, raw_reference)
        for finding in findings:
            if finding.finding_type == "unparsed_record":
                continue
            finding.finding_type = "instagram_profile"
            if finding.status == FindingStatus.FOUND:
                finding.confidence = 0.95
            elif finding.status == FindingStatus.UNKNOWN:
                finding.confidence = 0.25
        return findings


def _nexfil_proxy_arguments(proxy: str | None) -> list[str]:
    """Map a configured proxy URL onto Nexfil's separate host/port/protocol flags.

    Nexfil has no SOCKS support (-proto accepts only http/https), so a configured SOCKS/Tor
    proxy cannot be forwarded to it; it is left unset rather than passed incorrectly.
    """
    if not proxy:
        return []
    parsed = urlsplit(proxy)
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return []
    try:
        port = parsed.port
    except ValueError:
        return []
    if not port:
        return []
    return ["-proto", scheme, "-ph", parsed.hostname, "-pp", str(port)]


class NexfilAdapter(MaigretAdapter):
    tool_id, display_name, executable = "nexfil", "Nexfil", "nexfil"
    capabilities = frozenset({"username_search", "candidate_generation"})
    supported_query_types = frozenset({QueryType.USERNAME})
    default_timeout, concurrency_limit = 300, 1

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        command = [self.executable, "-u", query, "-t", str(min(timeout, 15))]
        command.extend(_nexfil_proxy_arguments(proxy))
        return command

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        text_parts = [result.stdout, result.stderr]
        for path in output_dir.rglob("*.txt"):
            if path.is_file() and path.stat().st_size <= 5 * 1024 * 1024:
                text_parts.append(path.read_text(encoding="utf-8", errors="replace"))
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        clean_text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", "\n".join(text_parts))
        for match in re.finditer(r"https?://[^\s<>\]\[\"']+", clean_text):
            url = match.group(0).rstrip(".,;:!?)}")
            canonical = canonicalise_url(url)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            host = (urlsplit(canonical).hostname or "").removeprefix("www.")
            records.append(
                {
                    "site": host or None,
                    "url": canonical,
                    "status": "unknown",
                    "candidate_generator": "nexfil",
                }
            )
        return records

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        findings = _normalise(self.tool_id, records, query, query_type, raw_reference)
        for finding in findings:
            if finding.finding_type == "unparsed_record":
                continue
            finding.status = FindingStatus.UNKNOWN
            finding.confidence = 0.10
            finding.finding_type = "unverified_profile_lead"
        return findings


class SocialAnalyzerAdapter(MaigretAdapter):
    tool_id, display_name, executable = (
        "social_analyzer",
        "Social Analyzer",
        "social-analyzer-osint",
    )
    capabilities = frozenset({"username_search", "candidate_generation", "metadata_extraction"})
    supported_query_types = frozenset({QueryType.USERNAME})
    default_timeout, concurrency_limit = 300, 1

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        return [self.executable, "--username", query]

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "results.json"
        if not path.exists():
            return []
        data: Any = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return []
        records: list[dict[str, Any]] = []
        seen: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if not isinstance(value, dict):
                return
            raw_url = _first(value, "link", "url", "profile_url", "profile")
            if isinstance(raw_url, str) and raw_url.startswith(("http://", "https://")):
                canonical = canonicalise_url(raw_url)
                if canonical and canonical not in seen:
                    seen.add(canonical)
                    host = (urlsplit(canonical).hostname or "").removeprefix("www.")
                    records.append(
                        {
                            "site": _first(value, "website", "site", "title", "name")
                            or host
                            or None,
                            "url": canonical,
                            "status": "unknown",
                            "candidate_generator": "social_analyzer",
                            "social_analyzer_rate": value.get("rate"),
                            "social_analyzer_title": value.get("title"),
                            "social_analyzer_text": value.get("text"),
                        }
                    )
            for item in value.values():
                if isinstance(item, dict | list):
                    visit(item)

        visit(data)
        return records

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        findings = _normalise(self.tool_id, records, query, query_type, raw_reference)
        for finding in findings:
            if finding.finding_type == "unparsed_record":
                continue
            finding.status = FindingStatus.UNKNOWN
            finding.confidence = 0.15
            finding.finding_type = "unverified_profile_lead"
        return findings


class HoleheAdapter(MaigretAdapter):
    tool_id, display_name, executable = "holehe", "Holehe", "holehe"
    capabilities = frozenset({"email_search", "account_recovery_endpoint"})
    supported_query_types = frozenset({QueryType.EMAIL})
    recovery_endpoint = True
    default_timeout, concurrency_limit = 180, 1

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        return [self.executable, query, "--only-used", "--no-color"]

    @staticmethod
    def detect_rate_limit(text: str) -> bool:
        return any(
            re.match(r"\s*\[x\]\s+[a-z0-9.-]+\.[a-z]{2,}\s*$", line, re.I)
            for line in text.splitlines()
        )

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        records = []
        for line in result.stdout.splitlines():
            match = re.match(r"\s*\[([+x!-])\]\s+([a-z0-9.-]+\.[a-z]{2,})\s*$", line, re.I)
            if not match:
                continue
            marker, site = match.groups()
            site = site.casefold()
            if site in {"email", "email.used", "email.not.used"}:
                continue
            records.append(
                {
                    "site": site,
                    "status": {
                        "+": "found",
                        "x": "rate_limited",
                        "!": "error",
                        "-": "unknown",
                    }[marker.lower()],
                    "value": site,
                }
            )
        return records

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        generic = {"email", "email used", "email not used"}
        cleaned = [
            record
            for record in records
            if str(record.get("site") or record.get("value") or "").strip().casefold()
            not in generic
        ]
        return _normalise(self.tool_id, cleaned, query, query_type, raw_reference)


class HarvesterAdapter(MaigretAdapter):
    tool_id, display_name, executable = "theharvester", "theHarvester", "theHarvester"
    capabilities = frozenset({"domain_discovery", "json_output"})
    supported_query_types = frozenset({QueryType.DOMAIN})
    default_timeout = 300

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        return [
            self.executable,
            "-d",
            query,
            "-b",
            "crtsh,duckduckgo",
            "-f",
            str(output_dir / "harvest"),
        ]

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "harvest.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        records = []
        mapping = {"emails": "email", "hosts": "host", "ips": "ip", "urls": "url", "names": "name"}
        for key, kind in mapping.items():
            for value in data.get(key, []) or []:
                item = value if isinstance(value, dict) else {"value": value}
                records.append(
                    {
                        "type": kind,
                        "status": "found",
                        "url": item.get("url") if isinstance(item, dict) else None,
                        **item,
                    }
                )
        return records

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        # theHarvester's records are passive-DNS/certificate-transparency hits, not
        # probabilistic username-existence claims, so the unverified-claim downgrade
        # MaigretAdapter applies to broad username probes does not apply here.
        return _normalise(self.tool_id, records, query, query_type, raw_reference)


MAX_SUBFINDER_FINDINGS = 500


class SubfinderAdapter(MaigretAdapter):
    tool_id, display_name, executable = "subfinder", "Subfinder", "subfinder"
    capabilities = frozenset({"passive_subdomain_discovery", "jsonl_output"})
    supported_query_types = frozenset({QueryType.DOMAIN})
    default_timeout, concurrency_limit = 180, 1
    version_args = ("-version",)

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        return [
            self.executable,
            "-d",
            query,
            "-oJ",
            "-cs",
            "-silent",
            "-timeout",
            str(max(5, min(timeout, 60))),
            "-o",
            str(output_dir / "subfinder.jsonl"),
        ]

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "subfinder.jsonl"
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            host = str(item.get("host") or item.get("name") or "").strip().removeprefix("*.")
            if not host:
                continue
            sources = item.get("sources") or item.get("source") or []
            if isinstance(sources, str):
                sources = [sources]
            records.append(
                {
                    "type": "subdomain",
                    "status": "found",
                    "value": host,
                    "host": host,
                    "input": item.get("input"),
                    "sources": [str(source) for source in sources if source],
                    "ip": item.get("ip"),
                }
            )
        return records

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        target = query.casefold().rstrip(".")
        candidates: list[tuple[int, str, dict[str, Any]]] = []
        seen: set[str] = set()
        for record in records:
            host = str(record.get("host") or record.get("value") or "").strip().rstrip(".")
            key = host.casefold()
            if not host or key in seen or (key != target and not key.endswith(f".{target}")):
                continue
            seen.add(key)
            sources = record.get("sources") or []
            source_count = len(sources) if isinstance(sources, list) else 0
            score = (source_count * 10) + (5 if record.get("ip") else 0)
            candidates.append((score, key, dict(record)))

        candidates.sort(key=lambda item: (-item[0], item[1]))
        bounded = candidates[:MAX_SUBFINDER_FINDINGS]
        findings: list[FindingRecord] = []
        for _, _, record in bounded:
            host = str(record["host"])
            sources = record.get("sources") or []
            source_count = len(sources) if isinstance(sources, list) else 0
            stronger = source_count >= 2 or bool(record.get("ip"))
            record["source_count"] = source_count
            record["bounded_result_limit"] = MAX_SUBFINDER_FINDINGS
            record["upstream_record_count"] = len(candidates)
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="subdomain",
                    platform="DNS",
                    value=host,
                    display_url=host,
                    status=FindingStatus.FOUND if stronger else FindingStatus.LIKELY,
                    confidence=0.9 if stronger else 0.65,
                    title=host,
                    metadata=record,
                    source_tool=self.tool_id,
                    source_record_id=host,
                    raw_output_reference=raw_reference,
                )
            )
        return findings


REGISTRY: dict[str, type[Adapter]] = {
    a.tool_id: a
    for a in (
        MaigretAdapter,
        SherlockAdapter,
        BlackbirdAdapter,
        NaminterAdapter,
        InstaloaderAdapter,
        NexfilAdapter,
        SocialAnalyzerAdapter,
        HoleheAdapter,
        HarvesterAdapter,
        SubfinderAdapter,
        CommonCrawlAdapter,
        BingNameSearchAdapter,
        SearXNGNameSearchAdapter,
        Metasearch2Adapter,
    )
}


def get_adapter(tool_id: str) -> Adapter:
    try:
        return REGISTRY[tool_id]()
    except KeyError as exc:
        raise ValueError(f"unsupported tool: {tool_id}") from exc
