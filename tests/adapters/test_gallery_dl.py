import json
from pathlib import Path

import pytest

from app.adapters.base import ExecutionResult
from app.adapters.gallery_dl import GalleryDLAdapter
from app.models.schemas import QueryType


def test_gallery_dl_is_optional_metadata_only_and_builds_bounded_targets(tmp_path: Path) -> None:
    adapter = GalleryDLAdapter()

    assert adapter.default_selected is False
    assert adapter.local_session_format == "netscape_cookie_jar"
    assert adapter.local_session_flag == "--cookies"

    command = adapter.build_command("example_user", QueryType.USERNAME, tmp_path, 90)

    assert command[0] == "gallery-dl"
    assert "--dump-json" in command
    assert "--no-download" in command
    assert "--no-input" in command
    assert "--config-ignore" in command
    assert "extractor.instagram.max-posts=5" in command
    assert "extractor.tiktok.tiktok-range=1-5" in command
    assert "https://www.instagram.com/example_user/posts/" in command
    assert "https://www.tiktok.com/@example_user/posts" in command


@pytest.mark.parametrize("bad_query", ["", "two words", "@", "x" * 101])
def test_gallery_dl_rejects_invalid_username_targets(tmp_path: Path, bad_query: str) -> None:
    with pytest.raises(ValueError):
        GalleryDLAdapter().build_command(bad_query, QueryType.USERNAME, tmp_path, 90)


def test_gallery_dl_parses_json_lines_and_normalises_per_platform(tmp_path: Path) -> None:
    records = [
        {
            "category": "instagram",
            "username": "example_user",
            "fullname": "Example Person",
            "biography": "Example biography",
            "profile_pic_url": "https://cdn.example/avatar.jpg",
            "post_id": "ig-1",
            "url": "https://www.instagram.com/p/one/",
        },
        {
            "category": "instagram",
            "username": "example_user",
            "post_id": "ig-2",
            "url": "https://www.instagram.com/p/two/",
        },
        {
            "category": "tiktok",
            "author_name": "Example TikTok",
            "video_id": "tt-1",
            "webpage_url": "https://www.tiktok.com/@example_user/video/1",
        },
    ]
    stdout = json.dumps([[3, record.get("url") or record.get("webpage_url") or "https://example.test", record] for record in records])
    adapter = GalleryDLAdapter()
    parsed = adapter.parse_results(ExecutionResult(0, stdout, ""), tmp_path)
    findings = adapter.normalise_results(
        parsed, "example_user", QueryType.USERNAME, "raw/gallery-dl-metadata.json"
    )

    assert len(parsed) == 3
    assert (tmp_path / "gallery-dl-metadata.json").is_file()
    assert {finding.platform for finding in findings} == {"Instagram", "TikTok"}
    by_platform = {finding.platform: finding for finding in findings}
    assert by_platform["Instagram"].profile is not None
    assert by_platform["Instagram"].profile.display_name == "Example Person"
    assert by_platform["Instagram"].metadata["items_observed"] == 2
    assert by_platform["Instagram"].metadata["collector_mode"] == "metadata_only"
    assert by_platform["TikTok"].metadata["items_observed"] == 1
    assert by_platform["TikTok"].canonical_url == "https://www.tiktok.com/@example_user"


def test_gallery_dl_ignores_unrelated_extractors(tmp_path: Path) -> None:
    adapter = GalleryDLAdapter()
    stdout = json.dumps({"category": "reddit", "url": "https://reddit.com/r/example"})
    parsed = adapter.parse_results(ExecutionResult(0, stdout, ""), tmp_path)

    assert parsed == []
    assert adapter.normalise_results(parsed, "example_user", QueryType.USERNAME, None) == []
