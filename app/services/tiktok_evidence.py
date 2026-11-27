"""Bounded, case-scoped acquisition of a single public TikTok post."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.adapters.yt_dlp_metadata import validate_video_url

MAX_VIDEO_BYTES = 12 * 1024 * 1024
MAX_INFO_BYTES = 4 * 1024 * 1024


class TikTokEvidenceError(ValueError):
    pass


def validate_tiktok_post(url: str) -> str:
    from urllib.parse import urlsplit

    target = validate_video_url(url)
    host = (urlsplit(target).hostname or "").casefold().rstrip(".")
    if host != "tiktok.com" and not host.endswith(".tiktok.com"):
        raise TikTokEvidenceError("a direct public TikTok video URL is required")
    return target


def acquisition_command(url: str) -> list[str]:
    target = validate_tiktok_post(url)
    return [
        "yt-dlp", "--no-playlist", "--no-warnings", "--no-progress", "--no-part",
        "--socket-timeout", "15", "--extractor-retries", "1", "--impersonate", "chrome",
        "--max-filesize", "12M", "-f", "b[ext=mp4]", "--write-info-json",
        "-o", "capture.%(ext)s", target,
    ]


def _read_bounded(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise TikTokEvidenceError("acquired evidence file missing, unsafe or exceeds collection limit")
    return path.read_bytes()


def finalize_acquisition(output_dir: Path, url: str, *, version: str, acquired_at: datetime) -> dict[str, Any]:
    target = validate_tiktok_post(url)
    media = output_dir / "capture.mp4"
    info = output_dir / "capture.info.json"
    video_bytes = _read_bounded(media, MAX_VIDEO_BYTES)
    info_bytes = _read_bounded(info, MAX_INFO_BYTES)
    if not video_bytes or video_bytes[4:8] != b"ftyp":
        raise TikTokEvidenceError("download did not produce a valid MP4 container")
    try:
        payload = json.loads(info_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TikTokEvidenceError("TikTok metadata JSON is invalid") from exc
    if not isinstance(payload, dict) or not str(payload.get("id") or "").isdigit():
        raise TikTokEvidenceError("TikTok metadata is missing its video ID")
    expected_video_id = target.split("/video/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    if str(payload["id"]) != expected_video_id:
        raise TikTokEvidenceError("retrieved TikTok video ID does not match the requested URL")
    fields = (
        "id", "title", "description", "webpage_url", "uploader", "uploader_id", "channel",
        "timestamp", "upload_date", "duration", "view_count", "like_count", "comment_count",
        "repost_count", "availability", "extractor_key", "tags",
    )
    metadata = {key: payload[key] for key in fields if payload.get(key) is not None}
    entries = [
        {"path": name, "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}
        for name, content in (("capture.mp4", video_bytes), ("capture.info.json", info_bytes))
    ]
    manifest: dict[str, Any] = {
        "schema_version": "1.0", "source_url": target,
        "acquired_at_utc": acquired_at.astimezone(UTC).isoformat(),
        "collector": "yt-dlp", "collector_version": version,
        "method": "public_tiktok_video_download_chrome_impersonation",
        "metadata": metadata, "files": entries,
        "limitations": "Public content may change; counts reflect collection time. No authentication or comments collected.",
    }
    manifest_path = output_dir / "acquisition-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    manifest_path.chmod(0o600)
    return manifest


def evidence_zip(output_dir: Path) -> bytes:
    manifest_path = output_dir / "acquisition-manifest.json"
    content = _read_bounded(manifest_path, MAX_INFO_BYTES)
    try:
        manifest = json.loads(content)
    except json.JSONDecodeError as exc:
        raise TikTokEvidenceError("acquisition manifest is invalid") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise TikTokEvidenceError("acquisition manifest is malformed")
    if {entry.get("path") for entry in manifest["files"] if isinstance(entry, dict)} != {
        "capture.mp4", "capture.info.json"
    }:
        raise TikTokEvidenceError("acquisition manifest lists unexpected files")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for entry in manifest["files"]:
            name = entry["path"]
            payload = _read_bounded(output_dir / name, MAX_VIDEO_BYTES if name.endswith(".mp4") else MAX_INFO_BYTES)
            if hashlib.sha256(payload).hexdigest() != entry["sha256"] or len(payload) != entry["size_bytes"]:
                raise TikTokEvidenceError("evidence integrity check failed")
            archive.writestr(name, payload)
        archive.writestr("acquisition-manifest.json", content)
    return output.getvalue()
