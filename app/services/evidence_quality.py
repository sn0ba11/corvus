from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.builtin import blackbird_metadata_value_is_evidence, platform_details
from app.models.db import AuditEvent, Finding

POSITIVE_STATUSES = {"found", "likely"}
BLACKBIRD_ACCOUNT_EVIDENCE_LABELS = {
    "avatar",
    "profile image",
    "profile picture",
    "image",
    "full name",
    "fullname",
    "display name",
    "biography",
    "bio",
    "about",
    "location",
    "city",
    "country",
    "username",
    "account id",
    "user id",
    "verified",
    "followers",
    "follower count",
    "following",
    "following count",
}


def _has_concrete_blackbird_metadata(extra: dict[str, Any]) -> bool:
    if isinstance(extra.get("blackbird_public_verification"), dict):
        return True
    profile = extra.get("profile")
    if isinstance(profile, dict):
        if any(
            profile.get(field) not in (None, "")
            for field in (
                "account_id",
                "avatar_url",
                "biography",
                "location",
                "followers",
                "following",
                "verified",
            )
        ):
            return True
    metadata = extra.get("metadata")
    if not isinstance(metadata, list):
        return False
    for item in metadata:
        if not isinstance(item, dict):
            continue
        label = str(item.get("name") or item.get("type") or "").strip().casefold()
        if label not in BLACKBIRD_ACCOUNT_EVIDENCE_LABELS:
            continue
        site = str(extra.get("site") or extra.get("platform") or "")
        if blackbird_metadata_value_is_evidence(label, item.get("value"), site):
            return True
    return False


def _sole_source(finding: Finding, tool_id: str) -> bool:
    sources = set(finding.source_tools or [finding.source_tool])
    return finding.source_tool == tool_id and sources == {tool_id}


def _record_previous_status(finding: Finding, now: datetime) -> dict[str, Any]:
    metadata = dict(finding.extra)
    observations = list(metadata.get("status_observations", []))
    if not observations:
        observations.append(
            {
                "source": finding.source_tool,
                "status": finding.status,
                "observed_at": finding.observed_at.isoformat(),
            }
        )
    observations.append(
        {
            "source": "evidence_quality_reconciliation",
            "status": "unknown",
            "observed_at": now.isoformat(),
        }
    )
    metadata["status_observations"] = observations[-100:]
    metadata["evidence_quality_reconciled_at"] = now.isoformat()
    return metadata


def reconcile_platform_metadata(db: Session) -> int:
    """Backfill displayable platform details from metadata retained by older runs."""
    updated = 0
    per_investigation: dict[str, int] = defaultdict(int)
    for finding in db.scalars(select(Finding)):
        metadata = dict(finding.extra)
        merged: dict[str, str] = {}
        existing = metadata.get("platform_details")
        if isinstance(existing, list):
            for item in existing:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or "").strip()
                value = str(item.get("value") or "").strip()
                if label and value:
                    merged[label] = value

        records: list[dict[str, Any]] = [metadata]
        source_metadata = metadata.get("source_metadata")
        if isinstance(source_metadata, dict):
            records.extend(value for value in source_metadata.values() if isinstance(value, dict))
        for record in records:
            for item in platform_details(record):
                merged[item["label"]] = item["value"]
        if not merged:
            continue
        normalised = [{"label": label, "value": value} for label, value in merged.items()]
        if normalised == existing:
            continue
        metadata["platform_details"] = normalised
        finding.extra = metadata
        updated += 1
        per_investigation[finding.investigation_id] += 1

    for investigation_id, count in per_investigation.items():
        db.add(
            AuditEvent(
                investigation_id=investigation_id,
                event_type="platform_metadata_reconciled",
                details={"finding_count": count, "policy": "promote retained public profile fields"},
            )
        )
    return updated


def reconcile_legacy_weak_claims(db: Session) -> dict[str, int]:
    """Downgrade legacy standalone existence assertions that lacked account evidence."""
    now = datetime.now(UTC)
    totals = {"sherlock": 0, "blackbird": 0}
    per_investigation: dict[str, dict[str, int]] = defaultdict(
        lambda: {"sherlock": 0, "blackbird": 0}
    )
    findings = list(db.scalars(select(Finding).where(Finding.status.in_(POSITIVE_STATUSES))))
    for finding in findings:
        tool_id: str | None = None
        metadata = dict(finding.extra)
        if _sole_source(finding, "sherlock"):
            tool_id = "sherlock"
            metadata["sherlock_unverified_claim"] = True
            metadata["sherlock_upstream_status"] = finding.status
        elif _sole_source(finding, "blackbird") and not _has_concrete_blackbird_metadata(metadata):
            tool_id = "blackbird"
            metadata["blackbird_unverified_claim"] = True
            metadata["blackbird_upstream_status"] = finding.status
            metadata["blackbird_evidence_strength"] = "bare_url_assertion"
        if tool_id is None:
            continue

        reconciled = _record_previous_status(finding, now)
        reconciled.update(metadata)
        finding.extra = reconciled
        finding.status = "unknown"
        finding.confidence = 0.15 if tool_id == "sherlock" else 0.10
        finding.finding_type = "unverified_profile_lead"
        totals[tool_id] += 1
        per_investigation[finding.investigation_id][tool_id] += 1

    for investigation_id, counts in per_investigation.items():
        db.add(
            AuditEvent(
                investigation_id=investigation_id,
                event_type="evidence_quality_reconciled",
                details={
                    "sherlock_downgraded": counts["sherlock"],
                    "blackbird_downgraded": counts["blackbird"],
                    "policy": "standalone existence assertions require account-specific evidence",
                },
            )
        )
    return totals
