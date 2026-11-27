from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from app.adapters.base import Adapter, ExecutionResult
from app.models.schemas import FindingRecord, FindingStatus, ProfileEvidence, QueryType

MAX_ITEMS_PER_PLATFORM = 5
SUPPORTED_PLATFORMS = {
    "instagram": ("Instagram", "https://www.instagram.com/{username}/"),
    "tiktok": ("TikTok", "https://www.tiktok.com/@{username}"),
}


class GalleryDLAdapter(Adapter):
    """Optional metadata-only gallery-dl collector using a scoped SOCK-browser session."""

    tool_id = "gallery_dl"
    display_name = "gallery-dl metadata"
    executable = "gallery-dl"
    capabilities = frozenset(
        {"username_search", "instagram_profile", "tiktok_profile", "session_aware", "metadata_only"}
    )
    supported_query_types = frozenset({QueryType.USERNAME})
    default_selected = False
    default_timeout = 90
    concurrency_limit = 1
    local_session_format = "netscape_cookie_jar"
    local_session_flag = "--cookies"

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        if query_type != QueryType.USERNAME:
            raise ValueError("gallery-dl metadata collection accepts username investigations only")
        username = query.strip().removeprefix("@").strip()
        if not username or len(username) > 100 or any(char.isspace() for char in username):
            raise ValueError("invalid username for gallery-dl metadata collection")
        encoded = quote(username, safe="._-")
        command = [
            self.executable,
            "--dump-json",
            "--no-download",
            "--no-input",
            "--config-ignore",
            "-o",
            f"extractor.instagram.max-posts={MAX_ITEMS_PER_PLATFORM}",
            "-o",
            f"extractor.tiktok.tiktok-range=1-{MAX_ITEMS_PER_PLATFORM}",
        ]
        if proxy:
            command.extend(("--proxy", proxy))
        command.extend(
            (
                f"https://www.instagram.com/{encoded}/posts/",
                f"https://www.tiktok.com/@{encoded}/posts",
            )
        )
        return command

    @staticmethod
    def _records_from_stdout(stdout: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                if any(key in value for key in ("category", "subcategory", "extractor", "site")):
                    records.append(value)
                return
            if not isinstance(value, list):
                return
            if value and isinstance(value[0], int):
                if len(value) >= 3 and isinstance(value[2], dict):
                    collect(value[2])
                    return
                if len(value) >= 2 and isinstance(value[1], dict):
                    collect(value[1])
                    return
            for item in value:
                collect(item)

        stripped = stdout.strip()
        if not stripped:
            return records
        decoder = json.JSONDecoder()
        offset = 0
        while offset < len(stripped):
            while offset < len(stripped) and stripped[offset].isspace():
                offset += 1
            if offset >= len(stripped):
                break
            try:
                value, end = decoder.raw_decode(stripped, offset)
            except json.JSONDecodeError:
                break
            collect(value)
            offset = end
        return records

    @staticmethod
    def _platform(record: dict[str, Any]) -> str | None:
        candidates = (
            record.get("category"),
            record.get("subcategory"),
            record.get("extractor"),
            record.get("site"),
        )
        for candidate in candidates:
            text = str(candidate or "").casefold()
            if "instagram" in text:
                return "instagram"
            if "tiktok" in text:
                return "tiktok"
        for key in ("webpage_url", "post_url", "url", "profile_url"):
            value = record.get(key)
            if not isinstance(value, str):
                continue
            host = (urlsplit(value).hostname or "").casefold()
            if host == "instagram.com" or host.endswith(".instagram.com"):
                return "instagram"
            if host == "tiktok.com" or host.endswith(".tiktok.com"):
                return "tiktok"
        return None

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        records = self._records_from_stdout(result.stdout)
        selected: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for record in records:
            platform = self._platform(record)
            if platform not in SUPPORTED_PLATFORMS:
                continue
            record_id = next(
                (
                    str(record[key])
                    for key in ("post_id", "id", "media_id", "video_id", "shortcode")
                    if record.get(key) not in (None, "")
                ),
                "",
            )
            identity = (platform, record_id or json.dumps(record, sort_keys=True, default=str)[:1000])
            if identity in seen:
                continue
            seen.add(identity)
            selected.append(record)
        if selected:
            artifact = output_dir / "gallery-dl-metadata.json"
            artifact.write_text(
                json.dumps(selected[: MAX_ITEMS_PER_PLATFORM * 2], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            artifact.chmod(0o600)
        return selected[: MAX_ITEMS_PER_PLATFORM * 2]

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            platform = self._platform(record)
            if platform:
                grouped[platform].append(record)

        username = query.strip().removeprefix("@").strip()
        findings: list[FindingRecord] = []
        for platform, platform_records in grouped.items():
            label, profile_template = SUPPORTED_PLATFORMS[platform]
            profile_url = profile_template.format(username=quote(username, safe="._-"))
            first = platform_records[0]
            raw_author = first.get("author")
            author: dict[str, Any] = raw_author if isinstance(raw_author, dict) else {}
            display_name = next(
                (
                    str(value).strip()
                    for value in (
                        first.get("fullname"),
                        first.get("full_name"),
                        first.get("display_name"),
                        first.get("author_name"),
                        first.get("nickname"),
                        author.get("nickname"),
                    )
                    if value
                ),
                None,
            )
            biography = next(
                (
                    str(value).strip()
                    for value in (
                        first.get("biography"),
                        first.get("bio"),
                        first.get("description"),
                        first.get("desc"),
                        author.get("signature"),
                    )
                    if value
                ),
                None,
            )
            avatar = next(
                (
                    str(value).strip()
                    for value in (
                        first.get("profile_pic_url"),
                        first.get("avatar"),
                        first.get("avatar_url"),
                        first.get("author_avatar"),
                        author.get("avatarLarger"),
                        author.get("avatarMedium"),
                        author.get("avatarThumb"),
                    )
                    if isinstance(value, str) and value.startswith("https://")
                ),
                None,
            )
            raw_stats = first.get("authorStats")
            stats: dict[str, Any] = raw_stats if isinstance(raw_stats, dict) else {}
            item_ids = []
            for record in platform_records:
                for key in ("post_id", "id", "media_id", "video_id"):
                    value = record.get(key)
                    if value not in (None, ""):
                        item_ids.append(str(value)[:200])
                        break
            metadata = {
                "collector_mode": "metadata_only",
                "items_observed": len(platform_records),
                "sample_item_ids": item_ids[:10],
                "gallery_dl_fields": sorted(
                    {str(key) for record in platform_records[:5] for key in record.keys()}
                )[:120],
            }
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type=f"{platform}_gallery_metadata",
                    platform=label,
                    value=username,
                    canonical_url=profile_url,
                    display_url=profile_url,
                    status=FindingStatus.LIKELY,
                    confidence=0.72,
                    title=display_name or f"{label} @{username}",
                    description=biography,
                    profile=ProfileEvidence(
                        avatar_url=avatar,
                        display_name=display_name,
                        biography=biography,
                        account_id=str(author.get("id") or username),
                        verified=(bool(author.get("verified")) if "verified" in author else None),
                        followers=(int(stats["followerCount"]) if str(stats.get("followerCount", "")).isdigit() else None),
                        following=(int(stats["followingCount"]) if str(stats.get("followingCount", "")).isdigit() else None),
                    ),
                    metadata=metadata,
                    source_tool=self.tool_id,
                    source_record_id=f"{platform}:{username.casefold()}",
                    raw_output_reference=raw_reference,
                )
            )
        return findings
