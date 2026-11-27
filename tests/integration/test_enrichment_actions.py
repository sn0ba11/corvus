import json
import re
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.adapters.base import ExecutionResult
from app.adapters.yt_dlp_metadata import YtDlpMetadataAdapter
from app.api import routes
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.db import Base, Finding, Investigation, ToolRun
from app.services.vies import VIESResult


def _csrf(page: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)


def _create_case() -> str:
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Enrichment action test",
            query="Example Person",
            query_type="name",
            case_reference=None,
            notes=None,
            tags=[],
            selected_tools=[],
            proxy_name=None,
            status="completed",
        )
        db.add(investigation)
        db.flush()
        return investigation.id


def _delete_case(investigation_id: str) -> None:
    with SessionLocal.begin() as db:
        db.execute(delete(Investigation).where(Investigation.id == investigation_id))


def test_vies_action_persists_official_validation(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    Base.metadata.create_all(engine)
    investigation_id = _create_case()

    async def fake_check(value: str) -> VIESResult:
        assert value == "IE6388047V"
        return VIESResult(
            vat_number="IE6388047V",
            country_code="IE",
            number="6388047V",
            valid=True,
            request_date="2026-09-11T06:00:00Z",
            request_identifier="request-1",
            name="EXAMPLE LIMITED",
            address="DUBLIN",
            trader_company_type="LTD",
            raw={"valid": True, "vatNumber": "6388047V", "countryCode": "IE"},
        )

    monkeypatch.setattr(routes, "check_vat_number", fake_check)
    try:
        with TestClient(app) as client:
            token = _csrf(client.get("/settings").text)
            monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
            response = client.post(
                f"/investigations/{investigation_id}/vies",
                data={"csrf": token, "vat_number": "IE6388047V"},
                follow_redirects=False,
            )
            assert response.status_code == 303
        with SessionLocal() as db:
            finding = db.scalar(
                select(Finding).where(
                    Finding.investigation_id == investigation_id,
                    Finding.source_record_id == "vies:IE6388047V",
                )
            )
            assert finding is not None
            assert finding.extra["valid"] is True
            assert finding.extra["registered_name"] == "EXAMPLE LIMITED"
            run = db.scalar(
                select(ToolRun).where(
                    ToolRun.investigation_id == investigation_id,
                    ToolRun.tool_id == "vies",
                )
            )
            assert run is not None
            assert run.status == "completed"
    finally:
        _delete_case(investigation_id)


def test_video_metadata_action_uses_metadata_only_adapter(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    Base.metadata.create_all(engine)
    investigation_id = _create_case()
    video_url = "https://www.youtube.com/watch?v=abc123"

    async def fake_execute(self, command, output_dir, timeout, query, log=None):  # type: ignore[no-untyped-def]
        assert self.tool_id == "yt_dlp_metadata"
        assert "--skip-download" in command
        assert "--dump-single-json" in command
        assert query == video_url
        return ExecutionResult(
            0,
            json.dumps(
                {
                    "id": "abc123",
                    "title": "Example video",
                    "description": "Public description",
                    "webpage_url": video_url,
                    "extractor_key": "Youtube",
                    "uploader": "Example Channel",
                    "duration": 42,
                    "formats": [{"url": "https://media.invalid/video"}],
                }
            ),
            "",
        )

    monkeypatch.setattr(YtDlpMetadataAdapter, "execute", fake_execute)
    try:
        with TestClient(app) as client:
            token = _csrf(client.get("/settings").text)
            monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
            response = client.post(
                f"/investigations/{investigation_id}/video-metadata",
                data={"csrf": token, "video_url": video_url},
                follow_redirects=False,
            )
            assert response.status_code == 303
        with SessionLocal() as db:
            finding = db.scalar(
                select(Finding).where(
                    Finding.investigation_id == investigation_id,
                    Finding.source_tool == "yt_dlp_metadata",
                )
            )
            assert finding is not None
            assert finding.finding_type == "video_metadata"
            assert "formats" not in finding.extra
            assert finding.extra["collector_mode"] == "metadata_only_no_media_download"
    finally:
        _delete_case(investigation_id)
