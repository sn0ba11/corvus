import json
from pathlib import Path

import pytest

from app.adapters.base import ExecutionResult
from app.adapters.yt_dlp_metadata import YtDlpMetadataAdapter, validate_video_url
from app.models.schemas import QueryType


def test_yt_dlp_builds_metadata_only_command_and_rejects_unapproved_hosts(tmp_path: Path) -> None:
    adapter = YtDlpMetadataAdapter()
    command = adapter.build_command("https://www.youtube.com/watch?v=abc123", QueryType.NAME, tmp_path, 60)
    assert command[:8] == ["yt-dlp", "--skip-download", "--dump-single-json", "--no-playlist", "--no-warnings", "--socket-timeout", "15", "--extractor-retries"]
    assert command[-1] == "https://www.youtube.com/watch?v=abc123"
    with pytest.raises(ValueError):
        validate_video_url("https://example.org/video")
    with pytest.raises(ValueError):
        validate_video_url("http://localhost/video")
    assert validate_video_url("https://www.tiktok.com/@scout2015/video/6718335390845095173") == (
        "https://www.tiktok.com/@scout2015/video/6718335390845095173"
    )
    tiktok_command = adapter.build_command(
        "https://www.tiktok.com/@scout2015/video/6718335390845095173", QueryType.NAME, tmp_path, 60
    )
    assert tiktok_command[-3:-1] == ["--impersonate", "chrome"]
    for invalid_tiktok_url in (
        "https://www.tiktok.com/@scout2015",
        "https://www.tiktok.com/@scout2015/video",
        "https://www.tiktok.com/@scout2015/video/not-a-video",
        "https://www.tiktok.com/search?q=example",
    ):
        with pytest.raises(ValueError):
            validate_video_url(invalid_tiktok_url)


def test_yt_dlp_parses_bounded_metadata_without_media_payload(tmp_path: Path) -> None:
    adapter = YtDlpMetadataAdapter()
    payload = {
        "id": "abc123",
        "title": "Example video",
        "description": "Public description",
        "webpage_url": "https://www.youtube.com/watch?v=abc123",
        "extractor_key": "Youtube",
        "uploader": "Example Channel",
        "upload_date": "20260910",
        "duration": 42,
        "view_count": 100,
        "formats": [{"url": "https://media.example.invalid/video.mp4"}],
    }
    result = ExecutionResult(0, json.dumps(payload), "")
    records = adapter.parse_results(result, tmp_path)
    assert len(records) == 1
    assert "formats" not in records[0]
    findings = adapter.normalise_results(records, payload["webpage_url"], QueryType.NAME, "yt-dlp-metadata.json")
    assert findings[0].finding_type == "video_metadata"
    assert findings[0].metadata["collector_mode"] == "metadata_only_no_media_download"
    assert findings[0].platform == "Youtube"
