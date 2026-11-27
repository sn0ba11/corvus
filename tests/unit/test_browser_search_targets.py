from app.api.routes import _browser_search_targets
from app.models.db import Investigation
from app.services.managed_browser_extraction import policy_for_url


def _case(query: str, query_type: str) -> Investigation:
    return Investigation(
        id="case-search",
        title="Search",
        query=query,
        query_type=query_type,
        selected_tools=[],
        tags=[],
        status="completed",
    )


def test_sock_browser_search_uses_engines_that_rarely_challenge() -> None:
    targets = _browser_search_targets(_case("@example", "username"))

    assert len(targets) == 4
    assert all("bing.com" not in url and "google.com" not in url for url in targets)
    assert sum("duckduckgo.com" in url for url in targets) == 2
    assert sum("startpage.com" in url for url in targets) == 2
    assert any("site%3Ainstagram.com" in url for url in targets)
    assert all("%22example%22" in url for url in targets)


def test_sock_browser_search_targets_are_readable_search_pages() -> None:
    for url in _browser_search_targets(_case("Jane Example", "name")):
        policy = policy_for_url(url)
        assert policy is not None and policy.kind == "search_results"


def test_sock_browser_search_is_not_offered_for_other_case_types() -> None:
    assert _browser_search_targets(_case("example.com", "domain")) == ()


def test_an_older_allowed_hosts_setting_cannot_remove_built_in_hosts() -> None:
    from app.core.config import Settings
    from app.services.browser_agent import configured_allowed_hosts, host_allowed

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, browser_allowed_hosts="google.com,internal.example"
    )
    hosts = configured_allowed_hosts(settings)

    assert host_allowed("t.me", hosts) and host_allowed("www.snapchat.com", hosts)
    assert host_allowed("internal.example", hosts)
    assert not host_allowed("unlisted.example", hosts)
