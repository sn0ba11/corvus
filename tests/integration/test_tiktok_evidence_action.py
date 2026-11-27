import hashlib
import json
import re
import zipfile
from io import BytesIO

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.adapters.base import ExecutionResult, HealthResult
from app.adapters.yt_dlp_metadata import YtDlpMetadataAdapter
from app.api import routes
from app.core.config import get_settings
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.db import Base, Finding, Investigation

URL = "https://www.tiktok.com/@scout2015/video/6718335390845095173"


def test_tiktok_acquisition_round_trip(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        case = Investigation(title="TikTok acquisition", query="scout2015", query_type="username",
                             tags=[], selected_tools=[], status="completed")
        db.add(case)
        db.flush()
        case_id = case.id

    async def fake_execute(self, command, output_dir, timeout, query, log=None):  # type: ignore[no-untyped-def]
        assert "--write-info-json" in command
        assert "--impersonate" in command
        assert query == URL
        (output_dir / "capture.mp4").write_bytes(b"\x00\x00\x00\x18ftypisom" + b"media")
        (output_dir / "capture.info.json").write_text(
            json.dumps({"id": "6718335390845095173", "title": "Sample TikTok",
                        "webpage_url": URL, "view_count": 100}), encoding="utf-8"
        )
        return ExecutionResult(0, "", "")

    async def fake_health(self):  # type: ignore[no-untyped-def]
        return HealthResult(True, "ready", "2026.10.26")

    monkeypatch.setattr(YtDlpMetadataAdapter, "execute", fake_execute)
    monkeypatch.setattr(YtDlpMetadataAdapter, "healthcheck", fake_health)
    try:
        with TestClient(app) as client:
            page = client.get("/settings")
            token = re.search(r'name="csrf" value="([^"]+)"', page.text)
            assert token
            scoped_settings = get_settings().model_copy(update={"data_dir": tmp_path})
            monkeypatch.setattr(routes, "get_settings", lambda: scoped_settings)
            post = client.post(
                f"/investigations/{case_id}/tiktok/evidence",
                data={"csrf": token.group(1), "video_url": URL},
                follow_redirects=False,
            )
            assert post.status_code == 303, post.text
            with SessionLocal() as db:
                finding = db.scalar(select(Finding).where(
                    Finding.investigation_id == case_id,
                    Finding.finding_type == "tiktok_video_evidence"))
                assert finding is not None
                assert finding.extra["media_sha256"] == hashlib.sha256(
                    b"\x00\x00\x00\x18ftypisommedia").hexdigest()
                acquisition_id = finding.extra["acquisition_id"]
                db.add(Finding(
                    investigation_id=case_id, tool_run_id=finding.tool_run_id,
                    query_type="username", queried_value="scout2015",
                    finding_type="profile", platform="TikTok", value="scout2015",
                    canonical_url="https://www.tiktok.com/@scout2015",
                    display_url="https://www.tiktok.com/@scout2015",
                    status="found", source_tool="tiktok_public", source_tools=["tiktok_public"],
                    extra={"profile": {}},
                ))
                db.commit()
            investigation_page = client.get(f"/investigations/{case_id}")
            assert investigation_page.status_code == 200
            assert 'Capture TikTok MP4 + evidence' in investigation_page.text
            assert 'Paste an individual video URL from this account' in investigation_page.text
            assert 'Download verified TikTok evidence ZIP' in investigation_page.text
            download = client.get(f"/investigations/{case_id}/tiktok/evidence/{acquisition_id}.zip")
            assert download.status_code == 200, download.text
            assert download.headers["x-content-sha256"] == hashlib.sha256(download.content).hexdigest()
            with zipfile.ZipFile(BytesIO(download.content)) as archive:
                assert archive.read("capture.mp4").endswith(b"media")
                assert json.loads(archive.read("acquisition-manifest.json"))["metadata"]["id"] == "6718335390845095173"
            invalid = client.post(
                f"/investigations/{case_id}/tiktok/evidence",
                data={"csrf": token.group(1), "video_url": "https://www.tiktok.com/@scout2015"},
            )
            assert invalid.status_code == 422
    finally:
        with SessionLocal.begin() as db:
            db.execute(delete(Investigation).where(Investigation.id == case_id))
