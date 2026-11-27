import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import routes
from app.core.config import Settings
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.db import ApplicationSetting, Base, BrowserRun, Finding, Investigation, ToolRun
from app.services import capture_run
from app.services.browser_agent import BrowserAgentStatus, BrowserCaptureResult

PROFILE_URLS = (
    "https://www.instagram.com/example_person",
    "https://www.instagram.com/second_person",
    "https://www.tiktok.com/@example_person",
)


CREATED_CASES: list[str] = []


@pytest.fixture(autouse=True)
def _remove_created_cases():  # type: ignore[no-untyped-def]
    """The test database is shared; leave no look-alike profiles for other tests to reconcile."""
    yield
    with SessionLocal() as db:
        for case_id in CREATED_CASES:
            case = db.get(Investigation, case_id)
            if case is not None:
                db.delete(case)
        db.commit()
    CREATED_CASES.clear()


def make_case(extra_findings: tuple[tuple[str, str, str], ...] = ()) -> str:
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        case = Investigation(title="Capture run", query="example_person", query_type="username")
        db.add(case)
        db.flush()
        run = ToolRun(investigation_id=case.id, tool_id="maigret", display_name="Maigret",
                      status="completed", timeout_seconds=60)
        db.add(run)
        db.flush()
        rows = [(url, "profile", "found") for url in PROFILE_URLS] + list(extra_findings)
        for index, (url, finding_type, status) in enumerate(rows):
            db.add(Finding(
                investigation_id=case.id, tool_run_id=run.id, query_type="username", queried_value="example_person",
                finding_type=finding_type, platform="x", value=f"v{index}", canonical_url=url,
                display_url=url, status=status, source_tool="maigret", source_tools=["maigret"],
                extra={"profile": {}},
            ))
        db.commit()
        CREATED_CASES.append(case.id)
        return case.id


def test_plan_takes_only_uncaptured_account_pages_and_alternates_platforms() -> None:
    case_id = make_case((
        ("https://www.instagram.com/p/Cabc123/", "profile", "found"),          # a post, not an account
        ("https://www.instagram.com/absent_person", "profile", "not_found"),    # not found
        ("https://www.instagram.com/stranger", "search_result", "likely"),      # another handle's lead
        ("https://www.tiktok.com/@example_person/", "search_result", "likely"),  # duplicate of a profile
        ("https://example.org/example_person", "profile", "found"),             # no browser reader
    ))
    with SessionLocal() as db:
        case = db.get(Investigation, case_id)
        assert [item["url"] for item in capture_run.capture_candidates(case)] == [
            PROFILE_URLS[0], PROFILE_URLS[1], PROFILE_URLS[2]]
        plan = capture_run.plan_capture_run(case, max_pages=5)
        assert [item["platform"] for item in plan] == ["Instagram", "TikTok", "Instagram"]
        # A capture that loaded the page but read nothing leaves it in the list for a retry...
        db.add(BrowserRun(investigation_id=case_id, requested_url=PROFILE_URLS[0] + "/",
                          collector_id="browser_instagram", status="completed"))
        db.commit()
        db.refresh(case)
        assert PROFILE_URLS[0] in [item["url"] for item in capture_run.capture_candidates(case)]
        # ...and a page whose profile the browser has read is not opened again.
        finding = next(f for f in case.findings if f.canonical_url == PROFILE_URLS[0])
        finding.extra = {**finding.extra, "source_metadata": {"browser_instagram": {"managed_browser": True}}}
        db.commit()
        db.refresh(case)
        assert PROFILE_URLS[0] not in [item["url"] for item in capture_run.capture_candidates(case)]


def test_exact_unverified_instagram_and_pinterest_profiles_are_browser_candidates() -> None:
    case_id = make_case((
        ('https://www.instagram.com/example_person/', 'unverified_profile_lead', 'unknown'),
        ('https://www.pinterest.com/example_person/', 'unverified_profile_lead', 'unknown'),
        ('https://www.threads.com/@example_person', 'profile', 'found'),
        ('https://www.pinterest.com/stranger/', 'unverified_profile_lead', 'unknown'),
    ))
    with SessionLocal() as db:
        case = db.get(Investigation, case_id)
        assert case is not None
        candidates = capture_run.capture_candidates(case)
        assert any(row['platform'] == 'Pinterest' and row['handle'] == 'example_person' for row in candidates)
        assert any(row['platform'] == 'Threads' and row['handle'] == 'example_person' for row in candidates)
        assert not any(row['handle'] == 'stranger' for row in candidates)


def test_a_platform_that_twice_returns_nothing_is_dropped() -> None:
    items = [{"url": f"https://www.instagram.com/u{index}", "platform": "Instagram", "handle": f"u{index}"}
             for index in range(4)]
    state = capture_run.new_state(items, 30, 75)
    state["items"][0]["status"] = "no_profile"
    assert not capture_run.platform_keeps_returning_nothing(state, "Instagram")
    state["items"][1]["status"] = "no_profile"
    assert capture_run.platform_keeps_returning_nothing(state, "Instagram")
    capture_run.cool_platform(state, "Instagram", "wants a sign-in")
    assert [item["status"] for item in state["items"]] == ["no_profile", "no_profile", "skipped", "skipped"]
    # One page read in between means the platform is answering; keep going.
    state = capture_run.new_state(items, 30, 75)
    for index, status in enumerate(("no_profile", "completed", "no_profile")):
        state["items"][index]["status"] = status
    assert not capture_run.platform_keeps_returning_nothing(state, "Instagram")

def test_queue_imports_profiles_and_drops_a_platform_that_asks_for_sign_in(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    case_id = make_case()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, browser_enabled=True, browser_agent_token="agent-" + "x" * 40,
        browser_vnc_password="capture-run-test-password",  # noqa: S106 - synthetic test secret
        browser_evidence_dir=tmp_path / "browser", data_dir=tmp_path / "data",
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
            walled = "instagram.com" in url
            root = settings.browser_evidence_dir / investigation_id / job_id
            root.mkdir(parents=True, exist_ok=True)
            (root / "page.png").write_bytes(b"png")
            (root / "manifest.json").write_text("{}", encoding="utf-8")
            relative = f"{investigation_id}/{job_id}"
            return BrowserCaptureResult(
                status="manual_action_required" if walled else "completed",
                requested_url=url, final_url=url,
                observed_at=datetime(2026, 11, 13, 12, 0, tzinfo=UTC), page_title="Profile",
                challenge_type="login" if walled else None,
                screenshot_reference=f"{relative}/page.png", html_reference=None,
                manifest_reference=f"{relative}/manifest.json", screenshot_sha256="1" * 64,
                html_sha256=None, manifest_sha256="2" * 64, browser_version="149.0",
                extraction_kind=None if walled else "social_profile",
                extraction_platform=None if walled else "tiktok",
                extracted=None if walled else {
                    "url": url, "meta": {"og:title": "Example Person (@example_person) | TikTok"},
                    "profile_objects": [{"uniqueId": "example_person", "nickname": "Example Person",
                                         "signature": "Public bio", "followerCount": 321}],
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
        plan = capture_run.plan_capture_run(case)
        capture_run.save_state(db, case_id, capture_run.new_state(plan, 30, 75))
        db.commit()

    asyncio.run(routes.run_capture_queue(case_id))

    with SessionLocal() as db:
        state = capture_run.load_state(db, case_id)
        tiktok = db.scalars(select(Finding).where(
            Finding.investigation_id == case_id,
            Finding.canonical_url == "https://www.tiktok.com/@example_person")).all()
    assert state["status"] == "completed"
    # Instagram asked for a sign-in on its first page: released, and its second page never opened.
    assert [(item["platform"], item["status"]) for item in state["items"]] == [
        ("Instagram", "challenged"), ("TikTok", "completed"), ("Instagram", "skipped")]
    assert state["cooled_platforms"] == ["Instagram"]
    assert captures == [PROFILE_URLS[0], PROFILE_URLS[2]] and len(abandoned) == 1
    assert any((finding.extra.get("profile") or {}).get("followers") == 321 for finding in tiktok)
    assert pauses and all(30 <= pause <= 75 for pause in pauses)


def test_browser_page_offers_the_run_and_case_deletion_removes_its_state(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    case_id = make_case()
    started: list[str] = []

    async def fake_queue(investigation_id: str) -> None:
        started.append(investigation_id)

    monkeypatch.setattr(routes, "run_capture_queue", fake_queue)
    monkeypatch.setattr(routes, "get_settings", lambda: Settings(  # type: ignore[call-arg]
        _env_file=None, browser_enabled=True, browser_agent_token="agent-" + "x" * 40,
        browser_vnc_password="capture-run-test-password",  # noqa: S106 - synthetic test secret
    ))
    with TestClient(app) as client:
        page = client.get(f"/investigations/{case_id}/browser")
        assert "Capture found profiles" in page.text and "3 found profile pages not yet captured" in page.text
        import re
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(f"/investigations/{case_id}/capture-run",
                               data={"csrf": token, "max_pages": "2"}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].endswith("/browser#capture-run")
        assert started == [case_id]
        with SessionLocal() as db:
            state = capture_run.load_state(db, case_id)
            assert state is not None and len(state["items"]) == 2
        client.post(f"/investigations/{case_id}/capture-run/cancel", data={"csrf": token})
        with SessionLocal() as db:
            assert capture_run.load_state(db, case_id)["status"] in {"cancelled", "running"}
            routes._discard_dork_run(db, case_id)
            db.commit()
            assert db.get(ApplicationSetting, capture_run.state_key(case_id)) is None


@pytest.mark.parametrize("code", ["session_required", "target_not_allowed"])
def test_queue_drops_a_platform_the_browser_cannot_read_and_carries_on(tmp_path, monkeypatch, code) -> None:  # type: ignore[no-untyped-def]
    from app.services.browser_agent import BrowserAgentError

    case_id = make_case()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, browser_enabled=True, browser_agent_token="agent-" + "x" * 40,
        browser_vnc_password="capture-run-test-password",  # noqa: S106 - synthetic test secret
        browser_evidence_dir=tmp_path / "browser", data_dir=tmp_path / "data",
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
            # Every page fails the same way: two in a row must not be read as a browser fault.
            raise BrowserAgentError(code, "the browser cannot read this platform")

        async def abandon(self, *, job_id, investigation_id):  # type: ignore[no-untyped-def]
            raise AssertionError("nothing is held open after a refused capture")

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "BrowserAgentClient", FakeBrowserClient)
    monkeypatch.setattr(routes, "_dork_run_sleep", fake_sleep)
    with SessionLocal() as db:
        plan = capture_run.plan_capture_run(db.get(Investigation, case_id))
        capture_run.save_state(db, case_id, capture_run.new_state(plan, 30, 75))
        db.commit()

    asyncio.run(routes.run_capture_queue(case_id))

    with SessionLocal() as db:
        state = capture_run.load_state(db, case_id)
    assert state["status"] == "completed"
    assert [(item["platform"], item["status"]) for item in state["items"]] == [
        ("Instagram", "skipped"), ("TikTok", "skipped"), ("Instagram", "skipped")]
    assert sorted(state["cooled_platforms"]) == ["Instagram", "TikTok"]
    # One attempt per platform; Instagram's second page was never opened.
    assert captures == [PROFILE_URLS[0], PROFILE_URLS[2]]
    assert all(item["detail"] for item in state["items"])
