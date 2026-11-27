from app.services.platform_icons import (
    platform_icon_path,
    platform_icon_slug,
    platform_icon_svg,
)


def test_platform_icon_slug_matches_clean_display_name() -> None:
    assert platform_icon_slug("TikTok") == "tiktok"
    assert platform_icon_slug("Instagram") == "instagram"


def test_platform_icon_slug_matches_bare_hostname() -> None:
    assert platform_icon_slug("tiktok.com") == "tiktok"
    assert platform_icon_slug("steamcommunity.com") == "steam"


def test_platform_icon_slug_strips_www_and_casefolds() -> None:
    assert platform_icon_slug("www.reddit.com") == "reddit"
    assert platform_icon_slug("REDDIT") == "reddit"
    assert platform_icon_slug("  Reddit  ") == "reddit"


def test_platform_icon_slug_twitter_alias_maps_to_x() -> None:
    assert platform_icon_slug("Twitter") == "x"
    assert platform_icon_slug("twitter.com") == "x"
    assert platform_icon_slug("X / Twitter") == "x"


def test_platform_icon_slug_unmapped_platform_returns_none() -> None:
    assert platform_icon_slug("LinkedIn") is None
    assert platform_icon_slug("Some Unlisted Platform") is None


def test_platform_icon_slug_handles_none_and_empty() -> None:
    assert platform_icon_slug(None) is None
    assert platform_icon_slug("") is None


def test_platform_icon_svg_returns_inline_markup_for_mapped_platform() -> None:
    svg = platform_icon_svg("Instagram")
    assert svg is not None
    assert svg.strip().startswith("<svg")
    assert "<title>Instagram</title>" in svg


def test_platform_icon_svg_returns_local_fallback_for_unmapped_platform() -> None:
    svg = platform_icon_svg("LinkedIn")
    assert svg is not None
    assert svg.startswith("<svg")
    assert "<title>LinkedIn</title>" in svg
    assert "href=" not in svg
    assert "url(" not in svg


def test_major_account_platforms_have_bundled_marks() -> None:
    for platform in (
        "Twitch",
        "SoundCloud",
        "YouTube",
        "Pinterest",
        "PayPal",
        "Roblox",
        "DeviantART",
        "Fiverr",
        "Chess",
        "Codecademy",
        "Freelancer.com",
        "MySpace",
        "Wattpad",
        "WordPress",
        "GoodReads",
        "ReverbNation",
        "Spotify",
    ):
        slug = platform_icon_slug(platform)
        assert slug is not None, platform
        svg = platform_icon_svg(platform)
        assert svg is not None and "<path" in svg, platform


def test_platform_icon_path_returns_static_url_for_mapped_platform() -> None:
    assert platform_icon_path("TikTok") == "/static/icons/platforms/tiktok.svg"


def test_platform_icon_path_returns_none_for_unmapped_platform() -> None:
    assert platform_icon_path("LinkedIn") is None
