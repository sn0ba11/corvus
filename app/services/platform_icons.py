from __future__ import annotations

import hashlib
from functools import lru_cache
from html import escape
from pathlib import Path

ICONS_DIR = Path(__file__).resolve().parents[1] / "static" / "icons" / "platforms"

# Finding.platform is free text: some collectors set a clean display name ("TikTok",
# "Steam"), others fall back to a bare hostname derived from a result URL
# ("tiktok.com", "www.reddit.com"). Both forms are normalised (casefold, strip a
# leading "www.") before this lookup, so either alias form below is enough.
_PLATFORM_ICON_SLUGS: dict[str, str] = {
    "instagram": "instagram",
    "instagram.com": "instagram",
    "facebook": "facebook",
    "facebook.com": "facebook",
    "tiktok": "tiktok",
    "tiktok.com": "tiktok",
    "x": "x",
    "x.com": "x",
    "x / twitter": "x",
    "twitter": "x",
    "twitter.com": "x",
    "github": "github",
    "github.com": "github",
    "gitlab": "gitlab",
    "gitlab.com": "gitlab",
    "reddit": "reddit",
    "reddit.com": "reddit",
    "steam": "steam",
    "steamcommunity.com": "steam",
    "tripadvisor": "tripadvisor",
    "tripadvisor.com": "tripadvisor",
    "tripadvisor.ie": "tripadvisor",
    "tripadvisor.co.uk": "tripadvisor",
    "yelp": "yelp",
    "yelp.com": "yelp",
    "strava": "strava",
    "strava.com": "strava",
    "alltrails": "alltrails",
    "alltrails.com": "alltrails",
    "komoot": "komoot",
    "komoot.com": "komoot",
    "garmin connect": "garmin",
    "connect.garmin.com": "garmin",
    "garmin.com": "garmin",
    "google maps": "googlemaps",
    "snapchat": "snapchat",
    "snapchat.com": "snapchat",
    "duolingo": "duolingo",
    "duolingo.com": "duolingo",
    "twitch": "twitch",
    "twitch.tv": "twitch",
    "m.twitch.tv": "twitch",
    "twitchtracker.com": "twitch",
    "soundcloud": "soundcloud",
    "soundcloud.com": "soundcloud",
    "youtube": "youtube",
    "youtube.com": "youtube",
    "pinterest": "pinterest",
    "pinterest.com": "pinterest",
    "paypal": "paypal",
    "paypal.com": "paypal",
    "roblox": "roblox",
    "roblox.com": "roblox",
    "deviantart": "deviantart",
    "deviantart.com": "deviantart",
    "fiverr": "fiverr",
    "fiverr.com": "fiverr",
    "chess": "chessdotcom",
    "chess.com": "chessdotcom",
    "codecademy": "codecademy",
    "codecademy.com": "codecademy",
    "freelancer": "freelancer",
    "freelancer.com": "freelancer",
    "myspace": "myspace",
    "myspace.com": "myspace",
    "wattpad": "wattpad",
    "wattpad.com": "wattpad",
    "wordpress": "wordpress",
    "wordpress.com": "wordpress",
    "wordpress.com (public)": "wordpress",
    "goodreads": "goodreads",
    "goodreads.com": "goodreads",
    "reverbnation": "reverbnation",
    "reverbnation.com": "reverbnation",
    "spotify": "spotify",
    "spotify.com": "spotify",
}


def _normalise(platform: str | None) -> str:
    return (platform or "").strip().casefold().removeprefix("www.")


def platform_icon_slug(platform: str | None) -> str | None:
    """Return the bundled icon slug for a platform value, or None if unmapped.

    Unmapped platforms (including LinkedIn - see THIRD_PARTY_NOTICES.md) are expected;
    callers should fall back to the existing text-initials badge rather than treat this
    as an error.
    """
    return _PLATFORM_ICON_SLUGS.get(_normalise(platform))


@lru_cache(maxsize=64)
def _read_icon_svg(slug: str) -> str | None:
    path = ICONS_DIR / f"{slug}.svg"
    try:
        if not path.is_file() or path.stat().st_size > 32_768:
            return None
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _fallback_icon_svg(platform: str) -> str:
    """Generate a local deterministic app tile when no trademark mark is bundled."""
    normalised = _normalise(platform) or "unknown"
    digest = hashlib.sha256(normalised.encode("utf-8")).digest()
    rotation = (digest[0] % 4) * 15 - 22
    inset = 4 + digest[1] % 4
    radius = 2 + digest[2] % 4
    title = escape(platform.strip() or "Unknown platform")
    return (
        '<svg role="img" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        f"<title>{title}</title>"
        f'<rect x="3" y="3" width="18" height="18" rx="{radius}" fill="currentColor" opacity=".18"/>'
        f'<rect x="{inset}" y="{inset}" width="{24 - inset * 2}" height="{24 - inset * 2}" '
        f'rx="{max(1, radius - 1)}" fill="none" stroke="currentColor" stroke-width="1.7" '
        f'transform="rotate({rotation} 12 12)"/>'
        '<circle cx="12" cy="12" r="2.4" fill="currentColor"/>'
        "</svg>"
    )


def platform_icon_svg(platform: str | None) -> str | None:
    """Return a bundled brand mark or a deterministic local fallback SVG.

    Reports are self-contained, so no favicon or third-party image request is ever
    required. Empty platform values still return None because there is no label to
    represent.
    """
    if not (platform or "").strip():
        return None
    slug = platform_icon_slug(platform)
    icon = _read_icon_svg(slug) if slug else None
    return icon or _fallback_icon_svg(str(platform))


def platform_icon_path(platform: str | None) -> str | None:
    """Return the /static/... URL for a platform's icon, for pages the running app
    itself serves (dashboard, investigation view)."""
    slug = platform_icon_slug(platform)
    return f"/static/icons/platforms/{slug}.svg" if slug else None
