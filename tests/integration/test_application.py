import io
import json
import re
import sqlite3
import zipfile
from datetime import UTC, datetime
from html import escape
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.adapters import REGISTRY
from app.api import routes
from app.core.config import Settings, get_settings
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.db import (
    ApplicationSetting,
    AuditEvent,
    Base,
    BrowserRun,
    Finding,
    Investigation,
    LocationObservation,
    SearchTemplate,
    ToolRun,
)
from app.models.schemas import LocationEvidenceType, LocationObservationInput
from app.services.browser_agent import (
    BrowserAgentStatus,
    BrowserCaptureResult,
)
from app.services.face_matching import FaceEmbedding, FaceMatchingError
from app.services.location_collection import CollectedLocation
from app.services.tool_settings import TOOL_TIMEOUTS_SETTING_KEY
from app.services.wayback import (
    IdentityChange,
    IdentityEvolution,
    IdentityObservation,
    SavePageNowResult,
)


def test_events_reconnect_starts_with_authoritative_snapshot() -> None:
    Base.metadata.create_all(engine)
    started = datetime.now(UTC)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="SSE reconnect snapshot",
            query="snapshot-reconnect",
            query_type="username",
            selected_tools=["nexfil", "github_discovery"],
            tags=[],
            status="completed",
        )
        investigation.runs.extend(
            [
                ToolRun(
                    tool_id="nexfil",
                    display_name="Nexfil",
                    status="completed",
                    timeout_seconds=300,
                    started_at=started,
                    finished_at=started,
                    duration_ms=1000,
                ),
                ToolRun(
                    tool_id="github_discovery",
                    display_name="GitHub Public Discovery",
                    status="completed",
                    timeout_seconds=30,
                    started_at=started,
                    finished_at=started,
                    duration_ms=500,
                ),
            ]
        )
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id

    with TestClient(app) as client:
        with client.stream("GET", f"/investigations/{investigation_id}/events") as response:
            body = "".join(response.iter_text())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: snapshot" in body
    assert '"status": "completed"' in body
    assert '"tool": "nexfil"' in body
    assert '"tool": "github_discovery"' in body
    assert "event: complete" in body


def test_startup_health_and_security_headers() -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")
        ready = client.get("/readyz")
        assert response.json() == {"status": "ok"}
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
        assert ready.json() == {"status": "ready"}
        assert response.headers["x-frame-options"] == "DENY"
        csp = response.headers["content-security-policy"]
        assert "base-uri 'none'" in csp
        assert "form-action 'self'" in csp
        assert "frame-ancestors 'none'" in csp


def test_dashboard_brand_is_product_only_and_links_manual() -> None:
    with TestClient(app) as client:
        response = client.get("/")
        manual = client.get("/manual")
        download = client.get("/manual/download")

    assert response.status_code == 200
    assert "CORVUS" in response.text
    assert "OSINT Investigation Workbench" in response.text
    assert "By Nova Example" not in response.text
    assert 'href="/manual"' in response.text
    assert manual.status_code == 200
    assert manual.headers["content-type"].startswith("text/html")
    assert 'class="manual-logo"' in manual.text
    assert '<h1>Corvus Operator Manual</h1>' in manual.text
    assert 'class="manual-toc panel"' in manual.text
    assert 'href="#7-captcha-and-manual-sock-browser-workflow"' in manual.text
    assert "CAPTCHA and manual sock-browser workflow" in manual.text
    assert download.status_code == 200
    assert download.headers["content-disposition"] == (
        'attachment; filename="Corvus-Operator-Manual.md"'
    )


def test_investigation_surfaces_searxng_captcha_sock_browser_handoff() -> None:
    with SessionLocal.begin() as db:
        item = Investigation(
            title="CAPTCHA handoff",
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
            error_message=(
                "Manual action required: qwant returned a CAPTCHA/challenge. "
                "Open the sock browser and run the search manually to complete the challenge."
            ),
        )
        item.runs.append(run)
        db.add(item)
        db.flush()
        investigation_id = item.id
        run_id = run.id

    raw_dir = get_settings().data_dir / "raw" / investigation_id / run_id
    raw_dir.mkdir(parents=True, exist_ok=True)
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
        response = client.get(f"/investigations/{investigation_id}")

    assert response.status_code == 200
    assert "blocked search" in response.text
    assert "CAPTCHA" in response.text
    assert "example_person" in response.text
    assert "site:instagram.com" in response.text
    assert "Open and clear Qwant" in response.text
    assert 'data-open-browser-desktop=' in response.text


def test_dashboard_offers_update_controls_and_host_launchers() -> None:
    with TestClient(app) as client:
        dashboard = client.get("/")
        update_page = client.get("/maintenance/update")
        whats_new = client.get("/maintenance/whats-new")
        windows = client.get("/maintenance/update-launcher/windows")
        macos = client.get("/maintenance/update-launcher/macos")
        changelog = client.get("/maintenance/changelog")

    assert dashboard.status_code == 200
    assert 'name="query_type" value="phone"' in dashboard.text
    assert 'value="phone_offline"' in dashboard.text
    assert 'data-types="phone"' in dashboard.text
    assert f"Version</span><code>{get_settings().version}</code>" in dashboard.text
    assert f"Corvus {get_settings().version}" in dashboard.text
    assert 'href="/maintenance/whats-new"' in dashboard.text
    assert "What's new" in dashboard.text
    assert 'href="/maintenance/update"' in dashboard.text
    assert "Corvus Update" in dashboard.text
    assert whats_new.status_code == 200
    assert "Changes installed in this update" in whats_new.text
    assert 'href="/maintenance/changelog"' in whats_new.text
    assert "View full changelog" in whats_new.text
    assert changelog.status_code == 200
    assert "# Changelog" in changelog.text
    assert update_page.status_code == 200
    assert "Run the registered updater" in update_page.text
    assert 'href="corvus-update://run"' in update_page.text
    assert "Download repair launcher" in update_page.text
    assert 'href="/maintenance/update-launcher/windows" download' in update_page.text
    assert windows.headers["content-disposition"] == (
        'attachment; filename="Check-Corvus-Updates.cmd"'
    )
    assert "HKCU:\\Software\\Classes\\corvus-update\\shell\\open\\command" in windows.text
    assert "Corvus Daily Update" in windows.text
    assert "%LOCALAPPDATA%\\Programs\\Corvus\\app" in windows.text
    assert "%USERPROFILE%\\osint-platform" not in windows.text
    assert "if not defined UPDATER" in windows.text
    assert "-OnlyIfChanged" in windows.text
    assert macos.headers["content-disposition"] == (
        'attachment; filename="Check-Corvus-Updates.command"'
    )
    assert "$HOME/Library/Application Support/OSINT Workbench/app" in macos.text


def test_whats_new_shows_commits_from_the_installed_build_range(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    changelog_path = tmp_path / ".last-update-changes.txt"
    build_range_path = tmp_path / ".last-update-range.json"
    changelog_path.write_text(
        "OSINT Workbench changes installed:\n"
        "@@CHANGE@@\n"
        "2026-08-06\n"
        "abc1234567890\n"
        "Add dashboard changelog\n"
        "See a short plain-English summary of the newest features from the dashboard.\n"
        "@@END@@\n",
        encoding="utf-8",
    )
    build_range_path.write_text(
        '{"from_commit":"efe0face00000000000000000000000000000001",'
        '"to_commit":"efe0face00000000000000000000000000000002",'
        '"fallback_used":false}\n',
        encoding="utf-8",
    )
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        update_changelog_path=changelog_path,
        update_build_range_path=build_range_path,
    )
    monkeypatch.setattr(routes, "get_settings", lambda: settings)

    with TestClient(app) as client:
        response = client.get("/maintenance/whats-new")

    assert response.status_code == 200
    assert "Changes installed in this update" in response.text
    assert "efe0face → efe0face" in response.text
    assert "Cleaner account results" not in response.text
    assert "What's new on the dashboard" not in response.text
    assert "Technical record from the most recent update" in response.text
    assert "2026-08-06" in response.text
    assert "abc123456789" in response.text
    assert "Add dashboard changelog" in response.text


def test_completed_investigation_can_queue_account_validation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    Base.metadata.create_all(engine)
    started: list[str] = []

    def start_validation(investigation_id: str) -> bool:
        started.append(investigation_id)
        return True

    monkeypatch.setattr(routes.job_manager, "start_profile_validation", start_validation)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Validation case",
            query="validation_example",
            query_type="username",
            selected_tools=["sherlock"],
            status="completed",
        )
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}")
        assert page.status_code == 200
        assert "Validate accounts" in page.text
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/investigations/{investigation_id}/validate-profiles",
            data={"csrf": token},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].endswith("?validation=started#findings-section")
    assert started == [investigation_id]
    with SessionLocal() as db:
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "profile_validation_requested",
            )
        )
        assert event is not None
        assert event.details == {"started": True}


def test_username_case_renders_unified_accounts_entities_timeline_and_technical_leads() -> None:
    Base.metadata.create_all(engine)
    observed = datetime(2026, 8, 5, 13, 0, tzinfo=UTC)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Unified username presentation",
            query="unified_example",
            query_type="username",
            selected_tools=["maigret", "sherlock", "nexfil"],
            status="completed",
            tags=[],
        )
        rich_run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
        technical_run = ToolRun(
            tool_id="nexfil",
            display_name="Nexfil",
            status="completed",
            timeout_seconds=60,
        )
        rich = Finding(
            query_type="username",
            queried_value="unified_example",
            finding_type="profile",
            platform="GitHub",
            value="unified_example",
            canonical_url="https://github.com/unified_example",
            status="found",
            confidence=0.95,
            extra={
                "profile": {
                    "display_name": "Unified Example",
                    "account_id": "unified_example",
                    "location": "Dublin, Ireland",
                    "followers": 42,
                },
                "platform_details": [{"label": "Account ID", "value": "12345"}],
                "timeline_events": [
                    {
                        "type": "registered",
                        "label": "Account registered",
                        "timestamp": "2010-01-02T03:04:05+00:00",
                        "source_field": "created_at",
                        "raw_value": "2010-01-02T03:04:05Z",
                        "semantic": "account_creation",
                        "confidence": "high",
                        "extractor": "GitHub API",
                    }
                ],
                "source_metadata": {
                    "maigret": {
                        "platform_details": [{"label": "Account ID", "value": "12345"}],
                        "timeline_events": [
                            {
                                "type": "registered",
                                "label": "Account registered",
                                "timestamp": "2010-01-02T03:04:05+00:00",
                                "source_field": "created_at",
                                "raw_value": "2010-01-02T03:04:05Z",
                                "semantic": "account_creation",
                                "confidence": "high",
                                "extractor": "GitHub API",
                            }
                        ],
                    },
                    "sherlock": {"sherlock_unverified_claim": True},
                },
            },
            source_tool="maigret",
            source_tools=["maigret", "sherlock"],
            observed_at=observed,
        )
        technical = Finding(
            query_type="username",
            queried_value="unified_example",
            finding_type="unverified_profile_lead",
            platform="Candidate Site",
            value="unified_example",
            canonical_url="https://candidate.example/unified_example",
            status="unknown",
            confidence=0.1,
            extra={"profile": {"account_id": "unified_example"}},
            source_tool="nexfil",
            source_tools=["nexfil"],
            observed_at=observed,
        )
        investigation.runs.extend([rich_run, technical_run])
        investigation.findings.extend([rich, technical])
        rich_run.findings.append(rich)
        technical_run.findings.append(technical)
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}")
        report = client.get(f"/investigations/{investigation_id}/report")
        graph = client.get(f"/investigations/{investigation_id}/graph/data").json()

    assert page.status_code == 200
    assert 'id="unified-account-view"' in page.text
    assert "Priority accounts" in page.text
    assert "Unified Example" in page.text
    assert "Dublin, Ireland" in page.text
    assert "Account registered" in page.text
    assert "GitHub API" in page.text
    assert "Technical candidate leads" in page.text
    assert "Candidate Site" in page.text
    assert "Underlying findings" in page.text
    assert report.status_code == 200
    assert "Unified account assessment" in report.text
    assert "Combined case timeline" in report.text
    event_nodes = [node["data"] for node in graph["nodes"] if node["data"]["type"] == "event"]
    assert event_nodes[0]["label"] == "Account registered"
    assert event_nodes[0]["occurred_at"] == "2010-01-02T03:04:05+00:00"


def test_dashboard_does_not_offer_downloadable_shortcuts() -> None:
    with TestClient(app) as client:
        dashboard = client.get("/")
        workbench = client.get("/shortcuts/osint-workbench.url")
        browser = client.get("/shortcuts/osint-sock-browser.url")
        mac_browser = client.get("/shortcuts/osint-sock-browser.webloc")

    assert dashboard.status_code == 200
    assert "Desktop shortcut" not in dashboard.text
    assert "Windows shortcut" not in dashboard.text
    assert workbench.status_code == 200  # compatibility-only route for old bookmarks
    assert browser.status_code == 404
    assert mac_browser.status_code == 404


def test_tool_timeout_settings_persist_and_apply_to_new_runs(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    Base.metadata.create_all(engine)
    started: list[str] = []
    monkeypatch.setattr(routes.job_manager, "start", started.append)
    with SessionLocal.begin() as db:
        existing = db.get(ApplicationSetting, TOOL_TIMEOUTS_SETTING_KEY)
        if existing is not None:
            db.delete(existing)

    try:
        with TestClient(app) as client:
            page = client.get("/settings")
            assert page.status_code == 200
            assert "Collector timeouts" in page.text
            assert "Built-in default" in page.text
            token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
            values = {
                f"timeout_{tool_id}": (777 if tool_id == "maigret" else adapter.default_timeout)
                for tool_id, adapter in REGISTRY.items()
            }
            saved = client.post(
                "/settings/tool-timeouts",
                data={"csrf": token, "action": "save", **values},
                follow_redirects=False,
            )
            assert saved.status_code == 303
            assert saved.headers["location"] == "/settings?saved=1"

            dashboard = client.get("/")
            assert 'name="timeout_maigret"' in dashboard.text
            assert 'name="timeout_maigret" min="10" max="3600" value="777"' in dashboard.text
            token = re.search(r'name="csrf" value="([^"]+)"', dashboard.text).group(1)
            created = client.post(
                "/investigations",
                data={
                    "csrf": token,
                    "query": "timeout_settings_example",
                    "query_type": "username",
                    "tools": "maigret",
                },
                follow_redirects=False,
            )
            assert created.status_code == 303
            investigation_id = created.headers["location"].rsplit("/", 1)[-1]

            settings_page = client.get("/settings")
            token = re.search(r'name="csrf" value="([^"]+)"', settings_page.text).group(1)
            reset = client.post(
                "/settings/tool-timeouts",
                data={"csrf": token, "action": "reset"},
                follow_redirects=False,
            )
            assert reset.status_code == 303
            assert reset.headers["location"] == "/settings?reset=1"

        with SessionLocal() as db:
            run = db.scalar(
                select(ToolRun).where(
                    ToolRun.investigation_id == investigation_id,
                    ToolRun.tool_id == "maigret",
                )
            )
            assert run is not None
            assert run.timeout_seconds == 777
            assert db.get(ApplicationSetting, TOOL_TIMEOUTS_SETTING_KEY) is None
            events = list(
                db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.event_type.in_(
                            ["tool_timeout_settings_updated", "tool_timeout_settings_reset"]
                        )
                    )
                )
            )
            assert {event.event_type for event in events} >= {
                "tool_timeout_settings_updated",
                "tool_timeout_settings_reset",
            }
        assert started == [investigation_id]
    finally:
        with SessionLocal.begin() as db:
            existing = db.get(ApplicationSetting, TOOL_TIMEOUTS_SETTING_KEY)
            if existing is not None:
                db.delete(existing)


def test_tool_timeout_settings_reject_invalid_values() -> None:
    with TestClient(app) as client:
        page = client.get("/settings")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        values = {
            f"timeout_{tool_id}": adapter.default_timeout for tool_id, adapter in REGISTRY.items()
        }
        values["timeout_maigret"] = 3601
        response = client.post(
            "/settings/tool-timeouts",
            data={"csrf": token, "action": "save", **values},
            headers={"accept": "text/html"},
        )
    assert response.status_code == 422
    assert "Maigret timeout must be between 10 and 3600 seconds" in response.text


def test_managed_browser_page_handles_explicitly_disabled_client(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(_env_file=None, browser_enabled=False)  # type: ignore[call-arg]
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Browser foundation test",
            query="example_user",
            query_type="username",
            status="completed",
        )
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}/browser")
        assert page.status_code == 200
        assert "Managed investigation browser" in page.text
        assert ">disabled<" in page.text
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        rejected = client.post(
            f"/investigations/{investigation_id}/browser/captures",
            data={"csrf": token, "url": "https://google.com/"},
            headers={"accept": "text/html"},
        )
        assert rejected.status_code == 409
        assert "managed browser support is disabled" in rejected.text

    with SessionLocal() as db:
        assert not list(
            db.scalars(select(BrowserRun).where(BrowserRun.investigation_id == investigation_id))
        )


def test_managed_browser_capture_persists_hashed_evidence(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        browser_enabled=True,
        browser_agent_token="agent-" + "x" * 40,
        browser_vnc_password="office-vnc-password",  # noqa: S106 - synthetic test secret
        browser_allowed_hosts="google.com",
        browser_evidence_dir=tmp_path,
    )

    class FakeBrowserClient:
        def __init__(self, configured_settings) -> None:  # type: ignore[no-untyped-def]
            assert configured_settings is settings

        async def status(self) -> BrowserAgentStatus:
            return BrowserAgentStatus(
                available=True,
                enabled=True,
                profile_ready=True,
                active_job=False,
                pending_job_id=None,
                browser_version="123.0",
                allowed_hosts=("google.com",),
                authenticated_sessions=("instagram",),
            )

        async def capture(self, *, job_id, investigation_id, url, collector_id):  # type: ignore[no-untyped-def]
            root = tmp_path / investigation_id / job_id
            root.mkdir(parents=True)
            (root / "page.png").write_bytes(b"PNG evidence")
            (root / "page.html").write_text("<html>evidence</html>", encoding="utf-8")
            (root / "manifest.json").write_text("{}", encoding="utf-8")
            relative = f"{investigation_id}/{job_id}"
            return BrowserCaptureResult(
                status="completed",
                requested_url=url,
                final_url="https://www.google.com/",
                observed_at=datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
                page_title="Google",
                challenge_type=None,
                screenshot_reference=f"{relative}/page.png",
                html_reference=f"{relative}/page.html",
                manifest_reference=f"{relative}/manifest.json",
                screenshot_sha256="1" * 64,
                html_sha256="2" * 64,
                manifest_sha256="3" * 64,
                browser_version="123.0",
            )

    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "BrowserAgentClient", FakeBrowserClient)
    monkeypatch.setattr(routes, "validate_browser_target", lambda target, allowed: target)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Browser capture persistence",
            query="example_user",
            query_type="username",
            status="completed",
        )
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}/browser")
        assert "Log in to sock browser" in page.text
        assert escape(settings.browser_desktop_url, quote=True) in page.text
        assert "office-vnc-password" not in page.text
        assert 'data-copy-value="office-vnc-password"' not in page.text
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/investigations/{investigation_id}/browser/captures",
            data={"csrf": token, "url": "https://google.com/"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        with SessionLocal() as db:
            browser_run = db.scalar(
                select(BrowserRun).where(BrowserRun.investigation_id == investigation_id)
            )
            assert browser_run is not None
            assert browser_run.status == "completed"
            assert browser_run.manifest_sha256 == "3" * 64
            browser_run_id = browser_run.id
            events = list(
                db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.investigation_id == investigation_id,
                        AuditEvent.event_type == "browser_capture_completed",
                    )
                )
            )
            assert events[0].details["manifest_sha256"] == "3" * 64

        screenshot = client.get(
            f"/investigations/{investigation_id}/browser/captures/{browser_run_id}/screenshot"
        )
        assert screenshot.status_code == 200
        assert screenshot.content == b"PNG evidence"


def test_managed_social_capture_imports_normalised_profile_and_auth_state(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    Base.metadata.create_all(engine)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        browser_enabled=True,
        browser_agent_token="agent-" + "x" * 40,
        browser_allowed_hosts="tiktok.com",
        browser_evidence_dir=tmp_path,
    )

    class FakeBrowserClient:
        def __init__(self, configured_settings) -> None:  # type: ignore[no-untyped-def]
            assert configured_settings is settings

        async def status(self) -> BrowserAgentStatus:
            return BrowserAgentStatus(
                available=True,
                enabled=True,
                profile_ready=True,
                active_job=False,
                pending_job_id=None,
                browser_version="149.0",
                allowed_hosts=("tiktok.com",),
                authenticated_sessions=(),
            )

        async def capture(self, *, job_id, investigation_id, url, collector_id):  # type: ignore[no-untyped-def]
            assert collector_id == "browser_tiktok"
            root = tmp_path / investigation_id / job_id
            root.mkdir(parents=True)
            (root / "page.png").write_bytes(b"PNG evidence")
            (root / "page.html").write_text("<html>profile</html>", encoding="utf-8")
            (root / "manifest.json").write_text("{}", encoding="utf-8")
            relative = f"{investigation_id}/{job_id}"
            return BrowserCaptureResult(
                status="completed",
                requested_url=url,
                final_url="https://www.tiktok.com/@example",
                observed_at=datetime(2026, 8, 6, 8, 0, tzinfo=UTC),
                page_title="Example Person (@example) | TikTok",
                challenge_type=None,
                screenshot_reference=f"{relative}/page.png",
                html_reference=f"{relative}/page.html",
                manifest_reference=f"{relative}/manifest.json",
                screenshot_sha256="4" * 64,
                html_sha256="5" * 64,
                manifest_sha256="6" * 64,
                browser_version="149.0",
                extraction_kind="social_profile",
                extraction_platform="tiktok",
                authenticated_session=False,
                extracted={
                    "canonical_url": "https://www.tiktok.com/@example",
                    "embedded_state": {
                        "userInfo": {
                            "user": {
                                "uniqueId": "example",
                                "nickname": "Example Person",
                                "id": "123456",
                                "signature": "Public biography",
                            },
                            "stats": {"followerCount": 120, "followingCount": 45},
                        }
                    },
                },
            )

    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "BrowserAgentClient", FakeBrowserClient)
    monkeypatch.setattr(routes, "validate_browser_target", lambda target, allowed: target)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Managed TikTok profile",
            query="example",
            query_type="username",
            status="completed",
        )
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}/browser")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/investigations/{investigation_id}/browser/captures",
            data={"csrf": token, "url": "https://www.tiktok.com/@example"},
            follow_redirects=False,
        )
        assert response.status_code == 303

    with SessionLocal() as db:
        finding = db.scalar(
            select(Finding).where(
                Finding.investigation_id == investigation_id,
                Finding.source_tool == "browser_tiktok",
            )
        )
        assert finding is not None
        assert finding.platform == "TikTok"
        assert finding.extra["profile"]["display_name"] == "Example Person"
        assert finding.extra["profile"]["followers"] == 120
        assert finding.extra["authenticated_observation"] is False
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "browser_capture_completed",
            )
        )
        assert event is not None
        assert event.details["collector_id"] == "browser_tiktok"
        assert event.details["authenticated_session"] is False
        assert event.details["imported_findings"] == 1


def test_csrf_and_server_side_injection_validation() -> None:
    with TestClient(app) as client:
        page = client.get("/")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        rejected = client.post(
            "/investigations",
            data={
                "csrf": token,
                "query": "name; touch /tmp/pwned",
                "query_type": "username",
                "tools": "maigret",
            },
        )
        assert rejected.status_code == 422
        browser_rejected = client.post(
            "/investigations",
            data={
                "csrf": token,
                "query": "name; touch /tmp/pwned",
                "query_type": "username",
                "tools": "maigret",
            },
            headers={"accept": "text/html"},
        )
        assert browser_rejected.status_code == 422
        assert browser_rejected.headers["content-type"].startswith("text/html")
        assert "REQUEST NOT COMPLETED" in browser_rejected.text
        denied = client.post(
            "/investigations",
            data={"csrf": "wrong", "query": "name", "query_type": "username", "tools": "maigret"},
        )
        assert denied.status_code == 403


def test_username_form_can_queue_every_compatible_collector(monkeypatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(routes.job_manager, "start", started.append)
    with TestClient(app) as client:
        page = client.get("/")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            "/investigations",
            data={
                "csrf": token,
                "query": "all_tools_example",
                "query_type": "username",
                "title": "All username collectors",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    investigation_id = response.headers["location"].rsplit("/", 1)[-1]
    assert started == [investigation_id]
    with SessionLocal() as db:
        investigation = db.get(Investigation, investigation_id)
        runs = list(db.scalars(select(ToolRun).where(ToolRun.investigation_id == investigation_id)))
    expected = {
        "maigret",
        "sherlock",
        "blackbird",
        "instaloader",
        "nexfil",
        "social_analyzer",
        "tiktok_public",
        "searxng_name_search",
        "username_public_profiles",
        "snapchat_public",
        "telegram_public",
    }
    assert investigation is not None
    assert set(investigation.selected_tools) == expected
    assert {run.tool_id for run in runs} == expected


def test_username_form_persists_explicit_extended_search(monkeypatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(routes.job_manager, "start", started.append)
    with TestClient(app) as client:
        page = client.get("/")
        assert 'name="extended_username_search"' in page.text
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            "/investigations",
            data={
                "csrf": token,
                "query": "extended_example",
                "query_type": "username",
                "tools": "maigret",
                "extended_username_search": "on",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    investigation_id = response.headers["location"].rsplit("/", 1)[-1]
    assert started == [investigation_id]
    with SessionLocal() as db:
        investigation = db.get(Investigation, investigation_id)
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "investigation_created",
            )
        )
    assert investigation is not None
    assert investigation.extended_username_search is True
    assert set(investigation.selected_tools) == {"maigret", "naminter"}
    assert event is not None
    assert event.details["extended_username_search"] is True


def test_full_name_form_creates_bing_report_case_and_search_workbench(monkeypatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(routes.job_manager, "start", started.append)
    with TestClient(app) as client:
        page = client.get("/")
        assert 'name="query_type" value="name"' in page.text
        assert "Bing Name Search" in page.text
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            "/investigations",
            data={
                "csrf": token,
                "query": "Nova Example",
                "query_type": "name",
                "title": "Full-name investigation",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/investigations/")
        assert not response.headers["location"].endswith("/search-queries")
        investigation_id = response.headers["location"].split("/")[2]
        investigation_page = client.get(response.headers["location"])
        workbench = client.get(f"/investigations/{investigation_id}/search-queries")

    assert started == [investigation_id]
    assert investigation_page.status_code == 200
    assert "Bing Name Search" in investigation_page.text
    assert workbench.status_code == 200
    assert "Profiles and biographies" in workbench.text
    assert "Username and handle mentions" in workbench.text
    assert "site:linkedin.com" in workbench.text
    with SessionLocal() as db:
        investigation = db.get(Investigation, investigation_id)
        runs = list(db.scalars(select(ToolRun).where(ToolRun.investigation_id == investigation_id)))
    assert investigation is not None
    assert investigation.query == "Nova Example"
    assert investigation.query_type == "name"
    assert investigation.selected_tools == ["bing_name_search", "searxng_name_search"]
    assert [run.tool_id for run in runs] == ["bing_name_search", "searxng_name_search"]


def test_bing_name_findings_render_snippet_and_query_provenance() -> None:
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Bing name report",
            query="Example Person",
            query_type="name",
            selected_tools=["bing_name_search"],
            tags=[],
            status="completed",
        )
        run = ToolRun(
            tool_id="bing_name_search",
            display_name="Bing Name Search",
            status="completed",
            timeout_seconds=90,
        )
        finding = Finding(
            query_type="name",
            queried_value="Example Person",
            finding_type="search_result",
            platform="profiles.example.org",
            value="Example Person public profile",
            canonical_url="https://profiles.example.org/example-person",
            display_url="https://profiles.example.org/example-person",
            status="likely",
            title="Example Person public profile",
            description="Public biography and professional history from the result snippet.",
            extra={
                "engine": "bing",
                "usage_scope": "personal_non_commercial_rss_rendering",
                "query_matches": [
                    {
                        "query_id": "exact",
                        "query_label": "Exact name across the public web",
                        "query": '"Example Person"',
                    },
                    {
                        "query_id": "professional",
                        "query_label": "Professional and researcher profiles",
                        "query": '"Example Person" site:github.com',
                    },
                ],
                "profile": {},
                "provenance": [{"tool": "bing_name_search", "record_id": "example"}],
            },
            source_tool="bing_name_search",
            source_tools=["bing_name_search"],
            observed_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        )
        item.runs.append(run)
        item.findings.append(finding)
        run.findings.append(finding)
        db.add(item)
        db.flush()
        investigation_id = item.id

    with TestClient(app) as client:
        investigation_page = client.get(f"/investigations/{investigation_id}")
        report = client.get(f"/investigations/{investigation_id}/report")

    assert investigation_page.status_code == 200
    assert "Public biography and professional history" in investigation_page.text
    assert "Matched Bing queries:" in investigation_page.text
    assert "Exact name across the public web" in investigation_page.text
    assert report.status_code == 200
    assert "Public biography and professional history" in report.text
    assert "Matched Bing queries:" in report.text
    assert "Professional and researcher profiles" in report.text


def test_unverified_tool_claims_are_hidden_by_default_and_not_linked() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Unverified claim",
            query="candidate_name",
            query_type="username",
            selected_tools=["blackbird"],
            tags=[],
            status="completed",
        )
        run = ToolRun(
            tool_id="blackbird",
            display_name="Blackbird",
            status="completed",
            timeout_seconds=60,
        )
        finding = Finding(
            query_type="username",
            queried_value="candidate_name",
            finding_type="unverified_profile_lead",
            platform="Example Social",
            value="candidate_name",
            canonical_url="https://social.example/candidate_name",
            display_url="https://social.example/candidate_name",
            status="unknown",
            confidence=0.1,
            extra={"blackbird_unverified_claim": True},
            source_tool="blackbird",
            source_tools=["blackbird"],
        )
        item.findings.append(finding)
        run.findings.append(finding)
        item.runs.append(run)
        db.add(item)
        db.flush()
        investigation_id = item.id

    with TestClient(app) as client:
        response = client.get(f"/investigations/{investigation_id}")

    assert response.status_code == 200
    assert '<option value="positive" selected>Positive only</option>' in response.text
    assert "Unverified candidate URL retained in technical detail." in response.text
    assert 'href="https://social.example/candidate_name"' not in response.text


def test_profile_cards_render_explicit_image_states_without_remote_image_requests() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Profile image states",
            query="controlled_test",
            query_type="username",
            selected_tools=["maigret"],
            tags=[],
        )
        run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
        states = [
            ("No image", {"profile": {}}),
            (
                "Not retrieved",
                {"profile": {"avatar_url": "https://images.example/not-retrieved.png"}},
            ),
            (
                "Blocked",
                {
                    "profile": {"avatar_url": "http://127.0.0.1/avatar.png"},
                    "image_cache": {
                        "source_url": "http://127.0.0.1/avatar.png",
                        "error": "private or non-routable image host is blocked",
                    },
                },
            ),
        ]
        for index, (platform, extra) in enumerate(states):
            finding = Finding(
                query_type="username",
                queried_value="controlled_test",
                finding_type="profile",
                platform=platform,
                value=f"controlled-{index}",
                canonical_url=f"https://example.org/{index}",
                display_url=f"https://example.org/{index}",
                status="found",
                extra=extra,
                source_tool="maigret",
                source_tools=["maigret"],
            )
            item.findings.append(finding)
            run.findings.append(finding)
        item.runs.append(run)
        db.add(item)
        db.flush()
        investigation_id = item.id

    with TestClient(app) as client:
        response = client.get(f"/investigations/{investigation_id}")

    assert response.status_code == 200
    assert "no image reported" in response.text
    assert "profile image available on demand" in response.text
    assert "image unavailable · private or non-routable image host is blocked" in response.text
    assert 'class="avatar-placeholder blocked"' in response.text
    assert 'src="https://images.example/not-retrieved.png"' not in response.text
    assert 'src="http://127.0.0.1/avatar.png"' not in response.text
    assert 'class="profile-avatar"' not in response.text
    assert "onerror=" not in response.text
    assert ">Load image<" in response.text
    assert "Reverse search:" in response.text
    assert ">Google<" in response.text
    assert ">Yandex<" in response.text
    assert ">Bing<" in response.text
    assert ">TinEye<" in response.text
    assert 'data-avatar-reverse-base="/findings/' in response.text


def test_toolkit_recommendation_launch_is_explicit_case_scoped_and_audited() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Registry pivots",
            query="Example Holdings Limited",
            query_type="name",
            selected_tools=["bing_name"],
            tags=[],
            status="completed",
        )
        db.add(item)
        db.flush()
        investigation_id = item.id

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}")
        assert page.status_code == 200
        assert "Investigator resource recommendations" in page.text
        assert "Irish CRO / CORE" in page.text
        assert "SEC EDGAR full-text search" in page.text
        response = client.get(
            f"/investigations/{investigation_id}/toolkit/sec-edgar",
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://www.sec.gov/edgar/search/?")
    assert response.headers["referrer-policy"] == "no-referrer"
    with SessionLocal() as db:
        audit = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "toolkit_recommendation_launched",
            )
        )
        assert audit is not None
        assert audit.details["recommendation_key"] == "sec-edgar"
        assert audit.details["destination_host"] == "www.sec.gov"
        assert audit.details["search_value"] == "Example Holdings Limited"
        assert audit.details["provenance"] == "investigation.query"
        assert audit.details["jurisdiction"] == "United States"
        assert audit.details["sends_case_value"] is True


def test_reverse_image_search_requires_per_image_confirmation_and_audits_destination() -> None:
    Base.metadata.create_all(engine)
    avatar_url = "https://scontent.example.net/avatar.jpg?token=a+b&size=hd"
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Reverse image search",
            query="example_user",
            query_type="username",
            selected_tools=["instaloader"],
            tags=[],
        )
        run = ToolRun(
            tool_id="instaloader",
            display_name="Instaloader",
            status="completed",
            timeout_seconds=60,
        )
        finding = Finding(
            query_type="username",
            queried_value="example_user",
            finding_type="profile",
            platform="Instagram",
            value="example_user",
            canonical_url="https://instagram.com/example_user",
            display_url="https://instagram.com/example_user",
            status="found",
            extra={
                "profile": {"avatar_url": avatar_url},
                "image_cache": {"sha256": "a" * 64},
            },
            source_tool="instaloader",
            source_tools=["instaloader"],
        )
        item.findings.append(finding)
        run.findings.append(finding)
        item.runs.append(run)
        db.add(item)
        db.flush()
        finding_id = finding.id
        investigation_id = item.id

    with TestClient(app) as client:
        confirmation = client.get(
            f"/findings/{finding_id}/reverse-image/google", follow_redirects=False
        )
        assert confirmation.status_code == 200
        assert "Confirm reverse-image search" in confirmation.text
        assert "I approve sending this image reference" in confirmation.text
        token = re.search(r'name="csrf" value="([^"]+)"', confirmation.text).group(1)
        response = client.post(
            f"/findings/{finding_id}/reverse-image/google",
            data={"csrf": token, "approved": "on"},
            follow_redirects=False,
        )
        bing_confirmation = client.get(
            f"/findings/{finding_id}/reverse-image/bing", follow_redirects=False
        )
        bing_token = re.search(r'name="csrf" value="([^"]+)"', bing_confirmation.text).group(1)
        bing_response = client.post(
            f"/findings/{finding_id}/reverse-image/bing",
            data={"csrf": bing_token, "approved": "on"},
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://lens.google.com/uploadbyurl?")
    assert "scontent.example.net%2Favatar.jpg" in response.headers["location"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert bing_response.status_code == 200
    assert 'method="post"' in bing_response.text
    assert "https://www.bing.com/images/detail/search?" in bing_response.text
    assert 'name="imgurl"' in bing_response.text
    assert "scontent.example.net/avatar.jpg?token=a+b&amp;size=hd" in bing_response.text
    assert bing_response.headers["referrer-policy"] == "no-referrer"
    assert "form-action https://www.bing.com" in bing_response.headers["content-security-policy"]
    assert "script-src 'unsafe-inline'" in bing_response.headers["content-security-policy"]

    with SessionLocal() as db:
        audits = list(
            db.scalars(
                select(AuditEvent).where(
                    AuditEvent.investigation_id == investigation_id,
                    AuditEvent.event_type == "reverse_image_search_launched",
                )
            )
        )
        by_provider = {str(audit.details.get("provider")): audit for audit in audits}
        assert by_provider["google"].details == {
            "finding_id": finding_id,
            "provider": "google",
            "provider_label": "Google Lens",
            "platform": "Instagram",
            "avatar_sha256": "a" * 64,
            "operator_confirmed": True,
            "payload_kind": "media",
            "destination_host": "lens.google.com",
        }
        assert by_provider["bing"].details["provider_label"] == "Bing Visual Search"
        assert by_provider["bing"].details["destination_host"] == "www.bing.com"


def test_reverse_image_search_rejects_private_avatar_url() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Private avatar",
            query="example_user",
            query_type="username",
            selected_tools=["maigret"],
            tags=[],
        )
        run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
        finding = Finding(
            query_type="username",
            queried_value="example_user",
            finding_type="profile",
            platform="Example",
            value="example_user",
            canonical_url="https://example.org/example_user",
            display_url="https://example.org/example_user",
            status="found",
            extra={"profile": {"avatar_url": "http://192.168.1.63/avatar.jpg"}},
            source_tool="maigret",
            source_tools=["maigret"],
        )
        item.findings.append(finding)
        run.findings.append(finding)
        item.runs.append(run)
        db.add(item)
        db.flush()
        finding_id = finding.id

    with TestClient(app) as client:
        response = client.get(
            f"/findings/{finding_id}/reverse-image/google", follow_redirects=False
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "private or non-routable image URLs are blocked"


def test_local_face_matching_surfaces_cross_investigation_candidate_and_review(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        current = Investigation(
            title="Current face case",
            query="current_user",
            query_type="username",
            selected_tools=["instaloader"],
            tags=[],
            status="completed",
        )
        current_run = ToolRun(
            tool_id="instaloader",
            display_name="Instaloader",
            status="completed",
            timeout_seconds=60,
        )
        current_finding = Finding(
            query_type="username",
            queried_value="current_user",
            finding_type="profile",
            platform="Instagram",
            value="current_user",
            canonical_url="https://instagram.com/current_user",
            display_url="https://instagram.com/current_user",
            status="found",
            extra={
                "profile": {
                    "avatar_url": "https://images.example.org/current.jpg",
                    "display_name": "Current Person",
                },
                "image_cache": {"sha256": "1" * 64},
            },
            source_tool="instaloader",
            source_tools=["instaloader"],
        )
        current.findings.append(current_finding)
        current_run.findings.append(current_finding)
        current.runs.append(current_run)
        db.add(current)

        peer = Investigation(
            title="Earlier face case",
            query="peer_user",
            query_type="username",
            selected_tools=["maigret"],
            tags=[],
            status="completed",
        )
        peer_run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
        peer_finding = Finding(
            query_type="username",
            queried_value="peer_user",
            finding_type="profile",
            platform="Facebook",
            value="peer_user",
            canonical_url="https://facebook.com/peer_user",
            display_url="https://facebook.com/peer_user",
            status="found",
            extra={
                "profile": {
                    "avatar_url": "https://images.example.org/peer.jpg",
                    "display_name": "Peer Person",
                },
                "image_cache": {"sha256": "2" * 64},
            },
            source_tool="maigret",
            source_tools=["maigret"],
        )
        peer.findings.append(peer_finding)
        peer_run.findings.append(peer_finding)
        peer.runs.append(peer_run)
        db.add(peer)
        db.flush()
        investigation_id = current.id
        finding_id = current_finding.id
        peer_id = peer_finding.id
        peer_investigation_id = peer.id

    class FakeMatcher:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def embed(self, path: Path) -> FaceEmbedding:
            return FaceEmbedding(
                vector=(1.0, 0.0),
                detection_confidence=0.99,
                bounding_box=(1, 2, 30, 40),
                faces_detected=1,
            )

    async def fake_retained_avatar_path(  # type: ignore[no-untyped-def]
        finding, *, settings, cache
    ) -> tuple[Path, dict[str, str]]:
        if finding.id not in {finding_id, peer_id}:
            raise FaceMatchingError("fixture candidate skipped")
        return Path("fixture-images") / f"{finding.id}.jpg", {
            "source_url": finding.extra["profile"]["avatar_url"],
            "sha256": finding.extra["image_cache"]["sha256"],
        }

    monkeypatch.setattr(routes, "FaceMatcher", FakeMatcher)
    monkeypatch.setattr(routes, "_retained_avatar_path", fake_retained_avatar_path)

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/investigations/{investigation_id}/match-faces",
            data={"csrf": token, "threshold": "0.45"},
            follow_redirects=False,
        )
        rendered = client.get(f"/investigations/{investigation_id}")

        assert response.status_code == 303
        assert "Face similarity candidate · 100.0%" in rendered.text
        assert "Peer Person" in rendered.text
        assert "Facebook" in rendered.text
        assert "cross-investigation" in rendered.text
        assert "Similarity is a model score, not an identity probability." in rendered.text
        assert f"/investigations/{peer_investigation_id}" in rendered.text

        review_token = re.search(r'name="csrf" value="([^"]+)"', rendered.text).group(1)
        review = client.post(
            f"/investigations/{investigation_id}/face-match/{finding_id}/{peer_id}",
            data={"csrf": review_token, "decision": "accepted"},
            follow_redirects=False,
        )
        assert review.status_code == 303

    with SessionLocal() as db:
        stored = db.get(Finding, finding_id)
        assert stored is not None
        matches = stored.extra["face_matching"]["matches"]
        match = next(item for item in matches if item["peer_id"] == peer_id)
        assert match["similarity"] == 1.0
        assert match["decision"] == "accepted"
        completed = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "local_face_matching_completed",
            )
        )
        reviewed = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "face_match_reviewed",
            )
        )
        assert completed is not None
        assert completed.details["model"] == "OpenCV YuNet + SFace"
        assert completed.details["matches"] >= 1
        assert reviewed is not None
        assert reviewed.details["decision"] == "accepted"


def test_location_intelligence_promotes_declared_location_and_records_fitness_evidence() -> None:
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Location intelligence",
            query="johnsmith",
            query_type="username",
            selected_tools=["maigret"],
            tags=[],
        )
        run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
        finding = Finding(
            query_type="username",
            queried_value="johnsmith",
            finding_type="profile",
            platform="Example Social",
            value="johnsmith",
            canonical_url="https://example.org/johnsmith",
            display_url="https://example.org/johnsmith",
            status="found",
            extra={"profile": {"location": "Dublin"}},
            source_tool="maigret",
            source_tools=["maigret"],
            observed_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
        )
        item.runs.append(run)
        item.findings.append(finding)
        run.findings.append(finding)
        db.add(item)
        db.flush()
        investigation_id = item.id

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}/locations")
        assert page.status_code == 200
        assert "Dublin" in page.text
        assert "declared" in page.text
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/investigations/{investigation_id}/locations",
            data={
                "csrf": token,
                "place_name": "Phoenix Park",
                "locality": "Dublin",
                "country": "Ireland",
                "evidence_type": "fitness_activity",
                "source_platform": "Strava",
                "source_url": "https://strava.com/activities/123?utm_source=test",
                "occurred_at": "2026-07-01T07:30",
                "privacy_masked": "on",
                "analyst_note": "Public activity; start and end hidden.",
            },
        )
        assert response.status_code == 200
        assert "Phoenix Park" in response.text
        assert "privacy masked" in response.text
        assert "75%" in response.text

    with SessionLocal() as db:
        observations = list(
            db.scalars(
                select(LocationObservation).where(
                    LocationObservation.investigation_id == investigation_id
                )
            )
        )
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "location_observation_added",
            )
        )

    assert {observation.place_name for observation in observations} == {"Dublin", "Phoenix Park"}
    fitness = next(item for item in observations if item.place_name == "Phoenix Park")
    assert fitness.evidence_type == "fitness_activity"
    assert fitness.privacy_masked is True
    assert fitness.source_url == "https://strava.com/activities/123"
    assert event is not None
    assert event.details["source_platform"] == "Strava"


def test_public_location_collector_imports_structured_activity_and_audits(monkeypatch) -> None:
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Collected location",
            query="runner42",
            query_type="username",
            selected_tools=[],
            tags=[],
        )
        db.add(item)
        db.flush()
        investigation_id = item.id

    class FakeCollector:
        def __init__(self, **kwargs) -> None:
            assert kwargs["respect_robots"] is True

        async def collect(self, url: str) -> list[CollectedLocation]:
            assert url == "https://www.strava.com/activities/123"
            return [
                CollectedLocation(
                    LocationObservationInput(
                        place_name="Phoenix Park",
                        locality="Dublin",
                        country="Ireland",
                        latitude=53.355,
                        longitude=-6.329,
                        precision_radius_m=1000,
                        evidence_type=LocationEvidenceType.FITNESS_ACTIVITY,
                        occurred_at=datetime(2026, 6, 1, 7, 30, tzinfo=UTC),
                        source_platform="Strava",
                        source_url=url,
                        account_identifier="runner42",
                        privacy_masked=True,
                        raw_evidence={"collector": "structured_public_page"},
                    ),
                    "exerciseaction",
                )
            ]

    monkeypatch.setattr(routes, "PublicLocationCollector", FakeCollector)
    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}/locations")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/investigations/{investigation_id}/locations/collect",
            data={
                "csrf": token,
                "source_url": "https://www.strava.com/activities/123",
                "respect_robots": "on",
            },
        )

    assert response.status_code == 200
    assert "Collection complete" in response.text
    assert "1 observations processed" in response.text
    assert "Phoenix Park" in response.text
    assert "privacy masked" in response.text

    with SessionLocal() as db:
        observation = db.scalar(
            select(LocationObservation).where(
                LocationObservation.investigation_id == investigation_id,
                LocationObservation.source_platform == "Strava",
            )
        )
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "location_public_collection",
            )
        )
    assert observation is not None
    assert observation.place_name == "Phoenix Park"
    assert observation.privacy_masked is True
    assert event is not None
    assert event.details["observations_processed"] == 1
    assert event.details["robots_policy"] == "respected"


def test_detected_email_overrides_stale_type_and_defaults_complete_workflow(monkeypatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(routes.job_manager, "start", started.append)
    query = "detected-email-defaults@example.org"

    with TestClient(app) as client:
        page = client.get("/")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        assert re.search(r'name="tools" value="maigret" checked', page.text)
        assert re.search(r'name="tools" value="sherlock" checked', page.text)
        assert re.search(r'name="tools" value="blackbird" checked', page.text)
        assert re.search(r'name="tools" value="holehe" checked', page.text)
        response = client.post(
            "/investigations",
            data={
                "csrf": token,
                "query": query,
                "query_type": "username",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    with SessionLocal() as db:
        investigation = db.scalar(select(Investigation).where(Investigation.query == query))
        assert investigation is not None
        assert investigation.query_type == "email"
        assert sorted(investigation.selected_tools) == ["blackbird", "gravatar_public", "holehe", "pgp_public"]
        assert investigation.auto_follow_username_pivots is True
        assert sorted(run.tool_id for run in investigation.runs) == ["blackbird", "gravatar_public", "holehe", "pgp_public"]
        assert started == [investigation.id]


def test_email_investigation_can_opt_into_automatic_username_pivots(monkeypatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(routes.job_manager, "start", started.append)
    query = "pivot-setting-test@example.org"

    with TestClient(app) as client:
        page = client.get("/")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            "/investigations",
            data={
                "csrf": token,
                "query": query,
                "query_type": "email",
                "tools": "blackbird",
                "auto_follow_username_pivots": "on",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    with SessionLocal() as db:
        investigation = db.scalar(select(Investigation).where(Investigation.query == query))
        assert investigation is not None
        assert investigation.auto_follow_username_pivots is True
        assert started == [investigation.id]


def test_analyst_can_follow_discovered_username_inside_email_case(monkeypatch) -> None:
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Username pivot case",
            query="pivot-source@example.org",
            query_type="email",
            selected_tools=["blackbird"],
            tags=[],
            status="completed",
        )
        run = ToolRun(
            tool_id="blackbird",
            display_name="Blackbird",
            status="completed",
            timeout_seconds=60,
        )
        finding = Finding(
            query_type="email",
            queried_value="pivot-source@example.org",
            finding_type="profile",
            platform="GitHub",
            value="GitHub account",
            status="found",
            extra={"profile": {"account_id": "johnsmith"}},
            source_tool="blackbird",
            source_tools=["blackbird"],
            observed_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
        )
        item.runs.append(run)
        item.findings.append(finding)
        run.findings.append(finding)
        db.add(item)
        db.flush()
        investigation_id = item.id
        finding_id = finding.id

    started: list[str] = []
    monkeypatch.setattr(routes.job_manager, "start", started.append)
    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}")
        assert "@johnsmith" in page.text
        assert "Follow username" in page.text
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/investigations/{investigation_id}/pivots/username",
            data={
                "csrf": token,
                "username": "johnsmith",
                "source_finding_id": finding_id,
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert started == [investigation_id]
    with SessionLocal() as db:
        investigation = db.get(Investigation, investigation_id)
        pivot_runs = list(
            db.scalars(
                select(ToolRun).where(
                    ToolRun.investigation_id == investigation_id,
                    ToolRun.query_type == "username",
                    ToolRun.query_value == "johnsmith",
                )
            )
        )
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "username_pivot_queued",
            )
        )

    assert investigation is not None
    assert investigation.status == "queued"
    assert {run.tool_id for run in pivot_runs} == {
        "maigret",
        "sherlock",
        "blackbird",
        "instaloader",
        "nexfil",
        "social_analyzer",
        "username_public_profiles",
        "snapchat_public",
        "telegram_public",
        "tiktok_public",
    }
    assert {run.pivot_source_finding_id for run in pivot_runs} == {finding_id}
    assert not any(run.pivot_automatic for run in pivot_runs)
    assert event is not None
    assert event.details["username"] == "johnsmith"
    assert event.details["pivot_depth"] == 1


def test_analyst_can_follow_discovered_email_inside_username_case(monkeypatch) -> None:
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Email pivot case",
            query="johnsmith",
            query_type="username",
            selected_tools=["instaloader"],
            tags=[],
            status="completed",
        )
        run = ToolRun(
            tool_id="instaloader",
            display_name="Instaloader",
            status="completed",
            timeout_seconds=60,
            query_value="johnsmith",
            query_type="username",
        )
        finding = Finding(
            query_type="username",
            queried_value="johnsmith",
            finding_type="profile",
            platform="Instagram",
            value="Instagram account",
            status="found",
            extra={"public_email": "person@example.org", "obfuscated_email": "pe***@example.org"},
            source_tool="instaloader",
            source_tools=["instaloader"],
            observed_at=datetime(2026, 8, 17, 10, 0, tzinfo=UTC),
        )
        item.runs.append(run)
        item.findings.append(finding)
        run.findings.append(finding)
        db.add(item)
        db.flush()
        investigation_id = item.id
        finding_id = finding.id

    started: list[str] = []
    monkeypatch.setattr(routes.job_manager, "start", started.append)
    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}")
        assert "person@example.org" in page.text
        assert "Follow email" in page.text
        assert 'name="email" value="person@example.org"' in page.text
        assert 'name="email" value="pe***@example.org"' not in page.text
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/investigations/{investigation_id}/pivots/email",
            data={
                "csrf": token,
                "email": "person@example.org",
                "source_finding_id": finding_id,
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert started == [investigation_id]
    with SessionLocal() as db:
        investigation = db.get(Investigation, investigation_id)
        pivot_runs = list(
            db.scalars(
                select(ToolRun).where(
                    ToolRun.investigation_id == investigation_id,
                    ToolRun.query_type == "email",
                    ToolRun.query_value == "person@example.org",
                )
            )
        )
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "email_pivot_queued",
            )
        )

    assert investigation is not None
    assert investigation.status == "queued"
    assert {run.tool_id for run in pivot_runs} == {"blackbird", "gravatar_public", "holehe", "pgp_public"}
    assert {run.pivot_source_finding_id for run in pivot_runs} == {finding_id}
    assert not any(run.pivot_automatic for run in pivot_runs)
    assert event is not None
    assert event.details["email"] == "person@example.org"
    assert event.details["pivot_depth"] == 1


def test_full_name_pivots_and_automatic_wayback_are_visible_without_manual_discovery() -> None:
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Automatic identity pivots",
            query="corvus_fixture_nq7az",
            query_type="username",
            selected_tools=["maigret"],
            tags=[],
            status="completed",
        )
        run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
        finding = Finding(
            query_type="username",
            queried_value="corvus_fixture_nq7az",
            finding_type="profile",
            platform="Instagram",
            value="corvus_fixture_nq7az",
            canonical_url="https://www.instagram.com/corvus_fixture_nq7az",
            status="found",
            extra={
                "profile": {"display_name": "Nova Example"},
                "wayback": {
                    "automatic_checked_at": "2026-07-20T16:00:00+00:00",
                    "target_url": "https://www.instagram.com/corvus_fixture_nq7az",
                    "capture_count": 2,
                    "materially_distinct_count": 2,
                    "error": None,
                    "first_capture": {
                        "timestamp": "20200102030405",
                        "captured_at": "2020-01-02T03:04:05+00:00",
                        "archive_url": "https://web.archive.org/web/20200102030405/https://www.instagram.com/corvus_fixture_nq7az",
                    },
                    "latest_capture": {
                        "timestamp": "20250607080910",
                        "captured_at": "2025-06-07T08:09:10+00:00",
                        "archive_url": "https://web.archive.org/web/20250607080910/https://www.instagram.com/corvus_fixture_nq7az",
                    },
                },
            },
            source_tool="maigret",
            source_tools=["maigret"],
            observed_at=datetime(2026, 7, 20, tzinfo=UTC),
        )
        item.runs.append(run)
        item.findings.append(finding)
        run.findings.append(finding)
        db.add(item)
        db.flush()
        investigation_id = item.id

    with TestClient(app) as client:
        case_page = client.get(f"/investigations/{investigation_id}")
        search_page = client.get(f"/investigations/{investigation_id}/search-queries")
        wayback_page = client.get(f"/investigations/{investigation_id}/wayback")
        report = client.get(f"/investigations/{investigation_id}/report")

    assert case_page.status_code == 200
    assert "Full-name pivots" in case_page.text
    assert "Nova Example" in case_page.text
    assert "First archived:" in case_page.text
    assert "2020-01-02" in case_page.text
    assert "2" in case_page.text
    assert "distinct captures" in case_page.text
    assert search_page.status_code == 200
    assert "Derived names:" in search_page.text
    assert "Nova Example" in search_page.text
    assert "Name + identifier: Nova Example" in search_page.text
    assert wayback_page.status_code == 200
    assert "AUTOMATIC DISCOVERY" in wayback_page.text
    assert "Archived profile coverage" in wayback_page.text
    assert "2020-01-02" in wayback_page.text
    assert "2025-06-07" in wayback_page.text
    assert report.status_code == 200
    assert "First archived" in report.text
    assert "2020-01-02" in report.text
    assert "latest distinct capture" in report.text
    assert "earliest public indexed capture found" in report.text


def test_identity_evolution_route_renders_dated_analyst_summary_and_audit(monkeypatch) -> None:
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Identity history",
            query="johnsmith",
            query_type="username",
            selected_tools=[],
            tags=[],
        )
        db.add(item)
        db.flush()
        investigation_id = item.id

    observations = (
        IdentityObservation(
            "20231212000000",
            datetime(2023, 12, 12, tzinfo=UTC),
            "https://web.archive.org/first",
            "John Smith",
            "Original biography",
            "https://example.org/avatar.jpg",
            "https://web.archive.org/avatar-one",
            "a" * 64,
            "0" * 16,
        ),
        IdentityObservation(
            "20251130000000",
            datetime(2025, 11, 30, tzinfo=UTC),
            "https://web.archive.org/latest",
            "John Smith",
            "Updated biography",
            "https://example.org/new-avatar.jpg",
            "https://web.archive.org/avatar-two",
            "b" * 64,
            "f" * 16,
        ),
    )
    evolution = IdentityEvolution(
        original_url="https://example.org/profile",
        identity_label="John Smith",
        identity_first_seen=datetime(2023, 12, 12, tzinfo=UTC),
        first_seen=datetime(2023, 12, 12, tzinfo=UTC),
        last_seen=datetime(2025, 11, 30, tzinfo=UTC),
        observations=observations,
        changes=(
            IdentityChange(
                "20251130000000",
                datetime(2025, 11, 30, tzinfo=UTC),
                "profile_picture_changed",
                "a" * 64,
                "b" * 64,
                64,
            ),
        ),
        analysed_capture_count=2,
        available_capture_count=2,
    )

    async def fake_build_identity_evolution(captures, *, image_cache, limit):
        assert [capture.timestamp for capture in captures] == [
            "20231212000000",
            "20251130000000",
        ]
        assert limit == 25
        return evolution

    monkeypatch.setattr(routes, "build_identity_evolution", fake_build_identity_evolution)
    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}/wayback")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/investigations/{investigation_id}/wayback/evolution",
            data={
                "csrf": token,
                "original_url": "https://example.org/profile",
                "timestamp": ["20231212000000", "20251130000000"],
            },
        )

    assert response.status_code == 200
    assert "John Smith" in response.text
    assert "first observed with that display name on" in response.text
    assert "2023-12-12" in response.text
    assert "Profile picture changed 1 time" in response.text
    assert "2025-11-30" in response.text

    with SessionLocal() as db:
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "wayback_identity_evolution",
            )
        )
        assert event is not None
        assert event.details["identity_label"] == "John Smith"
        assert event.details["change_types"] == ["profile_picture_changed"]


def test_save_page_now_route_is_csrf_protected_and_audited(monkeypatch) -> None:
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Archive test",
            query="example.org",
            query_type="domain",
            selected_tools=[],
            tags=[],
        )
        db.add(item)
        db.flush()
        investigation_id = item.id

    async def fake_save_page_now(value: str) -> SavePageNowResult:
        assert value == "https://example.org/profile"
        return SavePageNowResult(
            target_url=value,
            submitted_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
            status_code=302,
            archive_url="https://web.archive.org/web/20260720120000/https://example.org/profile",
            job_id="job-123",
            message="capture available",
        )

    monkeypatch.setattr(routes, "save_page_now", fake_save_page_now)
    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}/wayback")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        denied = client.post(
            f"/investigations/{investigation_id}/wayback/save",
            data={"csrf": "wrong", "target": "https://example.org/profile"},
        )
        assert denied.status_code == 403
        response = client.post(
            f"/investigations/{investigation_id}/wayback/save",
            data={"csrf": token, "target": "https://example.org/profile"},
        )
        assert response.status_code == 200
        assert "capture available" in response.text
        assert "Open saved capture" in response.text

    with SessionLocal() as db:
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "wayback_save_page_now",
            )
        )
        assert event is not None
        assert event.details["outcome"] == "submitted"
        assert event.details["job_id"] == "job-123"


def test_dashboard_and_report_preview_present_shared_case_summary() -> None:
    with SessionLocal.begin() as db:
        item = Investigation(
            title="GUI and report case",
            query="reportcase",
            query_type="username",
            selected_tools=["maigret"],
            tags=["priority"],
            status="completed",
        )
        run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
        finding = Finding(
            query_type="username",
            queried_value="reportcase",
            finding_type="profile",
            platform="GitHub",
            value="reportcase",
            canonical_url="https://github.com/reportcase",
            display_url="https://github.com/reportcase",
            status="found",
            extra={"profile": {"display_name": "Report Case"}},
            source_tool="maigret",
            source_tools=["maigret"],
            observed_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )
        item.runs.append(run)
        item.findings.append(finding)
        run.findings.append(finding)
        db.add(item)
        db.flush()
        investigation_id = item.id

    with TestClient(app) as client:
        dashboard = client.get("/")
        detail = client.get(f"/investigations/{investigation_id}")
        report = client.get(f"/investigations/{investigation_id}/report")

    assert dashboard.status_code == 200
    assert "Investigation dashboard" in dashboard.text
    assert "Collection and case options" in dashboard.text
    assert "Start full investigation" in dashboard.text
    assert "GUI and report case" in dashboard.text
    assert "<strong>1</strong> positive" in dashboard.text
    assert detail.status_code == 200
    assert "Open report preview" in detail.text
    assert "Investigation complete" in detail.text
    assert "Detailed tool runs" in detail.text
    assert "Advanced enrichment and correlation" in detail.text
    assert "Positive findings" in detail.text
    assert report.status_code == 200
    assert "content-disposition" not in report.headers
    assert "Report preview" in report.text
    assert "Executive overview" in report.text
    assert "Print / save PDF" in report.text
    assert "Report Case" in report.text


def test_auto_archiver_handoff_bundle_is_explicit_hashed_and_audited() -> None:
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Archive handoff case",
            query="archivecase",
            query_type="username",
            selected_tools=["maigret"],
            tags=[],
            status="completed",
        )
        run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
        finding = Finding(
            query_type="username",
            queried_value="archivecase",
            finding_type="profile",
            platform="Example",
            value="archivecase",
            canonical_url="https://example.org/profile#source",
            display_url="https://example.org/profile#source",
            status="found",
            extra={"profile": {}},
            source_tool="maigret",
            source_tools=["maigret"],
            observed_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        )
        item.runs.append(run)
        item.findings.append(finding)
        run.findings.append(finding)
        db.add(item)
        db.flush()
        investigation_id = item.id
        finding_id = finding.id

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}")
        assert page.status_code == 200
        assert "Auto Archiver bundle" in page.text
        csrf_token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/investigations/{investigation_id}/auto-archiver/bundle",
            data={"csrf": csrf_token, "finding_ids": finding_id},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["x-content-sha256"]
        assert "OSINT-Auto-Archiver-Archive-handoff-case" in response.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read("bundle-manifest.json"))
            assert manifest["candidate_count"] == 1
            assert manifest["candidates"][0]["finding_id"] == finding_id
            assert manifest["candidates"][0]["url"] == "https://example.org/profile"
            assert "auto-archiver==1.2.7" in archive.read("requirements.txt").decode()

    with SessionLocal() as db:
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "auto_archiver_bundle_exported",
            )
        )
        assert event is not None
        assert event.details["finding_ids"] == [finding_id]
        assert event.details["candidate_count"] == 1
        assert len(event.details["bundle_sha256"]) == 64
        assert len(event.details["manifest_sha256"]) == 64


def test_name_search_workbench_generates_bellingcat_style_variants_and_aliases() -> None:
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Name variant case",
            query="William Randolph Hearst",
            query_type="name",
            selected_tools=[],
            tags=[],
            status="completed",
        )
        db.add(item)
        db.flush()
        investigation_id = item.id

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}/search-queries")
        assert page.status_code == 200
        assert "Include deterministic name variants and transliterations" in page.text
        assert "William R Hearst" in page.text
        assert "Hearst, William" in page.text
        assert "Bellingcat's MIT-licensed Name Variant Search methodology" in page.text
        csrf_token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        generated = client.post(
            f"/investigations/{investigation_id}/search-queries/generate",
            data={
                "csrf": csrf_token,
                "engines": ["google"],
                "platform_ids": ["linkedin"],
                "document_types": ["pdf"],
                "include_core": "on",
                "include_name_variants": "on",
                "name_aliases": "Bill Hearst\nW. R. Hearst",
            },
        )
        assert generated.status_code == 200
        assert "Bill Hearst" in generated.text
        assert "W. R. Hearst" in generated.text
        assert "All variants across selected platforms" in generated.text

    with SessionLocal() as db:
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "search_query_pack_generated",
            )
        )
        assert event is not None
        assert event.details["include_name_variants"] is True
        assert event.details["name_aliases"] == ["Bill Hearst", "W. R. Hearst"]
        assert event.details["name_variant_count"] > 10


def test_google_dork_page_exposes_copyable_queries_and_direct_links() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Google dork case",
            query="johnsmith",
            query_type="username",
            selected_tools=[],
            tags=[],
            status="completed",
        )
        db.add(item)
        db.flush()
        investigation_id = item.id

    with TestClient(app) as client:
        case_page = client.get(f"/investigations/{investigation_id}")
        assert case_page.status_code == 200
        assert f'/investigations/{investigation_id}/google-dorks' in case_page.text

        page = client.get(f"/investigations/{investigation_id}/google-dorks")
        assert page.status_code == 200
        assert "Copyable Google search links" in page.text
        assert "Copy query" in page.text
        assert "Copy link" in page.text
        assert page.text.count("data-copy-value=") >= 2
        assert "johnsmith" in page.text
        assert "https://www.google.com/search?q=%22johnsmith%22" in page.text
        assert 'data-copy-value="cache:' not in page.text
        assert 'data-copy-value="related:' not in page.text
        assert "Best effort" in page.text


def test_advanced_search_workbench_templates_filters_and_signed_launch() -> None:
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Advanced search",
            query="johnsmith",
            query_type="username",
            selected_tools=[],
            tags=[],
            status="completed",
        )
        db.add(item)
        db.flush()
        investigation_id = item.id

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}/search-queries")
        assert page.status_code == 200
        assert "Search and dork builder" in page.text
        csrf_token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        created = client.post(
            f"/investigations/{investigation_id}/search-queries/templates",
            data={
                "csrf": csrf_token,
                "name": "Public profile evidence",
                "category": "Custom evidence",
                "query_template": "{quoted} {platforms} {filetypes}",
                "engines": ["google"],
                "query_types": ["username"],
            },
            follow_redirects=False,
        )
        assert created.status_code == 303

    with SessionLocal() as db:
        template = db.scalar(
            select(SearchTemplate).where(SearchTemplate.name == "Public profile evidence")
        )
        assert template is not None
        template_id = template.id

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}/search-queries")
        csrf_token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        generated = client.post(
            f"/investigations/{investigation_id}/search-queries/generate",
            data={
                "csrf": csrf_token,
                "engines": ["google"],
                "platform_ids": ["reddit"],
                "document_types": ["pdf", "pptx"],
                "from_date": "2024-01-01",
                "to_date": "2025-12-31",
                "include_core": "on",
                "template_ids": [template_id],
            },
        )
        assert generated.status_code == 200
        assert "site:reddit.com" in generated.text
        assert "filetype:pptx" in generated.text
        assert "after:2024-01-01" in generated.text
        assert "before:2025-12-31" in generated.text
        assert "Public profile evidence" in generated.text
        launch_token = re.search(r'name="token" value="([^"]+)"', generated.text).group(1)
        launched = client.post(
            f"/investigations/{investigation_id}/search-queries/launch",
            data={"csrf": csrf_token, "token": launch_token},
            follow_redirects=False,
        )
        assert launched.status_code == 303
        assert launched.headers["location"].startswith("https://www.google.com/search?")
        rejected = client.post(
            f"/investigations/{investigation_id}/search-queries/launch",
            data={"csrf": csrf_token, "token": launch_token + "x"},
        )
        assert rejected.status_code == 422
        updated = client.post(
            f"/investigations/{investigation_id}/search-queries/templates/{template_id}",
            data={
                "csrf": csrf_token,
                "name": "Updated profile evidence",
                "category": "Custom evidence",
                "query_template": "{quoted} site:example.org {date_range}",
                "engines": ["google", "bing"],
                "query_types": ["username"],
            },
            follow_redirects=False,
        )
        assert updated.status_code == 303

    with SessionLocal() as db:
        template = db.get(SearchTemplate, template_id)
        assert template is not None
        assert template.name == "Updated profile evidence"
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "search_query_launched",
            )
        )
        assert event is not None
        assert event.details["engine"] == "google"

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}/search-queries")
        csrf_token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        deleted = client.post(
            f"/investigations/{investigation_id}/search-queries/templates/{template_id}/delete",
            data={"csrf": csrf_token},
            follow_redirects=False,
        )
        assert deleted.status_code == 303
    with SessionLocal() as db:
        assert db.get(SearchTemplate, template_id) is None


def test_search_result_import_adds_leads_and_deduplicates_existing_findings() -> None:
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Search import case",
            query="johnsmith",
            query_type="username",
            selected_tools=["maigret"],
            tags=[],
            status="completed",
        )
        run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
        finding = Finding(
            query_type="username",
            queried_value="johnsmith",
            finding_type="profile",
            platform="example.org",
            value="johnsmith",
            canonical_url="https://example.org/profile",
            display_url="https://example.org/profile",
            status="found",
            extra={"profile": {}, "provenance": [{"tool": "maigret"}]},
            source_tool="maigret",
            source_tools=["maigret"],
            observed_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )
        item.runs.append(run)
        item.findings.append(finding)
        run.findings.append(finding)
        db.add(item)
        db.flush()
        investigation_id = item.id

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}/search-queries")
        assert page.status_code == 200
        csrf_token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        launch_token = re.search(r'name="token" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/investigations/{investigation_id}/search-queries/import",
            data={
                "csrf": csrf_token,
                "token": launch_token,
                "results": (
                    "https://example.org/profile\tExisting profile\tExisting snippet\n"
                    "https://new.example.net/user?utm_source=google\tImported result\tPublic snippet\n"
                    "http://127.0.0.1/private"
                ),
            },
        )
        assert response.status_code == 200
        assert "Imported 1 new results" in response.text
        assert "updated 1 existing findings" in response.text
        report = client.get(f"/investigations/{investigation_id}/report")
        assert "Imported result" in report.text
        assert "Analyst-imported" in report.text

    with SessionLocal() as db:
        findings = list(
            db.scalars(select(Finding).where(Finding.investigation_id == investigation_id))
        )
        assert len(findings) == 2
        existing = next(
            item for item in findings if item.canonical_url == "https://example.org/profile"
        )
        assert existing.source_tools == ["maigret", "search_import"]
        imported = next(item for item in findings if item.source_tool == "search_import")
        assert imported.canonical_url == "https://new.example.net/user"
        assert imported.status == "likely"
        import_run = db.scalar(
            select(ToolRun).where(
                ToolRun.investigation_id == investigation_id,
                ToolRun.tool_id == "search_import",
            )
        )
        assert import_run is not None
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "search_results_imported",
            )
        )
        assert event is not None
        assert event.details["imported_count"] == 1
        assert event.details["duplicate_count"] == 1
        assert event.details["rejected_count"] == 1


def test_search_query_searxng_capture_imports_results(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    with SessionLocal.begin() as db:
        item = Investigation(
            title="SearXNG capture case",
            query="johnsmith",
            query_type="username",
            selected_tools=[],
            tags=[],
            status="completed",
        )
        db.add(item)
        db.flush()
        investigation_id = item.id

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.org/johnsmith",
                        "title": "John Smith",
                        "content": "Public profile",
                        "engines": ["brave"],
                    }
                ]
            },
        )

    from app.services import search_searxng_capture

    original_client = httpx.AsyncClient

    def fake_async_client(*, transport: object = None, **kwargs: object) -> httpx.AsyncClient:
        del transport
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(search_searxng_capture.httpx, "AsyncClient", fake_async_client)

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}/search-queries")
        assert page.status_code == 200
        csrf_token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        token = re.search(r'name="token" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/investigations/{investigation_id}/search-queries/searxng",
            data={"csrf": csrf_token, "token": token},
        )
        assert response.status_code == 200
        assert "SearXNG returned 1 result" in response.text
        assert "imported 1 new" in response.text

    with SessionLocal() as db:
        finding = db.scalar(
            select(Finding).where(
                Finding.investigation_id == investigation_id,
                Finding.source_tool == "search_searxng_capture",
            )
        )
        assert finding is not None
        assert finding.canonical_url == "https://example.org/johnsmith"
        assert finding.title == "John Smith"
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "search_results_searxng",
            )
        )
        assert event is not None
        assert event.details["imported_count"] == 1


def test_delete_requires_confirmation_and_retains_audit_event() -> None:
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Delete test",
            query="controlled_test",
            query_type="username",
            selected_tools=["maigret"],
            tags=[],
            status="completed",
        )
        db.add(item)
        db.flush()
        investigation_id = item.id
        db.add(
            AuditEvent(
                investigation_id=investigation_id,
                event_type="sensitive_prior_event",
                details={"query": item.query},
            )
        )
    with TestClient(app) as client:
        page = client.get("/")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        assert (
            client.post(
                f"/investigations/{investigation_id}/delete",
                data={"csrf": token, "confirm": "delete"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/investigations/{investigation_id}/delete",
                data={"csrf": token, "confirm": "DELETE"},
            ).status_code
            == 200
        )
    with SessionLocal() as db:
        assert db.get(Investigation, investigation_id) is None
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "investigation_deleted",
            )
        )
        assert event is not None
        retained_events = list(
            db.scalars(select(AuditEvent).where(AuditEvent.investigation_id == investigation_id))
        )
        assert [item.event_type for item in retained_events] == ["investigation_deleted"]


def test_telegram_export_import_retains_messages_and_audits_zero_egress() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Telegram import",
            query="example_user",
            query_type="username",
            selected_tools=[],
            tags=[],
            status="completed",
        )
        db.add(item)
        db.flush()
        investigation_id = item.id

    payload = json.dumps(
        {
            "name": "Example Chat",
            "type": "public_supergroup",
            "id": -1001234567890,
            "messages": [
                {
                    "id": 1,
                    "date": "2026-08-17T09:00:00+00:00",
                    "from": "Alice",
                    "from_id": "user123",
                    "text": "Hello @bob_handle bob@example.org https://t.me/otheruser",
                },
                {
                    "id": 2,
                    "date": "2026-08-17T09:01:00+00:00",
                    "from": "Bob",
                    "from_id": "user456",
                    "reply_to_message_id": 1,
                    "text": "Reply",
                },
            ],
        }
    ).encode()

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/investigations/{investigation_id}/telegram/import",
            data={"csrf": token},
            files={"telegram_export": ("result.json", payload, "application/json")},
            follow_redirects=False,
        )

    assert response.status_code == 303
    with SessionLocal() as db:
        findings = list(db.scalars(select(Finding).where(Finding.investigation_id == investigation_id)))
        assert sum(finding.finding_type == "telegram_chat_export" for finding in findings) == 1
        assert sum(finding.finding_type == "telegram_message" for finding in findings) == 2
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "telegram_export_imported",
            )
        )
        assert event is not None
        assert event.details["network_egress"] is False
        assert event.details["messages_parsed"] == 2


def test_discord_chat_export_import_retains_messages_and_audits_zero_egress() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Discord import",
            query="example_user",
            query_type="username",
            selected_tools=[],
            tags=[],
            status="completed",
        )
        db.add(item)
        db.flush()
        investigation_id = item.id

    payload = json.dumps(
        {
            "guild": {"id": "1", "name": "Example Guild"},
            "channel": {"id": "2", "type": "GuildTextChat", "name": "general"},
            "messages": [
                {
                    "id": "100",
                    "type": "Default",
                    "timestamp": "2026-08-17T09:00:00.000+00:00",
                    "content": "Hello @bob_handle bob@example.org",
                    "author": {
                        "id": "175928847299117063",
                        "name": "alice",
                        "nickname": "Alice",
                        "avatarUrl": "https://cdn.discordapp.com/avatars/175928847299117063/avatar.png",
                        "isBot": False,
                    },
                },
                {
                    "id": "101",
                    "type": "Default",
                    "timestamp": "2026-08-17T09:01:00.000+00:00",
                    "content": "Reply",
                    "author": {"id": "175928847299117064", "name": "bob", "isBot": False},
                    "reference": {"messageId": "100"},
                },
            ],
            "messageCount": 2,
        }
    ).encode()

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/investigations/{investigation_id}/discord/import",
            data={"csrf": token},
            files={"discord_export": ("result.json", payload, "application/json")},
            follow_redirects=False,
        )

    assert response.status_code == 303
    with SessionLocal() as db:
        findings = list(db.scalars(select(Finding).where(Finding.investigation_id == investigation_id)))
        assert sum(finding.finding_type == "discord_chat_export" for finding in findings) == 1
        assert sum(finding.finding_type == "discord_message" for finding in findings) == 2
        discord_profiles = [
            finding
            for finding in findings
            if finding.finding_type == "profile" and finding.platform == "Discord"
        ]
        assert len(discord_profiles) == 2
        alice = next(finding for finding in discord_profiles if finding.value == "alice")
        assert alice.extra["profile"]["display_name"] == "Alice"
        assert alice.extra["profile"]["account_id"] == "175928847299117063"
        assert alice.extra["profile"]["avatar_url"].startswith("https://cdn.discordapp.com/")
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "discord_export_imported",
            )
        )
        assert event is not None
        assert event.details["network_egress"] is False
        assert event.details["messages_parsed"] == 2


def test_discord_sqlite_import_retains_messages_and_audits_zero_egress(tmp_path: Path) -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        item = Investigation(
            title="Discord SQLite import",
            query="example_user",
            query_type="username",
            selected_tools=[],
            tags=[],
            status="completed",
        )
        db.add(item)
        db.flush()
        investigation_id = item.id

    db_path = tmp_path / "archive.sqlite"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        create table guilds (id text primary key, name text not null, raw_json text not null, updated_at text not null);
        create table channels (id text primary key, guild_id text not null, kind text not null, name text not null, raw_json text not null, updated_at text not null);
        create table members (guild_id text not null, user_id text not null, username text not null, global_name text, display_name text, nick text, raw_json text not null, updated_at text not null, primary key (guild_id, user_id));
        create table messages (id text primary key, guild_id text not null, channel_id text not null, author_id text, message_type integer not null, created_at text not null, edited_at text, content text not null, normalized_content text not null, reply_to_message_id text, raw_json text not null, updated_at text not null);
        """
    )
    connection.execute(
        "insert into guilds (id, name, raw_json, updated_at) values ('1', 'Example Guild', '{}', '2026-08-17T09:00:00+00:00')"
    )
    connection.execute(
        "insert into channels (id, guild_id, kind, name, raw_json, updated_at) values ('2', '1', 'text', 'general', '{}', '2026-08-17T09:00:00+00:00')"
    )
    connection.execute(
        "insert into members (guild_id, user_id, username, display_name, raw_json, updated_at) values ('1', '175928847299117063', 'alice', 'Alice', '{}', '2026-08-17T09:00:00+00:00')"
    )
    connection.execute(
        "insert into messages (id, guild_id, channel_id, author_id, message_type, created_at, content, normalized_content, raw_json, updated_at) "
        "values ('100', '1', '2', '175928847299117063', 0, '2026-08-17T09:00:00+00:00', 'Hello', 'hello', '{}', '2026-08-17T09:00:00+00:00')"
    )
    connection.commit()
    connection.close()
    raw = db_path.read_bytes()

    with TestClient(app) as client:
        page = client.get(f"/investigations/{investigation_id}")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/investigations/{investigation_id}/discord/import-sqlite",
            data={"csrf": token},
            files={"discord_archive": ("archive.sqlite", raw, "application/x-sqlite3")},
            follow_redirects=False,
        )

    assert response.status_code == 303
    with SessionLocal() as db:
        findings = list(db.scalars(select(Finding).where(Finding.investigation_id == investigation_id)))
        assert sum(finding.finding_type == "discord_chat_export" for finding in findings) == 1
        assert sum(finding.finding_type == "discord_message" for finding in findings) == 1
        assert sum(
            finding.finding_type == "profile" and finding.platform == "Discord"
            for finding in findings
        ) == 1
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "discord_sqlite_imported",
            )
        )
        assert event is not None
        assert event.details["network_egress"] is False


def test_dashboard_bulk_delete_removes_only_selected_finished_cases_and_raw_evidence() -> None:
    settings = get_settings()
    with SessionLocal.begin() as db:
        first = Investigation(
            title="Bulk delete one",
            query="bulk_one",
            query_type="username",
            selected_tools=[],
            tags=[],
            status="completed",
        )
        second = Investigation(
            title="Bulk delete two",
            query="bulk_two",
            query_type="username",
            selected_tools=[],
            tags=[],
            status="failed",
        )
        active = Investigation(
            title="Keep active",
            query="bulk_active",
            query_type="username",
            selected_tools=[],
            tags=[],
            status="running",
        )
        db.add_all((first, second, active))
        db.flush()
        first_id, second_id, active_id = first.id, second.id, active.id
        db.add_all(
            (
                AuditEvent(
                    investigation_id=first_id,
                    event_type="sensitive_prior_event",
                    details={"query": first.query},
                ),
                AuditEvent(
                    investigation_id=second_id,
                    event_type="sensitive_prior_event",
                    details={"query": second.query},
                ),
            )
        )

    for investigation_id in (first_id, second_id):
        raw_dir = settings.data_dir / "raw" / investigation_id / "run-one"
        raw_dir.mkdir(parents=True)
        (raw_dir / "process-output.txt").write_text("case evidence", encoding="utf-8")

    with TestClient(app) as client:
        dashboard = client.get("/")
        token = re.search(r'name="csrf" value="([^"]+)"', dashboard.text).group(1)
        assert 'id="bulk-delete-form"' in dashboard.text
        assert "DELETE SELECTED" in dashboard.text
        rejected = client.post(
            "/investigations/delete-selected",
            data={
                "csrf": token,
                "confirm": "DELETE",
                "investigation_ids": [first_id, second_id],
            },
        )
        assert rejected.status_code == 422
        deleted = client.post(
            "/investigations/delete-selected",
            data={
                "csrf": token,
                "confirm": "DELETE SELECTED",
                "investigation_ids": [first_id, second_id],
            },
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        assert deleted.headers["location"] == "/?deleted=2"
        with SessionLocal.begin() as db:
            retained_active = db.get(Investigation, active_id)
            assert retained_active is not None
            retained_active.status = "running"
        active_rejected = client.post(
            "/investigations/delete-selected",
            data={
                "csrf": token,
                "confirm": "DELETE SELECTED",
                "investigation_ids": [active_id],
            },
        )
        assert active_rejected.status_code == 409

    assert not (settings.data_dir / "raw" / first_id).exists()
    assert not (settings.data_dir / "raw" / second_id).exists()
    with SessionLocal() as db:
        assert db.get(Investigation, first_id) is None
        assert db.get(Investigation, second_id) is None
        assert db.get(Investigation, active_id) is not None
        events = list(
            db.scalars(
                select(AuditEvent).where(
                    AuditEvent.investigation_id.in_((first_id, second_id)),
                    AuditEvent.event_type == "investigation_deleted",
                )
            )
        )
        assert len(events) == 2
        assert all(event.details["bulk_delete"] is True for event in events)
