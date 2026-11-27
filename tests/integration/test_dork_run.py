import asyncio
import re
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import routes
from app.core.config import Settings
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.db import Base, BrowserRun, Finding, Investigation
from app.services import dork_run
from app.services.browser_agent import BrowserAgentStatus, BrowserCaptureResult
from app.services.managed_browser_extraction import _search_item_matches_investigation
from app.services.search_imports import SearchResultItem


def make_case(query: str = "example_person", query_type: str = "username") -> str:
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        case = Investigation(title="Dork run case", query=query, query_type=query_type, status="completed")
        db.add(case)
        db.commit()
        return case.id


def test_plan_groups_social_platforms_first_and_rotates_engines() -> None:
    case = Investigation(title="t", query="@example_person", query_type="username")
    plan = dork_run.plan_dork_run(case, ("duckduckgo", "brave", "bing"), max_queries=6)
    assert dork_run.DEFAULT_ENGINES == ("duckduckgo", "brave")
    assert len(plan) == 6
    assert plan[0].query == '"example_person" (site:instagram.com OR site:threads.net OR site:facebook.com)'
    # Bing ignores site: and filetype: restrictions, so it only receives the bare exact query.
    assert [item.engine for item in plan[:4]] == ["duckduckgo", "brave", "duckduckgo", "brave"]
    assert [item.id for item in plan if item.engine == "bing"] == ["exact"]
    try:
        dork_run.plan_dork_run(case, ("bing",))
    except ValueError as exc:
        assert "restrictions" in str(exc)
    else:
        raise AssertionError("expected ValueError")
    assert len({item.query for item in plan}) == 6
    # Single-site profile dorks are covered by the grouped queries.
    assert not any(item.id == "username-instagram" for item in plan)


def test_plan_rejects_unsupported_case_types_and_empty_engine_choice() -> None:
    domain_case = Investigation(title="t", query="example.org", query_type="domain")
    for call in (
        lambda: dork_run.plan_dork_run(domain_case, ("duckduckgo",)),
        lambda: dork_run.normalise_engines(["yahoo"]),
    ):
        try:
            call()
        except ValueError:
            continue
        raise AssertionError("expected ValueError")
    assert dork_run.clamp_delays(1, 5000) == (10, 600)
    assert dork_run.clamp_delays(120, 30) == (120, 120)


def test_grouped_site_query_accepts_any_listed_site_only() -> None:
    case = Investigation(title="t", query="example_person", query_type="username")
    query = '"example_person" (site:instagram.com OR site:reddit.com/user) -site:pinterest.com'

    def matches(url: str) -> bool:
        return _search_item_matches_investigation(
            case, query, SearchResultItem(url=url, title="example_person", snippet=None)
        )

    assert matches("https://www.instagram.com/example_person/")
    assert matches("https://old.reddit.com/user/example_person")
    assert not matches("https://www.pinterest.com/example_person/")
    assert not matches("https://unrelated.example/example_person")


def test_queue_imports_results_and_drops_a_challenged_engine(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    case_id = make_case()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        browser_enabled=True,
        browser_agent_token="agent-" + "x" * 40,
        browser_vnc_password="dork-run-test-password",  # noqa: S106 - synthetic test secret
        browser_evidence_dir=tmp_path / "browser",
        data_dir=tmp_path / "data",
    )
    settings.browser_evidence_dir.mkdir(parents=True)
    captures: list[str] = []
    abandoned: list[str] = []
    pauses: list[float] = []

    class FakeBrowserClient:
        def __init__(self, configured_settings) -> None:  # type: ignore[no-untyped-def]
            assert configured_settings is settings

        async def status(self) -> BrowserAgentStatus:
            return BrowserAgentStatus(
                available=True, enabled=True, profile_ready=True, active_job=False,
                pending_job_id=None, browser_version="149.0", allowed_hosts=(),
                authenticated_sessions=(),
            )

        async def capture(self, *, job_id, investigation_id, url, collector_id):  # type: ignore[no-untyped-def]
            captures.append(url)
            manual = "search.brave.com" in url
            root = settings.browser_evidence_dir / investigation_id / job_id
            root.mkdir(parents=True, exist_ok=True)
            (root / "page.png").write_bytes(b"png")
            (root / "manifest.json").write_text("{}", encoding="utf-8")
            relative = f"{investigation_id}/{job_id}"
            return BrowserCaptureResult(
                status="manual_action_required" if manual else "completed",
                requested_url=url,
                final_url=url,
                observed_at=datetime(2026, 11, 12, 10, 0, tzinfo=UTC),
                page_title="Search",
                challenge_type="captcha" if manual else None,
                screenshot_reference=f"{relative}/page.png",
                html_reference=None,
                manifest_reference=f"{relative}/manifest.json",
                screenshot_sha256="1" * 64,
                html_sha256=None,
                manifest_sha256="2" * 64,
                browser_version="149.0",
                extraction_kind=None if manual else "search_results",
                extraction_platform=None if manual else "startpage_search",
                extracted=None if manual else {
                    "search_results": [
                        {"href": f"https://www.instagram.com/example_person/?n={len(captures)}",
                         "title": "example_person • Instagram", "snippet": "Public profile"},
                        {"href": "https://unrelated.example/page", "title": "Unrelated", "snippet": ""},
                    ]
                },
            )

        async def abandon(self, *, job_id, investigation_id):  # type: ignore[no-untyped-def]
            abandoned.append(job_id)

    async def fake_sleep(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "BrowserAgentClient", FakeBrowserClient)
    monkeypatch.setattr(routes, "_dork_run_sleep", fake_sleep)

    with SessionLocal() as db:
        case = db.get(Investigation, case_id)
        plan = dork_run.plan_dork_run(case, ("duckduckgo", "brave"), max_queries=3)
        dork_run.save_state(db, case_id, dork_run.new_state(plan, ("duckduckgo", "brave"), 45, 90))
        db.commit()

    asyncio.run(routes.run_dork_queue(case_id))

    with SessionLocal() as db:
        state = dork_run.load_state(db, case_id)
        runs = db.scalars(select(BrowserRun).where(BrowserRun.investigation_id == case_id)).all()
        findings = db.scalars(select(Finding).where(Finding.investigation_id == case_id)).all()
    assert state["status"] == "completed"
    assert state["cooled_engines"] == ["brave"]
    assert [item["status"] for item in state["items"]] == ["completed"] * 3
    # The challenged Brave query was released and re-sent once through DuckDuckGo.
    assert state["items"][1]["engine"] == "duckduckgo" and state["items"][1]["retried"] is True
    assert len(abandoned) == 1
    assert sum("search.brave.com" in url for url in captures) == 1
    assert len(captures) == 4
    assert sorted(run.status for run in runs) == ["cancelled", "completed", "completed", "completed"]
    assert dork_run.summarise(state)["imported"] >= 1
    assert findings and all("unrelated.example" not in (f.canonical_url or "") for f in findings)
    assert pauses and all(45 <= pause <= 90 for pause in pauses)


def test_page_starts_and_cancels_a_run(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    case_id = make_case()
    started: list[str] = []

    async def fake_queue(investigation_id: str) -> None:
        started.append(investigation_id)
        await asyncio.sleep(30)

    monkeypatch.setattr(routes, "run_dork_queue", fake_queue)
    settings = routes.get_settings().model_copy(update={"browser_enabled": True})
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    with TestClient(app) as client:
        page = client.get(f"/investigations/{case_id}/google-dorks")
        assert page.status_code == 200 and "Start automated run" in page.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/investigations/{case_id}/dork-run",
            data={"csrf": csrf, "engines": ["duckduckgo", "bing"], "max_queries": "4",
                  "delay_min": "20", "delay_max": "30"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        running = client.get(f"/investigations/{case_id}/google-dorks")
        assert 'data-auto-reload="15"' in running.text and "Cancel run" in running.text
        duplicate = client.post(
            f"/investigations/{case_id}/dork-run",
            data={"csrf": csrf, "engines": ["brave"]}, follow_redirects=False,
        )
        assert duplicate.status_code == 409
        cancel = client.post(
            f"/investigations/{case_id}/dork-run/cancel", data={"csrf": csrf}, follow_redirects=False
        )
        assert cancel.status_code == 303
        final = client.get(f"/investigations/{case_id}/google-dorks")
        assert "cancelled" in final.text and "data-auto-reload" not in final.text
        for task in list(routes.dork_run_tasks.values()):
            task.cancel()
    with SessionLocal() as db:
        state = dork_run.load_state(db, case_id)
    assert state["status"] == "cancelled" and len(state["items"]) == 4


def test_cancel_during_a_search_is_kept_and_empty_pages_are_flagged(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    case_id = make_case()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        browser_enabled=True,
        browser_agent_token="agent-" + "x" * 40,
        browser_vnc_password="dork-run-test-password",  # noqa: S106 - synthetic test secret
        browser_evidence_dir=tmp_path / "browser",
        data_dir=tmp_path / "data",
    )
    settings.browser_evidence_dir.mkdir(parents=True)
    captures: list[str] = []

    class FakeBrowserClient:
        def __init__(self, configured_settings) -> None:  # type: ignore[no-untyped-def]
            pass

        async def status(self) -> BrowserAgentStatus:
            return BrowserAgentStatus(
                available=True, enabled=True, profile_ready=True, active_job=False,
                pending_job_id=None, browser_version="149.0", allowed_hosts=(),
                authenticated_sessions=(),
            )

        async def capture(self, *, job_id, investigation_id, url, collector_id):  # type: ignore[no-untyped-def]
            captures.append(url)
            if len(captures) == 2:
                # Another request cancels the run while this search is in the browser.
                with SessionLocal() as other:
                    state = dork_run.load_state(other, investigation_id)
                    dork_run.finish_state(state, "cancelled", "cancelled by the analyst")
                    dork_run.save_state(other, investigation_id, state)
                    other.commit()
            root = settings.browser_evidence_dir / investigation_id / job_id
            root.mkdir(parents=True, exist_ok=True)
            (root / "page.png").write_bytes(b"png")
            (root / "manifest.json").write_text("{}", encoding="utf-8")
            relative = f"{investigation_id}/{job_id}"
            return BrowserCaptureResult(
                status="completed", requested_url=url, final_url=url,
                observed_at=datetime(2026, 11, 12, 10, 0, tzinfo=UTC), page_title="Search",
                challenge_type=None, screenshot_reference=f"{relative}/page.png",
                html_reference=None, manifest_reference=f"{relative}/manifest.json",
                screenshot_sha256="1" * 64, html_sha256=None, manifest_sha256="2" * 64,
                browser_version="149.0", extraction_kind="search_results",
                extraction_platform="startpage_search", extracted={"search_results": []},
            )

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "BrowserAgentClient", FakeBrowserClient)
    monkeypatch.setattr(routes, "_dork_run_sleep", fake_sleep)
    with SessionLocal() as db:
        case = db.get(Investigation, case_id)
        plan = dork_run.plan_dork_run(case, ("startpage",), max_queries=4)
        dork_run.save_state(db, case_id, dork_run.new_state(plan, ("startpage",), 45, 90))
        db.commit()

    asyncio.run(routes.run_dork_queue(case_id))

    with SessionLocal() as db:
        state = dork_run.load_state(db, case_id)
    assert state["status"] == "cancelled"
    assert len(captures) == 2
    assert [item["status"] for item in state["items"]] == ["no_results", "no_results", "skipped", "skipped"]
    assert state["items"][0]["extracted"] == 0 and state["items"][0]["detail"]


def test_deleting_a_case_removes_its_stored_run() -> None:
    case_id = make_case()
    with SessionLocal() as db:
        case = db.get(Investigation, case_id)
        plan = dork_run.plan_dork_run(case, ("startpage",), max_queries=2)
        state = dork_run.new_state(plan, ("startpage",), 45, 90)
        dork_run.finish_state(state, "completed")
        dork_run.save_state(db, case_id, state)
        db.commit()
    with TestClient(app) as client:
        page = client.get(f"/investigations/{case_id}")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/investigations/{case_id}/delete", data={"csrf": csrf, "confirm": "DELETE"},
            follow_redirects=False,
        )
        assert response.status_code == 303
    with SessionLocal() as db:
        assert dork_run.load_state(db, case_id) is None


def test_plan_interleaves_every_username_and_name_in_the_case() -> None:
    from app.models.db import ToolRun

    case = Investigation(title="t", query="jane@example.org", query_type="email")
    case.runs = [
        ToolRun(tool_id="maigret", display_name="Maigret", query_value="jane_example",
                query_type="username", pivot_depth=0, pivot_automatic=False),
        ToolRun(tool_id="bing_name", display_name="Bing", query_value="Jane Example",
                query_type="name", pivot_depth=0, pivot_automatic=False),
        # A pivot discovered from a finding is not an analyst-supplied identifier.
        ToolRun(tool_id="maigret", display_name="Maigret", query_value="other_handle",
                query_type="username", pivot_depth=1, pivot_automatic=True),
    ]
    plan = dork_run.plan_dork_run(case, ("duckduckgo", "brave"), max_queries=9)
    # The email address has no social-platform group, so it starts on its wider pack.
    assert plan[0].query == '"jane@example.org"'
    assert plan[1].query.startswith('"jane_example" (site:instagram.com')
    assert plan[2].query.startswith('"Jane Example" (site:instagram.com')
    assert not any("other_handle" in item.query for item in plan)
    assert not any("example.org" in item.query and "site:instagram.com" in item.query for item in plan)
    assert plan[1].label.endswith("· jane_example")
    # Results are judged against the identifier each query quotes.
    for query, title, expected in (
        (plan[1].query, "jane_example (@jane_example)", True),
        (plan[2].query, "Jane Example | Facebook", True),
        (plan[2].query, "Somebody Else | Facebook", False),
    ):
        url = "https://www.facebook.com/jane_example" if "jane_example" in query else "https://www.facebook.com/people/x/1"
        item = SearchResultItem(url=url, title=title, snippet=None)
        assert _search_item_matches_investigation(case, query, item) is expected


def test_a_failed_search_is_retried_once_on_another_engine(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.services.browser_agent import BrowserAgentError

    case_id = make_case()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, browser_enabled=True, browser_agent_token="agent-" + "x" * 40,
        browser_vnc_password="dork-run-test-password",  # noqa: S106 - synthetic test secret
        browser_evidence_dir=tmp_path / "browser", data_dir=tmp_path / "data",
    )
    settings.browser_evidence_dir.mkdir(parents=True)
    captures: list[str] = []

    class FailingBrowserClient:
        def __init__(self, configured_settings) -> None:  # type: ignore[no-untyped-def]
            pass

        async def status(self) -> BrowserAgentStatus:
            return BrowserAgentStatus(
                available=True, enabled=True, profile_ready=True, active_job=False,
                pending_job_id=None, browser_version="149.0", allowed_hosts=(),
                authenticated_sessions=(),
            )

        async def capture(self, *, job_id, investigation_id, url, collector_id):  # type: ignore[no-untyped-def]
            captures.append(url)
            raise BrowserAgentError("target_unresolved", "target host did not resolve")

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "BrowserAgentClient", FailingBrowserClient)
    monkeypatch.setattr(routes, "_dork_run_sleep", fake_sleep)
    with SessionLocal() as db:
        case = db.get(Investigation, case_id)
        plan = dork_run.plan_dork_run(case, ("duckduckgo", "brave"), max_queries=4)
        dork_run.save_state(db, case_id, dork_run.new_state(plan, ("duckduckgo", "brave"), 45, 90))
        db.commit()

    asyncio.run(routes.run_dork_queue(case_id))

    with SessionLocal() as db:
        state = dork_run.load_state(db, case_id)
    # Each search gets one retry on the other engine; two exhausted searches stop the run.
    assert state["status"] == "stopped" and "two searches failed in a row" in state["detail"]
    assert ["duckduckgo.com" in url for url in captures] == [True, False, False, True]
    assert [item["status"] for item in state["items"]] == ["failed", "failed", "skipped", "skipped"]


def test_results_that_ignore_operators_or_only_contain_the_handle_are_rejected() -> None:
    case = Investigation(title="t", query="jdoe", query_type="username")
    case.runs = []

    def matches(query: str, url: str, title: str = "jdoe") -> bool:
        return _search_item_matches_investigation(
            case, query, SearchResultItem(url=url, title=title, snippet=None)
        )

    documents = '"jdoe" (filetype:pdf OR filetype:docx)'
    assert matches(documents, "https://example.org/files/jdoe-report.PDF")
    # An engine that drops filetype: returns ordinary pages; they do not answer the query.
    assert not matches(documents, "https://x.com/jdoe")
    exact = '"jdoe"'
    assert matches(exact, "https://x.com/jdoe", title="")
    assert matches(exact, "https://example.org/post/1", title="Interview with @jdoe")
    assert not matches(exact, "https://www.instagram.com/bigjdoe", title="")
    assert not matches(exact, "https://www.instagram.com/jdoe_fan", title="")
    dotted = Investigation(title="t", query="john.doe", query_type="username")
    dotted.runs = []
    # On a platform's own account page the handle must match exactly: @johndoe is a
    # different account from john.doe, however likely it is to be the same person.
    item = SearchResultItem(url="https://www.tiktok.com/@johndoe", title="", snippet=None)
    assert not _search_item_matches_investigation(dotted, '"john.doe"', item)
    forum = SearchResultItem(url="https://forum.example/members/john-doe", title="", snippet=None)
    assert _search_item_matches_investigation(dotted, '"john.doe"', forum)
    # Name cases obey site: restrictions too.
    named = Investigation(title="t", query="Jane Example", query_type="name")
    named.runs = []
    off_site = SearchResultItem(url="https://x.com/jane", title="Jane Example", snippet=None)
    assert not _search_item_matches_investigation(named, '"Jane Example" (site:instagram.com OR site:facebook.com)', off_site)


def test_wildcard_site_terms_accept_subdomains() -> None:
    case = Investigation(title="t", query="example.org", query_type="domain")
    case.runs = []
    query = "site:*.example.org -site:www.example.org"
    hit = SearchResultItem(url="https://blog.example.org/post", title="Post", snippet=None)
    miss = SearchResultItem(url="https://other.test/post", title="Post", snippet=None)
    assert _search_item_matches_investigation(case, query, hit)
    assert not _search_item_matches_investigation(case, query, miss)


def test_email_and_phone_cases_get_the_wider_pack_and_checked_results() -> None:
    email_case = Investigation(title="t", query="jane.doe@example.org", query_type="email")
    email_case.runs = []
    plan = dork_run.plan_dork_run(email_case, ("duckduckgo", "brave"), max_queries=10)
    assert plan and not any("site:instagram.com" in item.query for item in plan)
    assert plan[0].query == '"jane.doe@example.org"'

    def judge(case: Investigation, query: str, url: str, title: str, snippet: str | None) -> bool:
        return _search_item_matches_investigation(
            case, query, SearchResultItem(url=url, title=title, snippet=snippet)
        )

    exact = '"jane.doe@example.org"'
    assert judge(email_case, exact, "https://a.example/staff", "Staff", "Contact jane.doe@example.org today")
    assert judge(email_case, exact, "https://a.example/staff", "Staff", "jane.doe at example.org")
    assert not judge(email_case, exact, "https://a.example/news", "News", "Nothing about the address")
    # No snippet means nothing to check: keep only results a site: or filetype: term narrowed.
    assert not judge(email_case, exact, "https://a.example/news", "News", None)
    assert judge(email_case, 'site:github.com "jane.doe@example.org"', "https://github.com/x/y", "y", None)

    phone_case = Investigation(title="t", query="+353 86 123 4567", query_type="phone")
    phone_case.runs = []
    plan = dork_run.plan_dork_run(phone_case, ("duckduckgo",), max_queries=5)
    assert plan[0].query == '"+353 86 123 4567"'
    assert judge(phone_case, plan[0].query, "https://a.example/c", "Contact", "Call (086) 123-4567 now")
    assert judge(phone_case, '"353861234567"', "https://a.example/c", "Contact", "+353 86 123 4567")
    assert not judge(phone_case, plan[0].query, "https://a.example/c", "Contact", "Call 086 999 0000")


def test_email_case_runs_through_the_queue_and_keeps_only_matching_snippets(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    case_id = make_case("jane.doe@example.org", "email")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, browser_enabled=True, browser_agent_token="agent-" + "x" * 40,
        browser_vnc_password="dork-run-test-password",  # noqa: S106 - synthetic test secret
        browser_evidence_dir=tmp_path / "browser", data_dir=tmp_path / "data",
    )
    settings.browser_evidence_dir.mkdir(parents=True)

    class FakeBrowserClient:
        def __init__(self, configured_settings) -> None:  # type: ignore[no-untyped-def]
            pass

        async def status(self) -> BrowserAgentStatus:
            return BrowserAgentStatus(
                available=True, enabled=True, profile_ready=True, active_job=False,
                pending_job_id=None, browser_version="149.0", allowed_hosts=(),
                authenticated_sessions=(),
            )

        async def capture(self, *, job_id, investigation_id, url, collector_id):  # type: ignore[no-untyped-def]
            root = settings.browser_evidence_dir / investigation_id / job_id
            root.mkdir(parents=True, exist_ok=True)
            (root / "page.png").write_bytes(b"png")
            (root / "manifest.json").write_text("{}", encoding="utf-8")
            relative = f"{investigation_id}/{job_id}"
            return BrowserCaptureResult(
                status="completed", requested_url=url, final_url=url,
                observed_at=datetime(2026, 11, 13, 10, 0, tzinfo=UTC), page_title="Search",
                challenge_type=None, screenshot_reference=f"{relative}/page.png", html_reference=None,
                manifest_reference=f"{relative}/manifest.json", screenshot_sha256="1" * 64,
                html_sha256=None, manifest_sha256="2" * 64, browser_version="149.0",
                extraction_kind="search_results", extraction_platform="duckduckgo_search",
                extracted={"search_results": [
                    {"href": "https://staff.example/people", "title": "People",
                     "snippet": "Contact jane.doe@example.org for details"},
                    {"href": "https://noise.example/page", "title": "Unrelated", "snippet": "Nothing here"},
                ]},
            )

        async def abandon(self, *, job_id, investigation_id):  # type: ignore[no-untyped-def]
            raise AssertionError("nothing should be abandoned")

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "BrowserAgentClient", FakeBrowserClient)
    monkeypatch.setattr(routes, "_dork_run_sleep", fake_sleep)
    with SessionLocal() as db:
        case = db.get(Investigation, case_id)
        plan = dork_run.plan_dork_run(case, ("duckduckgo",), max_queries=1)
        dork_run.save_state(db, case_id, dork_run.new_state(plan, ("duckduckgo",), 45, 90))
        db.commit()

    asyncio.run(routes.run_dork_queue(case_id))

    with SessionLocal() as db:
        state = dork_run.load_state(db, case_id)
        findings = db.scalars(select(Finding).where(Finding.investigation_id == case_id)).all()
    assert state["status"] == "completed" and state["items"][0]["status"] == "completed"
    assert [finding.canonical_url for finding in findings] == ["https://staff.example/people"]


def test_username_results_must_concern_the_exact_handle_not_the_word() -> None:
    """Replays the shapes seen in the live run of 2026-11-13 for a surname-like handle."""
    from app.services.search_relevance import username_result_is_relevant as relevant

    assert relevant("torvalds", "https://x.com/torvalds", "torvalds (@torvalds) / X")
    assert relevant("torvalds", "https://x.com/torvalds/status/123", "A post")
    assert relevant("torvalds", "https://www.reddit.com/user/torvalds/comments/1/x/", "comment")
    assert relevant("torvalds", "https://www.youtube.com/watch?v=abc", "A talk", "Uploaded by @torvalds")
    assert relevant("torvalds", "https://blog.example/authors/torvalds", "Author page")
    for url, title in (
        ("https://www.instagram.com/thorbalds/", "TORVALDS (@thorbalds) • Instagram photos and videos"),
        ("https://www.instagram.com/linustorvalds/", "Linus Torvalds (@linustorvalds) • Instagram"),
        ("https://www.instagram.com/torvalds_cybertech/", "Linus J Torvalds (@torvalds_cybertech)"),
        ("https://www.linkedin.com/in/robert-torvalds-b0b804357", "Robert Torvalds - LinkedIn"),
        ("https://www.facebook.com/public/Linus-Torvalds/", "Linus Torvalds Profiles - Facebook"),
        ("https://www.instagram.com/popular/linus-torvalds/", "Linus Torvalds • 28K reels"),
        ("https://www.youtube.com/watch?v=3eGwsng4p24", "Linus Torvalds talks about AI in Linux"),
        ("https://www.pinterest.com/ideas/linus-torvalds/915313849851/", "Linus Torvalds - Pinterest"),
        ("https://www.reddit.com/r/Torvalds/", "Linus Torvalds - Reddit"),
        ("https://news.example/2026/interview", "Linus Torvalds on the kernel"),
        ("https://news.example/2026/interview-with-linus-torvalds", "Interview"),
        ("https://example.org/search?q=torvalds", "Search results"),
    ):
        assert not relevant("torvalds", url, title), url
    # Sites that give each account its own hostname.
    for url in ("https://jdoe.tumblr.com/", "https://jdoe.github.io/", "https://jdoe.substack.com/p/x",
                "https://jdoe.carrd.co/"):
        assert relevant("jdoe", url, "A page"), url
    assert not relevant("alice", "https://alice-extra.example.org/", "Unrelated")
    assert not relevant("example", "https://example.org/", "Example Domain")
    # A handle that is not an ordinary word is distinctive enough to trust in a title.
    assert relevant("jdoe_1987", "https://forum.example/thread/9", "Posts by jdoe_1987")
    assert not relevant("jdoe_1987", "https://forum.example/thread/9", "Posts by John Doe")


def _automatic_run_setup(monkeypatch, tmp_path, *, available: bool):  # type: ignore[no-untyped-def]
    from app.models.db import ApplicationSetting, AuditEvent

    case_id = make_case()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, browser_enabled=True, browser_agent_token="agent-" + "x" * 40,
        browser_vnc_password="automatic-dork-test-password",  # noqa: S106 - synthetic test secret
        browser_evidence_dir=tmp_path / "browser", data_dir=tmp_path / "data",
    )
    started: list[str] = []

    class FakeBrowserClient:
        def __init__(self, configured_settings) -> None:  # type: ignore[no-untyped-def]
            pass

        async def status(self) -> BrowserAgentStatus:
            return BrowserAgentStatus(
                available=available, enabled=True, profile_ready=True, active_job=False,
                pending_job_id=None, browser_version="149.0", allowed_hosts=(),
                authenticated_sessions=(),
            )

    async def fake_queue(investigation_id: str) -> None:
        started.append(investigation_id)

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "BrowserAgentClient", FakeBrowserClient)
    monkeypatch.setattr(routes, "run_dork_queue", fake_queue)
    monkeypatch.setattr(routes, "_dork_run_sleep", no_sleep)

    async def run() -> None:
        await routes._automatic_dork_run_after_collection(case_id)
        await asyncio.sleep(0)  # let the started queue task run

    asyncio.run(run())
    with SessionLocal() as db:
        state = dork_run.load_state(db, case_id)
        events = {
            event.event_type: event.details
            for event in db.scalars(select(AuditEvent).where(AuditEvent.investigation_id == case_id))
        }
        setting = db.get(ApplicationSetting, dork_run.state_key(case_id))
        if setting is not None:
            db.delete(setting)
        case = db.get(Investigation, case_id)
        if case is not None:
            db.delete(case)
        db.commit()
    routes.dork_run_tasks.pop(case_id, None)
    return case_id, state, events, started


def test_new_case_starts_the_default_dork_run_once_collection_has_finished(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    case_id, state, events, started = _automatic_run_setup(monkeypatch, tmp_path, available=True)

    assert started == [case_id]
    assert state is not None and state["status"] == "running"
    assert 1 <= len(state["items"]) <= dork_run.DEFAULT_MAX_QUERIES
    assert events["dork_run_started"]["automatic"] is True
    assert events["dork_run_started"]["engines"] == list(dork_run.DEFAULT_ENGINES)


def test_automatic_dork_run_is_skipped_with_a_reason_when_the_browser_is_not_ready(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _case_id, state, events, started = _automatic_run_setup(monkeypatch, tmp_path, available=False)

    assert started == [] and state is None
    assert "not available" in events["automatic_dork_run_skipped"]["reason"]
