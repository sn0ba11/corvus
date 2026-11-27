import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def browser_agent_main(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    playwright_async_api = ModuleType("playwright.async_api")
    for name in ("BrowserContext", "Page", "Playwright", "Request", "Route"):
        setattr(playwright_async_api, name, type(name, (), {}))
    playwright_async_api.Error = type("PlaywrightError", (Exception,), {})  # type: ignore[attr-defined]

    def _unexpected_async_playwright() -> None:
        raise AssertionError("real playwright must not be invoked in this test")

    playwright_async_api.async_playwright = _unexpected_async_playwright  # type: ignore[attr-defined]
    playwright_pkg = ModuleType("playwright")
    monkeypatch.setitem(sys.modules, "playwright", playwright_pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", playwright_async_api)

    path = Path(__file__).parents[2] / "browser_agent" / "main.py"
    spec = importlib.util.spec_from_file_location("browser_agent_main_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "browser_agent_main_test", module)
    spec.loader.exec_module(module)
    return module


def test_google_maps_and_google_search_route_to_distinct_policies(
    browser_agent_main: ModuleType,
) -> None:
    maps_policy = browser_agent_main.platform_policy(
        "https://www.google.com/maps/place/Some+Cafe/@53.34,-6.26,15z"
    )
    search_policy = browser_agent_main.platform_policy(
        "https://www.google.com/search?q=someone"
    )

    assert maps_policy == ("google_maps", "location_history", False)
    assert search_policy == ("google_search", "search_results", False)


def test_google_ie_search_routes_to_search_policy(browser_agent_main: ModuleType) -> None:
    assert browser_agent_main.platform_policy("https://www.google.ie/search?q=someone") == (
        "google_search",
        "search_results",
        False,
    )


def test_bing_search_routes_to_search_policy(browser_agent_main: ModuleType) -> None:
    assert browser_agent_main.platform_policy("https://www.bing.com/search?q=someone") == (
        "bing_search",
        "search_results",
        False,
    )


def test_manual_search_handoff_engines_are_allowlisted_and_extractable(
    browser_agent_main: ModuleType,
) -> None:
    expected = {
        "https://duckduckgo.com/?q=someone": "duckduckgo_search",
        "https://search.brave.com/search?q=someone": "brave_search",
        "https://www.qwant.com/?q=someone&t=web": "qwant_search",
        "https://www.startpage.com/sp/search?query=someone": "startpage_search",
    }
    for url, platform in expected.items():
        host = url.split("/", 3)[2]
        assert browser_agent_main.host_allowed(host)
        assert browser_agent_main.platform_policy(url) == (platform, "search_results", False)


def test_google_com_with_unrelated_path_matches_no_policy(browser_agent_main: ModuleType) -> None:
    assert browser_agent_main.platform_policy("https://www.google.com/preferences") is None


def test_instagram_reserved_path_still_matches_no_policy(browser_agent_main: ModuleType) -> None:
    """Regression guard: the return-None-to-continue loop fix must not weaken existing gates."""
    assert browser_agent_main.platform_policy("https://www.instagram.com/explore/") is None


def test_pinterest_and_threads_browser_policies_are_profile_only(browser_agent_main: ModuleType) -> None:
    policy = browser_agent_main.platform_policy
    assert policy('https://www.pinterest.com/lazeredwithlove/') == ('pinterest', 'social_profile', False)
    assert policy('https://www.threads.com/@lazeredwithlove') == ('threads', 'social_profile', False)
    assert policy('https://www.threads.net/@lazeredwithlove') == ('threads', 'social_profile', False)
    assert policy('https://www.pinterest.com/pin/1234/') is None
    assert policy('https://www.pinterest.com/ideas/example/1234/') is None
    assert policy('https://www.threads.com/@lazeredwithlove/post/123') is None


def test_instagram_profile_url_still_matches_social_profile(
    browser_agent_main: ModuleType,
) -> None:
    assert browser_agent_main.platform_policy("https://www.instagram.com/someuser/") == (
        "instagram",
        "social_profile",
        False,
    )


def test_stealth_disabled_selects_standard_playwright(browser_agent_main: ModuleType) -> None:
    browser_agent_main.STEALTH_BROWSER_ENABLED = False

    assert browser_agent_main._async_playwright_factory() is browser_agent_main.async_playwright


def test_stealth_enabled_selects_patchright(
    browser_agent_main: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    patchright_async_api = ModuleType("patchright.async_api")
    sentinel = object()
    patchright_async_api.async_playwright = sentinel  # type: ignore[attr-defined]
    patchright_pkg = ModuleType("patchright")
    monkeypatch.setitem(sys.modules, "patchright", patchright_pkg)
    monkeypatch.setitem(sys.modules, "patchright.async_api", patchright_async_api)
    browser_agent_main.STEALTH_BROWSER_ENABLED = True

    assert browser_agent_main._async_playwright_factory() is sentinel


def test_self_completing_browser_checks_are_recognised(browser_agent_main: ModuleType) -> None:
    check = browser_agent_main.is_automatic_browser_check
    assert check("Verifying your request...\n\nCalculating...\nDifficulty: 6")
    assert check("Making sure you're not a bot!")
    assert not check("Linus Torvalds (@torvalds) · Instagram photos and videos")
    # A check that never clears is reported as a challenge, not as an empty result page.
    assert "verifying your request" in browser_agent_main.CHALLENGE_MARKERS
