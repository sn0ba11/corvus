import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.db import AuditEvent, Finding, LocationObservation, ToolRun
from app.services.deduplication import PROFILE_FINDING_TYPES, canonical_url_score

_STATUS_RANK = {
    "error": 0,
    "rate_limited": 1,
    "not_found": 2,
    "unknown": 3,
    "likely": 4,
    "found": 5,
}
_POSITIVE_SOURCE_STATUSES = {"found", "claimed", "exists", "true", "likely"}


def _normalise(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _group_key(finding: Finding) -> tuple[str, str, str] | None:
    if (
        finding.query_type != "username"
        or finding.finding_type not in PROFILE_FINDING_TYPES
        or not finding.platform
    ):
        return None
    identifier = _normalise(finding.queried_value)
    if not identifier:
        return None
    return finding.investigation_id, finding.platform.strip().casefold(), identifier


def _metadata_size(finding: Finding) -> int:
    try:
        return len(json.dumps(finding.extra, sort_keys=True, default=str))
    except (TypeError, ValueError):
        return 0


def _survivor_score(finding: Finding) -> tuple[int, int, float, int]:
    return (
        canonical_url_score(finding.canonical_url),
        _STATUS_RANK.get(finding.status, 0),
        finding.confidence or 0.0,
        _metadata_size(finding),
    )


def _merge_unique_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        try:
            marker = json.dumps(value, sort_keys=True, default=str)
        except (TypeError, ValueError):
            marker = repr(value)
        if marker in seen:
            continue
        seen.add(marker)
        merged.append(value)
    return merged


def _merge_extras(findings: list[Finding], survivor: Finding) -> dict[str, Any]:
    merged = dict(survivor.extra or {})
    profile = dict(merged.get("profile") or {})
    source_metadata = dict(merged.get("source_metadata") or {})
    provenance = [item for item in merged.get("provenance", []) if isinstance(item, dict)]

    for finding in findings:
        extra = dict(finding.extra or {})
        for key, value in extra.items():
            if key in {"profile", "source_metadata", "provenance", "verification"}:
                continue
            if key not in merged or merged[key] in (None, "", [], {}):
                merged[key] = value
        incoming_profile = extra.get("profile")
        if isinstance(incoming_profile, dict):
            for key, value in incoming_profile.items():
                if value not in (None, "") and profile.get(key) in (None, ""):
                    profile[key] = value
        incoming_sources = extra.get("source_metadata")
        if isinstance(incoming_sources, dict):
            for tool, metadata in incoming_sources.items():
                if isinstance(metadata, dict):
                    source_metadata[str(tool)] = metadata
        incoming_provenance = extra.get("provenance")
        if isinstance(incoming_provenance, list):
            provenance.extend(item for item in incoming_provenance if isinstance(item, dict))

    merged["profile"] = profile
    merged["source_metadata"] = source_metadata
    merged["provenance"] = _merge_unique_dicts(provenance)

    account_id = _normalise(profile.get("account_id"))
    query = _normalise(survivor.queried_value)
    positive_tools = []
    for tool, metadata in source_metadata.items():
        status = _normalise(metadata.get("status")).replace("_", " ")
        if status in _POSITIVE_SOURCE_STATUSES:
            positive_tools.append(tool)
    if (
        account_id
        and account_id == query
        and positive_tools
        and any(profile.get(key) for key in ("avatar_url", "display_name", "biography", "location"))
    ):
        state = "corroborated_account" if len(positive_tools) > 1 else "confirmed_account"
        merged["verification"] = {
            "state": state,
            "existence_state": "confirmed",
            "existence_confidence": 0.99,
            "method": "reconciled_collector_account_metadata",
            "source_url": survivor.canonical_url,
            "observed_at": datetime.now(UTC).isoformat(),
            "status_code": None,
            "matched_identifier": True,
            "evidence": {"source_tools": positive_tools, **profile},
            "limitation": None,
        }
    elif isinstance(merged.get("verification"), dict):
        verification = dict(merged["verification"])
        if verification.get("state") == "identifier_mismatch" and account_id == query:
            merged.pop("verification", None)
    return merged


def _merge_group(db: Session, findings: list[Finding]) -> tuple[Finding, list[str]]:
    survivor = max(findings, key=_survivor_score)
    duplicates = [finding for finding in findings if finding.id != survivor.id]

    survivor.status = max(findings, key=lambda item: _STATUS_RANK.get(item.status, 0)).status
    confidences = [finding.confidence for finding in findings if finding.confidence is not None]
    survivor.confidence = max(confidences) if confidences else survivor.confidence
    survivor.source_tools = list(
        dict.fromkeys(
            tool
            for finding in [survivor, *duplicates]
            for tool in (finding.source_tools or [finding.source_tool])
        )
    )
    survivor.extra = _merge_extras([survivor, *duplicates], survivor)
    for finding in duplicates:
        if not survivor.title and finding.title:
            survivor.title = finding.title
        if not survivor.description and finding.description:
            survivor.description = finding.description
        db.execute(
            update(LocationObservation)
            .where(LocationObservation.finding_id == finding.id)
            .values(finding_id=survivor.id)
        )
        db.execute(
            update(ToolRun)
            .where(ToolRun.pivot_source_finding_id == finding.id)
            .values(pivot_source_finding_id=survivor.id)
        )
        db.delete(finding)
    return survivor, [finding.id for finding in duplicates]


def reconcile_profile_duplicates() -> int:
    """Merge legacy duplicate profile findings created from platform API/profile URL aliases."""
    merged_count = 0
    with SessionLocal.begin() as db:
        groups: dict[tuple[str, str, str], list[Finding]] = defaultdict(list)
        for finding in db.scalars(
            select(Finding).where(
                Finding.query_type == "username",
                Finding.finding_type.in_(PROFILE_FINDING_TYPES),
            )
        ):
            key = _group_key(finding)
            if key is not None:
                groups[key].append(finding)

        for findings in groups.values():
            if len(findings) < 2:
                continue
            survivor, removed_ids = _merge_group(db, findings)
            merged_count += len(removed_ids)
            db.add(
                AuditEvent(
                    investigation_id=survivor.investigation_id,
                    event_type="duplicate_profiles_reconciled",
                    details={
                        "survivor_finding_id": survivor.id,
                        "removed_finding_ids": removed_ids,
                        "platform": survivor.platform,
                        "queried_value": survivor.queried_value,
                    },
                )
            )
    return merged_count
