"""Unified account, entity and timeline projection for normal investigations.

The projection is deliberately derived from retained findings rather than stored as a
second case model. Every account card, entity and event keeps stable finding IDs and
collector provenance so the analyst can return to the technical evidence register.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.models.db import Finding, Investigation
from app.services.collector_audit import (
    analyse_parsing_gaps,
    build_excluded_timeline_observations,
    build_timeline,
    extract_entities,
    merge_accounts,
)

PROFILE_FINDING_TOKENS = ("profile", "account", "social")
POSITIVE_STATUSES = {"found", "likely"}
WEAK_COLLECTORS = {"sherlock", "nexfil", "social_analyzer"}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _is_profile_finding(finding: Finding) -> bool:
    kind = finding.finding_type.casefold()
    return (
        finding.query_type == "username"
        or any(token in kind for token in PROFILE_FINDING_TOKENS)
    ) and bool(finding.platform or finding.canonical_url)


def _finding_records(finding: Finding) -> list[dict[str, Any]]:
    sources = list(dict.fromkeys(finding.source_tools or [finding.source_tool]))
    extra = dict(finding.extra or {})
    raw_profile = extra.get("profile")
    merged_profile: dict[str, Any] = raw_profile if isinstance(raw_profile, dict) else {}
    raw_source_metadata = extra.get("source_metadata")
    source_metadata: dict[str, Any] = (
        raw_source_metadata if isinstance(raw_source_metadata, dict) else {}
    )
    global_metadata = {
        key: value for key, value in extra.items() if key not in {"profile", "source_metadata"}
    }

    def record_for(
        source: str,
        *,
        metadata: dict[str, Any],
        profile: dict[str, Any],
        source_record_id: str,
    ) -> dict[str, Any]:
        return {
            "query_type": finding.query_type,
            "queried_value": finding.queried_value,
            "finding_type": finding.finding_type,
            "platform": finding.platform,
            "value": finding.value,
            "canonical_url": finding.canonical_url,
            "display_url": finding.display_url,
            "status": finding.status,
            "confidence": finding.confidence,
            "title": finding.title,
            "description": finding.description,
            "metadata": metadata,
            "profile": profile,
            "source_tool": source,
            "source_tools": [source],
            "source_record_id": source_record_id,
            "finding_id": finding.id,
            "tool_run_id": finding.tool_run_id,
            "observed_at": _iso(finding.observed_at),
            "raw_output_reference": finding.raw_output_reference,
        }

    if source_metadata:
        records: list[dict[str, Any]] = []
        for source in sources:
            raw_metadata = source_metadata.get(source)
            metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
            if source == finding.source_tool:
                metadata = {**global_metadata, **metadata}
            records.append(
                record_for(
                    source,
                    metadata=metadata,
                    profile=merged_profile if source == finding.source_tool else {},
                    source_record_id=f"{finding.id}:{source}",
                )
            )
        return records

    return [
        record_for(
            finding.source_tool,
            metadata=extra,
            profile=merged_profile,
            source_record_id=str(finding.id),
        )
        | {"source_tools": sources}
    ]


def _source_record_score(record: dict[str, Any]) -> int:
    raw_metadata = record.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    raw_profile = record.get("profile")
    profile: dict[str, Any] = raw_profile if isinstance(raw_profile, dict) else {}
    score = 0
    if record.get("status") in POSITIVE_STATUSES:
        score += 20
    for key in ("display_name", "account_id", "biography", "location", "avatar_url"):
        if profile.get(key) not in (None, ""):
            score += 5
    image_cache = metadata.get("image_cache")
    if isinstance(image_cache, dict) and image_cache.get("sha256"):
        score += 8
    if metadata.get("avatar_local_path"):
        score += 8
    if isinstance(metadata.get("platform_details"), list):
        score += min(10, len(metadata["platform_details"]))
    if isinstance(metadata.get("timeline_events"), list):
        score += min(9, len(metadata["timeline_events"]) * 3)
    return score


def _account_priority(account: dict[str, Any]) -> tuple[str, str]:
    raw_profile = account.get("profile")
    profile: dict[str, Any] = raw_profile if isinstance(raw_profile, dict) else {}
    raw_sources = account.get("sources")
    sources = set(raw_sources if isinstance(raw_sources, list) else [])
    rich_identity = any(
        profile.get(key) not in (None, "")
        for key in ("display_name", "biography", "location", "avatar_url")
    ) or len(account.get("details", [])) >= 2 or bool(account.get("timeline_events"))
    independent_sources = sources - WEAK_COLLECTORS

    if account.get("status") in POSITIVE_STATUSES and (
        account.get("source_count", 0) > 1 or (rich_identity and independent_sources)
    ):
        return "priority", "Rich or independently corroborated positive account evidence"
    if account.get("status") in POSITIVE_STATUSES:
        return "found", "Positive single-source account lead"
    return "technical", "Unverified candidate retained for technical review"


def _finalise_account(account: dict[str, Any]) -> dict[str, Any]:
    source_records = [
        item for item in account.get("source_records", []) if isinstance(item, dict)
    ]
    source_records.sort(key=_source_record_score, reverse=True)
    finding_ids = [str(item["finding_id"]) for item in source_records if item.get("finding_id")]
    finding_ids = list(dict.fromkeys(finding_ids))
    primary = source_records[0] if source_records else {}
    primary_finding_id = str(primary.get("finding_id") or "") or None

    avatar_finding_id: str | None = None
    for record in source_records:
        raw_metadata = record.get("metadata")
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        raw_profile = record.get("profile")
        profile: dict[str, Any] = raw_profile if isinstance(raw_profile, dict) else {}
        raw_cache = metadata.get("image_cache")
        cache: dict[str, Any] = raw_cache if isinstance(raw_cache, dict) else {}
        if cache.get("sha256") or metadata.get("avatar_local_path") or profile.get("avatar_url"):
            avatar_finding_id = str(record.get("finding_id") or "") or None
            if avatar_finding_id:
                break

    group, reason = _account_priority(account)
    output = {key: value for key, value in account.items() if key != "source_records"}
    output.update(
        {
            "finding_ids": finding_ids,
            "primary_finding_id": primary_finding_id,
            "avatar_finding_id": avatar_finding_id,
            "group": group,
            "group_reason": reason,
            "technical_only": group == "technical",
            "evidence_links": [
                {
                    "finding_id": str(record.get("finding_id")),
                    "tool_run_id": str(record.get("tool_run_id")),
                    "source_tool": str(record.get("source_tool") or "unknown"),
                    "observed_at": record.get("observed_at"),
                }
                for record in source_records
                if record.get("finding_id")
            ],
        }
    )
    return output


def _timeline_sort_key(event: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(event.get("timestamp") or ""),
        str(event.get("platform") or ""),
        str(event.get("label") or ""),
    )


def _non_account_timeline_events(investigation: Investigation) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    for run in investigation.runs:
        timestamp = _iso(run.finished_at or run.started_at)
        if not timestamp:
            continue
        events.append(
            {
                "timestamp": timestamp,
                "platform": "Corvus",
                "label": f"Collector {run.status.replace('_', ' ')}: {run.display_name}",
                "display_name": None,
                "username": run.query_value,
                "source_field": "tool_run.finished_at" if run.finished_at else "tool_run.started_at",
                "extractor": run.tool_id,
                "confidence": None,
                "event_type": "tool_run",
                "sources": [run.tool_id],
            }
        )

    for browser_run in investigation.browser_runs:
        timestamp = _iso(browser_run.observed_at or browser_run.finished_at or browser_run.started_at)
        if not timestamp:
            continue
        events.append(
            {
                "timestamp": timestamp,
                "platform": "Managed browser",
                "label": f"Browser capture {browser_run.status.replace('_', ' ')}",
                "display_name": browser_run.page_title,
                "username": None,
                "source_field": "browser_run.observed_at",
                "extractor": browser_run.collector_id,
                "confidence": None,
                "event_type": "browser_capture",
                "sources": [browser_run.collector_id],
            }
        )

    for observation in investigation.location_observations:
        timestamp = _iso(
            observation.occurred_at
            or observation.published_at
            or observation.first_observed_at
        )
        if not timestamp:
            continue
        events.append(
            {
                "timestamp": timestamp,
                "platform": observation.source_platform,
                "label": f"Location evidence: {observation.place_name}",
                "display_name": observation.account_identifier,
                "username": observation.account_identifier,
                "source_field": (
                    "location.occurred_at"
                    if observation.occurred_at
                    else "location.published_at"
                    if observation.published_at
                    else "location.first_observed_at"
                ),
                "extractor": observation.evidence_type,
                "confidence": f"{observation.confidence:.2f}",
                "event_type": "location_observation",
                "sources": [observation.source_platform],
            }
        )

    for finding in investigation.findings:
        if finding.finding_type == "telegram_message":
            timeline_events = finding.extra.get("timeline_events", [])
            if isinstance(timeline_events, list):
                for event in timeline_events[:10]:
                    if not isinstance(event, dict):
                        continue
                    timestamp = str(event.get("timestamp") or "").strip()
                    if not timestamp:
                        continue
                    events.append(
                        {
                            "timestamp": timestamp,
                            "platform": "Telegram",
                            "label": str(event.get("label") or "Telegram message")[:200],
                            "display_name": finding.extra.get("telegram_sender_name"),
                            "username": finding.extra.get("telegram_sender_id"),
                            "source_field": str(event.get("source_field") or "date")[:100],
                            "extractor": "Telegram Desktop export",
                            "confidence": "high",
                            "event_type": "telegram_message",
                            "sources": [finding.source_tool],
                        }
                    )
        wayback = finding.extra.get("wayback", {})
        if not isinstance(wayback, dict):
            continue
        seen_archive_times: set[str] = set()
        for label, key in (("First archive observation", "first_capture"), ("Latest archive observation", "latest_capture")):
            capture = wayback.get(key)
            if not isinstance(capture, dict):
                continue
            timestamp = str(capture.get("captured_at") or "").strip()
            if not timestamp or timestamp in seen_archive_times:
                continue
            seen_archive_times.add(timestamp)
            events.append(
                {
                    "timestamp": timestamp,
                    "platform": finding.platform or "Web archive",
                    "label": label,
                    "display_name": finding.title,
                    "username": finding.value,
                    "source_field": f"wayback.{key}.captured_at",
                    "extractor": "Wayback CDX",
                    "confidence": None,
                    "event_type": "archive_observation",
                    "sources": [finding.source_tool],
                }
            )
    return events


def build_investigation_account_view(investigation: Investigation) -> dict[str, Any]:
    """Build the production account view from retained investigation findings."""
    findings = [finding for finding in investigation.findings if _is_profile_finding(finding)]
    records = [record for finding in findings for record in _finding_records(finding)]
    merged = merge_accounts(records)
    entities = extract_entities(merged)
    account_timeline = build_timeline(merged)
    timeline = sorted(
        [*account_timeline, *_non_account_timeline_events(investigation)],
        key=_timeline_sort_key,
    )
    excluded = build_excluded_timeline_observations(merged)
    accounts = [_finalise_account(account) for account in merged]
    groups = {
        name: [account for account in accounts if account["group"] == name]
        for name in ("priority", "found", "technical")
    }
    parsing_gaps = analyse_parsing_gaps(records)
    return {
        "enabled": investigation.query_type == "username" or bool(records),
        "query": investigation.query,
        "accounts": accounts,
        "groups": groups,
        "entities": entities,
        "timeline": timeline,
        "excluded_timeline_observations": excluded,
        "parsing_gaps": parsing_gaps,
        "summary": {
            "account_count": len(accounts),
            "priority_account_count": len(groups["priority"]),
            "found_account_count": len(groups["found"]),
            "technical_account_count": len(groups["technical"]),
            "corroborated_account_count": sum(
                account.get("source_count", 0) > 1 for account in accounts
            ),
            "timeline_event_count": len(timeline),
            "account_timeline_event_count": len(account_timeline),
            "excluded_timeline_observation_count": len(excluded),
            "entity_counts": {kind: len(values) for kind, values in entities.items()},
            "parser_gap_field_count": parsing_gaps["field_count"],
        },
        "methodology": {
            "identity_warning": (
                "Matching usernames across platforms remain separate account leads. "
                "The view does not assert that they belong to one person."
            ),
            "technical_lead_policy": (
                "Unresolved candidate-generator and unknown results are collapsed by default but "
                "remain in the technical evidence register."
            ),
        },
    }
