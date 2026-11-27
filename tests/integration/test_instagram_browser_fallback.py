"""A failed Instaloader API login check uses the existing authenticated browser reader."""
import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.api import routes
from app.core.config import Settings
from app.db.session import SessionLocal, engine
from app.models.db import AuditEvent, Base, BrowserRun, Finding, Investigation
from app.services import jobs
from app.services.browser_agent import BrowserAgentStatus, BrowserCaptureResult


@pytest.mark.parametrize("mode", ("completed", "challenged", "busy", "no_session"))
def test_instaloader_fallback_uses_public_browser_without_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        case = Investigation(
            title="Instagram fallback", query="example_person", query_type="username",
        )
        db.add(case)
        db.flush()
        case_id = case.id
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        browser_enabled=True,
        browser_agent_token="test-" + "x" * 40,
        browser_evidence_dir=tmp_path / "browser",
        data_dir=tmp_path / "data",
    )
    settings.browser_evidence_dir.mkdir(parents=True)
    calls: list[str] = []

    class FakeBrowser:
        def __init__(self, value: Settings) -> None:
            assert value is settings

        async def status(self) -> BrowserAgentStatus:
            return BrowserAgentStatus(
                available=True, enabled=True, profile_ready=True,
                active_job=mode == "busy", pending_job_id=None,
                browser_version="149", allowed_hosts=("instagram.com",),
                authenticated_sessions=() if mode == "no_session" else ("instagram",),
            )

        async def capture(self, *, job_id: str, investigation_id: str,
                          url: str, collector_id: str) -> BrowserCaptureResult:
            assert investigation_id == case_id
            assert collector_id == "browser_instagram"
            assert url == "https://www.instagram.com/example_person/"
            calls.append("capture")
            root = settings.browser_evidence_dir / investigation_id / job_id
            root.mkdir(parents=True)
            (root / "page.png").write_bytes(b"png")
            (root / "manifest.json").write_text("{}", encoding="utf-8")
            relative = f"{investigation_id}/{job_id}"
            blocked = mode == "challenged"
            return BrowserCaptureResult(
                status="manual_action_required" if blocked else "completed",
                requested_url=url, final_url=url,
                observed_at=datetime(2026, 11, 16, tzinfo=UTC),
                page_title="Example (@example_person) • Instagram",
                challenge_type="login" if blocked else None,
                screenshot_reference=f"{relative}/page.png", html_reference=None,
                manifest_reference=f"{relative}/manifest.json",
                screenshot_sha256="1" * 64,
                html_sha256=None, manifest_sha256="2" * 64,
                browser_version="149", extraction_kind="social_profile",
                extraction_platform="instagram",
                authenticated_session=not blocked,
                extracted={"url": url, "meta": {
                    "og:title": "Example Person (@example_person) • Instagram",
                    "og:description": "Example Person • Instagram profile",
                }, "profile_objects": [{
                    "username": "example_person", "full_name": "Example Person",
                    "biography": "A public profile", "follower_count": 129,
                }]},
            )

        async def abandon(self, *, job_id: str, investigation_id: str) -> None:
            calls.append("abandon")

    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(jobs, "BrowserAgentClient", FakeBrowser)
    manager = jobs.JobManager()
    try:
        asyncio.run(manager._capture_instagram_browser_fallback(
            case_id, run_id="source-instaloader", username="example_person",
        ))
        with SessionLocal() as db:
            captures = list(db.scalars(select(BrowserRun).where(
                BrowserRun.investigation_id == case_id,
            )))
            evidence = list(db.scalars(select(Finding).where(
                Finding.investigation_id == case_id,
            )))
            events = list(db.scalars(select(AuditEvent).where(
                AuditEvent.investigation_id == case_id,
            )))
        if mode in {"completed", "no_session"}:
            assert calls == ["capture"]
            assert len(captures) == 1 and captures[0].status == "completed"
            assert captures[0].manifest_reference is not None
            assert any(item.extra.get("managed_browser") for item in evidence)
            assert any(item.event_type == "instagram_browser_fallback_result" for item in events)
        elif mode == "challenged":
            assert calls == ["capture", "abandon"]
            assert len(captures) == 1 and captures[0].status == "cancelled"
            assert not evidence
        else:
            assert not calls and not captures
            assert any(item.event_type == "instagram_browser_fallback_skipped" for item in events)
    finally:
        with SessionLocal.begin() as db:
            case = db.get(Investigation, case_id)
            if case:
                db.delete(case)
