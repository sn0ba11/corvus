import json
import re
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import routes
from app.core.config import Settings
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.db import ApplicationSetting, Base, BrowserRun, Investigation, ToolRun
from app.services.browser_agent import BrowserCaptureResult


def test_searxng_challenge_warmup_resume_retries_original_query(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    Base.metadata.create_all(engine)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        browser_enabled=True,
        browser_agent_token="agent-" + "x" * 40,
        browser_vnc_password="handoff-test-password",  # noqa: S106 - synthetic test secret
        browser_allowed_hosts="qwant.com",
        browser_evidence_dir=tmp_path / "browser",
        data_dir=tmp_path / "data",
    )
    settings.browser_evidence_dir.mkdir(parents=True)
    calls: list[str] = []

    def capture_result(*, job_id: str, investigation_id: str, url: str, manual: bool) -> BrowserCaptureResult:
        root = settings.browser_evidence_dir / investigation_id / job_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "page.png").write_bytes(b"png")
        (root / "manifest.json").write_text("{}", encoding="utf-8")
        relative = f"{investigation_id}/{job_id}"
        return BrowserCaptureResult(
            status="manual_action_required" if manual else "completed",
            requested_url=url,
            final_url=url,
            observed_at=datetime(2026, 9, 1, 15, 30, tzinfo=UTC),
            page_title="Qwant",
            challenge_type="captcha" if manual else None,
            screenshot_reference=f"{relative}/page.png",
            html_reference=None,
            manifest_reference=f"{relative}/manifest.json",
            screenshot_sha256="1" * 64,
            html_sha256=None,
            manifest_sha256="2" * 64,
            browser_version="149.0",
            extraction_kind=None if manual else "search_results",
            extraction_platform=None if manual else "qwant_search",
            extracted=(
                None
                if manual
                else {
                    "search_results": [
                        {
                            "href": "https://www.instagram.com/example_person/",
                            "title": "example_person • Instagram",
                            "snippet": "Public Instagram profile",
                        }
                    ]
                }
            ),
        )

    class FakeBrowserClient:
        def __init__(self, configured_settings) -> None:  # type: ignore[no-untyped-def]
            assert configured_settings is settings

        async def capture(self, *, job_id, investigation_id, url, collector_id):  # type: ignore[no-untyped-def]
            calls.append(url)
            return capture_result(
                job_id=job_id,
                investigation_id=investigation_id,
                url=url,
                manual=len(calls) == 1,
            )

        async def resume(self, *, job_id, investigation_id):  # type: ignore[no-untyped-def]
            warmup = "https://www.qwant.com/?q=time+in+new+york&t=web"
            return capture_result(
                job_id=job_id,
                investigation_id=investigation_id,
                url=warmup,
                manual=False,
            )

    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "BrowserAgentClient", FakeBrowserClient)

    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Stateful Qwant handoff",
            query="example_person",
            query_type="username",
            selected_tools=["searxng_name_search"],
            tags=[],
            status="completed",
        )
        run = ToolRun(
            tool_id="searxng_name_search",
            display_name="SearXNG Web Search",
            status="completed",
            timeout_seconds=90,
            error_code="manual_action_required",
        )
        investigation.runs.append(run)
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id
        run_id = run.id

    raw_dir = settings.data_dir / "raw" / investigation_id / run_id
    raw_dir.mkdir(parents=True)
    (raw_dir / "searxng-name-search.json").write_text(
        json.dumps(
            {
                "unresponsive_engines": [
                    {
                        "engine": "qwant",
                        "error": "CAPTCHA",
                        "query_id": "instagram",
                        "query": '"example_person" site:instagram.com',
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        started = client.post(
            f"/investigations/{investigation_id}/searxng-handoff/{run_id}/start",
            data={"csrf": csrf, "engine": "qwant", "query_id": "instagram"},
            follow_redirects=False,
        )
        assert started.status_code == 303

        with SessionLocal() as db:
            warmup_run = db.scalar(
                select(BrowserRun)
                .where(BrowserRun.investigation_id == investigation_id)
                .order_by(BrowserRun.created_at.desc())
            )
            assert warmup_run is not None
            assert warmup_run.status == "manual_action_required"
            assert db.get(ApplicationSetting, f"searxng_handoff:{warmup_run.id}") is not None
            warmup_run_id = warmup_run.id

        resumed = client.post(
            f"/investigations/{investigation_id}/browser/captures/{warmup_run_id}/resume",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert resumed.status_code == 303

    assert calls == [
        "https://www.qwant.com/?q=time+in+new+york&t=web",
        "https://www.qwant.com/?q=%22example_person%22+site%3Ainstagram.com&t=web",
    ]
    with SessionLocal() as db:
        assert db.get(ApplicationSetting, f"searxng_handoff:{warmup_run_id}") is None
        browser_runs = list(
            db.scalars(
                select(BrowserRun)
                .where(BrowserRun.investigation_id == investigation_id)
                .order_by(BrowserRun.created_at)
            )
        )
    assert [run.status for run in browser_runs] == ["completed", "completed"]
    assert browser_runs[1].requested_url == calls[1]


def test_exact_search_challenge_resumes_in_place_without_duplicate_rerun(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    Base.metadata.create_all(engine)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        browser_enabled=True,
        browser_agent_token="agent-" + "y" * 40,
        browser_vnc_password="handoff-test-password",  # noqa: S106 - synthetic test secret
        browser_allowed_hosts="qwant.com",
        browser_evidence_dir=tmp_path / "browser",
        data_dir=tmp_path / "data",
    )
    settings.browser_evidence_dir.mkdir(parents=True)
    capture_calls: list[str] = []
    resumed_jobs: list[str] = []
    run_urls: dict[str, str] = {}

    def capture_result(
        *, job_id: str, investigation_id: str, url: str, manual: bool
    ) -> BrowserCaptureResult:
        root = settings.browser_evidence_dir / investigation_id / job_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "page.png").write_bytes(b"png")
        (root / "manifest.json").write_text("{}", encoding="utf-8")
        relative = f"{investigation_id}/{job_id}"
        return BrowserCaptureResult(
            status="manual_action_required" if manual else "completed",
            requested_url=url,
            final_url=url,
            observed_at=datetime(2026, 9, 2, 9, 30, tzinfo=UTC),
            page_title="Qwant",
            challenge_type="captcha" if manual else None,
            screenshot_reference=f"{relative}/page.png",
            html_reference=None,
            manifest_reference=f"{relative}/manifest.json",
            screenshot_sha256="3" * 64,
            html_sha256=None,
            manifest_sha256="4" * 64,
            browser_version="151.0",
            extraction_kind=None if manual else "search_results",
            extraction_platform=None if manual else "qwant_search",
            extracted=(
                None
                if manual
                else {
                    "search_results": [
                        {
                            "href": "https://www.instagram.com/example_person/",
                            "title": "example_person • Instagram",
                            "snippet": "Public Instagram profile",
                        }
                    ]
                }
            ),
        )

    class FakeBrowserClient:
        def __init__(self, configured_settings) -> None:  # type: ignore[no-untyped-def]
            assert configured_settings is settings

        async def capture(self, *, job_id, investigation_id, url, collector_id):  # type: ignore[no-untyped-def]
            capture_calls.append(url)
            run_urls[job_id] = url
            # Warm-up and then the exact original search are both challenged.
            return capture_result(
                job_id=job_id,
                investigation_id=investigation_id,
                url=url,
                manual=True,
            )

        async def resume(self, *, job_id, investigation_id):  # type: ignore[no-untyped-def]
            resumed_jobs.append(job_id)
            return capture_result(
                job_id=job_id,
                investigation_id=investigation_id,
                url=run_urls[job_id],
                manual=False,
            )

    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "BrowserAgentClient", FakeBrowserClient)

    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Exact search challenge",
            query="example_person",
            query_type="username",
            selected_tools=["searxng_name_search"],
            tags=[],
            status="completed",
        )
        run = ToolRun(
            tool_id="searxng_name_search",
            display_name="SearXNG Web Search",
            status="completed",
            timeout_seconds=90,
            error_code="manual_action_required",
        )
        investigation.runs.append(run)
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id
        run_id = run.id

    raw_dir = settings.data_dir / "raw" / investigation_id / run_id
    raw_dir.mkdir(parents=True)
    (raw_dir / "searxng-name-search.json").write_text(
        json.dumps(
            {
                "unresponsive_engines": [
                    {
                        "engine": "qwant",
                        "error": "CAPTCHA",
                        "query_id": "instagram",
                        "query": '"example_person" site:instagram.com',
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        started = client.post(
            f"/investigations/{investigation_id}/searxng-handoff/{run_id}/start",
            data={"csrf": csrf, "engine": "qwant", "query_id": "instagram"},
            follow_redirects=False,
        )
        assert started.status_code == 303

        with SessionLocal() as db:
            warmup = db.scalar(
                select(BrowserRun)
                .where(BrowserRun.investigation_id == investigation_id)
                .order_by(BrowserRun.created_at.desc())
            )
            assert warmup is not None
            warmup_id = warmup.id

        first_resume = client.post(
            f"/investigations/{investigation_id}/browser/captures/{warmup_id}/resume",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert first_resume.status_code == 303

        with SessionLocal() as db:
            runs = list(
                db.scalars(
                    select(BrowserRun)
                    .where(BrowserRun.investigation_id == investigation_id)
                    .order_by(BrowserRun.created_at)
                )
            )
            assert [item.status for item in runs] == ["completed", "manual_action_required"]
            exact_run = runs[1]
            handoff = db.get(ApplicationSetting, f"searxng_handoff:{exact_run.id}")
            assert handoff is not None
            assert handoff.value["phase"] == "original"
            assert handoff.value["query"] == '"example_person" site:instagram.com'
            exact_run_id = exact_run.id

        second_resume = client.post(
            f"/investigations/{investigation_id}/browser/captures/{exact_run_id}/resume",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert second_resume.status_code == 303

    assert len(capture_calls) == 2
    assert capture_calls[0] == "https://www.qwant.com/?q=time+in+new+york&t=web"
    assert capture_calls[1] == (
        "https://www.qwant.com/?q=%22example_person%22+site%3Ainstagram.com&t=web"
    )
    assert resumed_jobs == [warmup_id, exact_run_id]
    with SessionLocal() as db:
        assert db.get(ApplicationSetting, f"searxng_handoff:{exact_run_id}") is None
        exact_run = db.get(BrowserRun, exact_run_id)
        assert exact_run is not None and exact_run.status == "completed"
