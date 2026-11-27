import hashlib
import json
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest

from app.services.tiktok_evidence import (
    TikTokEvidenceError,
    acquisition_command,
    evidence_zip,
    finalize_acquisition,
    validate_tiktok_post,
)

URL = "https://www.tiktok.com/@scout2015/video/6718335390845095173"


def test_tiktok_direct_video_only() -> None:
    assert validate_tiktok_post(URL) == URL
    command = acquisition_command(URL)
    assert command[-1] == URL
    assert command[command.index("--max-filesize") + 1] == "12M"
    assert command[command.index("--impersonate") + 1] == "chrome"
    for rejected in ("https://www.tiktok.com/@scout2015", "https://youtu.be/example", "http://localhost/video"):
        with pytest.raises((ValueError, TikTokEvidenceError)):
            acquisition_command(rejected)


def test_acquisition_manifest_and_zip_verify_integrity(tmp_path: Path) -> None:
    video = b"\x00\x00\x00\x18ftypisom" + b"video evidence" * 12
    (tmp_path / "capture.mp4").write_bytes(video)
    (tmp_path / "capture.info.json").write_text(
        json.dumps({"id": "6718335390845095173", "title": "Public test", "view_count": 100, "formats": [{"url": "private temporary URL"}]}),
        encoding="utf-8",
    )
    manifest = finalize_acquisition(tmp_path, URL, version="2026.10.26", acquired_at=datetime(2026, 11, 16, tzinfo=UTC))
    assert manifest["files"][0]["sha256"] == hashlib.sha256(video).hexdigest()
    assert "formats" not in manifest["metadata"]
    content = evidence_zip(tmp_path)
    with zipfile.ZipFile(BytesIO(content)) as archive:
        assert set(archive.namelist()) == {"capture.mp4", "capture.info.json", "acquisition-manifest.json"}
        assert archive.read("capture.mp4") == video
    (tmp_path / "capture.mp4").write_bytes(video + b"changed")
    with pytest.raises(TikTokEvidenceError, match="integrity"):
        evidence_zip(tmp_path)


def test_reject_wrong_tiktok_video_id(tmp_path: Path) -> None:
    (tmp_path / "capture.mp4").write_bytes(b"\x00\x00\x00\x18ftypisom")
    (tmp_path / "capture.info.json").write_text('{"id":"123456"}', encoding="utf-8")
    with pytest.raises(TikTokEvidenceError, match="does not match"):
        finalize_acquisition(tmp_path, URL, version="test", acquired_at=datetime.now(UTC))


def test_reject_invalid_media_and_overlarge_file(tmp_path: Path) -> None:
    (tmp_path / "capture.mp4").write_bytes(b"not an mp4")
    (tmp_path / "capture.info.json").write_text('{"id":"12"}', encoding="utf-8")
    with pytest.raises(TikTokEvidenceError):
        finalize_acquisition(tmp_path, URL, version="test", acquired_at=datetime.now(UTC))
