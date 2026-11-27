from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.adapters.base import Adapter, ExecutionResult, HealthResult
from app.models.schemas import FindingRecord, FindingStatus, QueryType

SUPPORTED_HOSTS = (
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "x.com",
    "twitter.com",
    "reddit.com",
    "facebook.com",
    "tiktok.com",
)
MAX_STDOUT_BYTES = 4 * 1024 * 1024


def validate_video_url(value: str) -> str:
    target = value.strip()
    if len(target) > 2048:
        raise ValueError("video URL is too long")
    parsed = urlsplit(target)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("enter a public HTTP(S) video URL")
    host = parsed.hostname.casefold().rstrip(".")
    if not any(host == allowed or host.endswith(f".{allowed}") for allowed in SUPPORTED_HOSTS):
        raise ValueError("yt-dlp metadata is limited to YouTube, Vimeo, X/Twitter, Reddit, Facebook and TikTok URLs")
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        # Only individual published videos: never enumerate arbitrary profile feeds or
        # let a supplied playlist silently trigger bulk collection.
        import re

        if not re.fullmatch(r"/@[A-Za-z0-9._-]+/video/[0-9]+/?", parsed.path):
            raise ValueError("enter a direct TikTok /@username/video/123... URL")
    return target


class YtDlpMetadataAdapter(Adapter):
    tool_id = "yt_dlp_metadata"
    display_name = "yt-dlp video metadata"
    executable = "yt-dlp"
    capabilities = frozenset({"video_metadata", "metadata_only", "explicit_url"})
    supported_query_types = frozenset()
    default_selected = False
    default_timeout = 60
    concurrency_limit = 1

    async def healthcheck(self) -> HealthResult:
        return await super().healthcheck()

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        target = validate_video_url(query)
        command = [
            self.executable,
            "--skip-download",
            "--dump-single-json",
            "--no-playlist",
            "--no-warnings",
            "--socket-timeout",
            "15",
            "--extractor-retries",
            "1",
        ]
        host = (urlsplit(target).hostname or "").casefold().rstrip(".")
        if host == "tiktok.com" or host.endswith(".tiktok.com"):
            # TikTok blocks the default yt-dlp HTTP client for public posts.
            # curl_cffi is pinned only in the yt-dlp image for this purpose.
            command.extend(["--impersonate", "chrome"])
        if proxy:
            command.extend(["--proxy", proxy])
        command.append(target)
        return command

    def build_tiktok_acquisition_command(self, url: str) -> list[str]:
        from app.services.tiktok_evidence import acquisition_command

        return acquisition_command(url)

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        if not result.stdout.strip() or len(result.stdout.encode("utf-8", errors="replace")) > MAX_STDOUT_BYTES:
            return []
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        retained = {
            key: payload.get(key)
            for key in (
                "id", "title", "description", "webpage_url", "original_url", "extractor", "extractor_key",
                "uploader", "uploader_id", "uploader_url", "channel", "channel_id", "channel_url",
                "upload_date", "timestamp", "release_timestamp", "duration", "view_count", "like_count",
                "comment_count", "availability", "live_status", "age_limit", "language", "categories", "tags",
            )
            if payload.get(key) not in (None, "")
        }
        path = output_dir / "yt-dlp-metadata.json"
        path.write_text(json.dumps(retained, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)
        return [retained]

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        if not records:
            return []
        record = records[0]
        media_id = str(record.get("id") or "").strip()
        title = str(record.get("title") or media_id or "Video metadata")[:500]
        canonical = str(record.get("webpage_url") or record.get("original_url") or validate_video_url(query))
        platform = str(record.get("extractor_key") or record.get("extractor") or "Video")[:100]
        metadata = dict(record)
        metadata["collector_mode"] = "metadata_only_no_media_download"
        return [
            FindingRecord(
                query_type=query_type,
                queried_value=query,
                finding_type="video_metadata",
                platform=platform,
                value=media_id or canonical,
                canonical_url=canonical,
                display_url=canonical,
                status=FindingStatus.FOUND,
                confidence=0.95,
                title=title,
                description=str(record.get("description") or "")[:2000] or None,
                metadata=metadata,
                source_tool=self.tool_id,
                source_record_id=f"{platform.casefold()}:{media_id or canonical}",
                raw_output_reference=raw_reference,
            )
        ]
