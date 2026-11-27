import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.models.db import AuditEvent, Finding, Investigation
from app.models.schemas import FindingStatus
from app.services.wayback import WaybackCapture, WaybackError, discover_captures

MAX_AUTOMATIC_WAYBACK_TARGETS = 10
MAX_AUTOMATIC_CAPTURES = 50


@dataclass(frozen=True, slots=True)
class AutomaticWaybackTarget:
    finding_id: str
    url: str
    platform: str | None


@dataclass(frozen=True, slots=True)
class AutomaticWaybackResult:
    finding_id: str
    url: str
    platform: str | None
    captures: tuple[WaybackCapture, ...]
    error: str | None = None


def _eligible_url(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.casefold()
    path = parsed.path.casefold()
    if host.startswith("api.") or "/api/" in path or "oembed" in path:
        return False
    return True


def automatic_wayback_targets(
    investigation: Investigation,
    *,
    limit: int = MAX_AUTOMATIC_WAYBACK_TARGETS,
) -> list[AutomaticWaybackTarget]:
    ranked: list[tuple[int, Finding]] = []
    seen_urls: set[str] = set()
    for finding in investigation.findings:
        if finding.status not in {FindingStatus.FOUND.value, FindingStatus.LIKELY.value}:
            continue
        if not finding.canonical_url or not _eligible_url(finding.canonical_url):
            continue
        existing = finding.extra.get("wayback", {})
        if isinstance(existing, dict) and existing.get("automatic_checked_at"):
            continue
        if finding.canonical_url in seen_urls:
            continue
        seen_urls.add(finding.canonical_url)
        profile = finding.extra.get("profile", {})
        richness = 0
        if isinstance(profile, dict):
            richness = sum(
                1
                for key in ("display_name", "avatar_url", "biography", "location", "account_id")
                if profile.get(key)
            )
        ranked.append((richness, finding))
    ranked.sort(
        key=lambda item: (
            -item[0],
            0 if item[1].status == FindingStatus.FOUND.value else 1,
            str(item[1].platform or "").casefold(),
            item[1].canonical_url or "",
        )
    )
    return [
        AutomaticWaybackTarget(finding.id, finding.canonical_url or "", finding.platform)
        for _, finding in ranked[: max(1, min(limit, MAX_AUTOMATIC_WAYBACK_TARGETS))]
    ]


async def discover_automatic_wayback(
    targets: list[AutomaticWaybackTarget],
) -> list[AutomaticWaybackResult]:
    semaphore = asyncio.Semaphore(4)

    async def inspect(target: AutomaticWaybackTarget) -> AutomaticWaybackResult:
        async with semaphore:
            try:
                captures = await discover_captures(target.url, limit=MAX_AUTOMATIC_CAPTURES)
            except WaybackError as exc:
                return AutomaticWaybackResult(
                    target.finding_id,
                    target.url,
                    target.platform,
                    (),
                    str(exc),
                )
            return AutomaticWaybackResult(
                target.finding_id,
                target.url,
                target.platform,
                tuple(captures),
            )

    return await asyncio.gather(*(inspect(target) for target in targets))


def _capture_summary(capture: WaybackCapture) -> dict[str, object]:
    return {
        "timestamp": capture.timestamp,
        "captured_at": capture.captured_at.isoformat() if capture.captured_at else None,
        "original_url": capture.original_url,
        "archive_url": capture.archive_url,
        "digest": capture.digest,
        "mime_type": capture.mime_type,
        "length": capture.length,
    }


def apply_automatic_wayback_results(
    db: Session,
    investigation: Investigation,
    results: list[AutomaticWaybackResult],
) -> dict[str, int]:
    checked_at = datetime.now(UTC).isoformat()
    checked = 0
    archived = 0
    capture_count = 0
    failed = 0
    for result in results:
        finding = db.get(Finding, result.finding_id)
        if finding is None or finding.investigation_id != investigation.id:
            continue
        captures = list(result.captures)
        metadata: dict[str, object] = {
            "automatic_checked_at": checked_at,
            "target_url": result.url,
            "capture_count": len(captures),
            "materially_distinct_count": len(captures),
            "error": result.error,
        }
        if captures:
            captures.sort(key=lambda capture: capture.timestamp)
            capture_index = [_capture_summary(capture) for capture in captures]
            metadata["first_capture"] = capture_index[0]
            metadata["latest_capture"] = capture_index[-1]
            metadata["cdx_records"] = capture_index
            metadata["cdx_endpoint"] = "https://web.archive.org/cdx/search/cdx"
            metadata["capture_count_is_distinct"] = True
            metadata["capture_limit"] = MAX_AUTOMATIC_CAPTURES
            archived += 1
            capture_count += len(captures)
        if result.error:
            failed += 1
        finding.extra = {**finding.extra, "wayback": metadata}
        checked += 1

    summary = {
        "targets_checked": checked,
        "targets_archived": archived,
        "captures_found": capture_count,
        "targets_failed": failed,
    }
    if checked:
        db.add(
            AuditEvent(
                investigation_id=investigation.id,
                event_type="wayback_automatic_discovery",
                details=summary,
            )
        )
    return summary
