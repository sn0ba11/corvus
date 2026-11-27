"""Explicit retries that keep the failed attempt and its evidence intact."""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.adapters import REGISTRY
from app.core.upstreams import commit_for
from app.models.db import AuditEvent, Investigation, ToolRun
from app.models.schemas import QueryType
from app.services.tool_settings import get_tool_timeouts, validate_tool_timeout

RETRY_STATUSES = frozenset({"failed", "timed_out", "cancelled"})
# session_required: retrying cannot help until the analyst signs in to the platform.
MANUAL_ERRORS = frozenset(
    {"manual_action_required", "captcha", "challenge", "blocked", "session_required"}
)


class RetryConflict(ValueError):
    pass


def can_retry(run: ToolRun) -> bool:
    return (
        run.status in RETRY_STATUSES
        and run.tool_id in REGISTRY
        and not run.rate_limited
        and run.error_code not in MANUAL_ERRORS
    )


def retry_targets(db: Session, investigation_id: str) -> dict[str, str]:
    events = db.scalars(select(AuditEvent).where(
        AuditEvent.investigation_id == investigation_id,
        AuditEvent.event_type == "collector_retry_queued",
    ))
    return {str(event.details["original_run_id"]): str(event.details["retry_run_id"])
            for event in events if event.details.get("original_run_id") and event.details.get("retry_run_id")}


def queue_retry(db: Session, investigation: Investigation, original: ToolRun, timeout: int) -> ToolRun:
    if original.investigation_id != investigation.id:
        raise ValueError("Collector run does not belong to this investigation.")
    if not can_retry(original):
        raise ValueError("Only unsuccessful collectors can be retried here. Use the manual challenge workflow for blocked searches.")
    if original.id in retry_targets(db, investigation.id):
        raise RetryConflict("This attempt already has a retry. Review the newer attempt instead.")
    query_type = QueryType(original.query_type or investigation.query_type)
    if query_type not in REGISTRY[original.tool_id].supported_query_types:
        raise ValueError("This collector does not support the original query type.")
    timeout = validate_tool_timeout(original.tool_id, timeout)
    # Conditional update prevents simultaneous clicks from queuing duplicate retries.
    claimed = db.execute(update(Investigation).where(
        Investigation.id == investigation.id,
        Investigation.status.not_in(("queued", "running")),
    ).values(status="queued", updated_at=datetime.now(UTC)).returning(Investigation.id)).scalar_one_or_none()
    if claimed is None:
        raise RetryConflict("Wait for the current collection to finish before retrying.")
    retry = ToolRun(
        investigation_id=investigation.id,
        tool_id=original.tool_id,
        display_name=REGISTRY[original.tool_id].display_name,
        status="queued",
        timeout_seconds=timeout,
        tool_commit=commit_for(original.tool_id),
        query_type=query_type.value,
        query_value=original.query_value or investigation.query,
        pivot_depth=original.pivot_depth,
        pivot_source_finding_id=original.pivot_source_finding_id,
        pivot_automatic=False,
    )
    db.add(retry)
    db.flush()
    db.add(AuditEvent(
        investigation_id=investigation.id,
        event_type="collector_retry_queued",
        details={"original_run_id": original.id, "retry_run_id": retry.id,
                 "tool_id": retry.tool_id, "timeout_seconds": timeout,
                 "original_pivot_automatic": original.pivot_automatic,
                 "original_status": original.status},
    ))
    return retry


def queue_deep_username_search(db: Session, investigation: Investigation) -> list[ToolRun]:
    if investigation.query_type != QueryType.USERNAME:
        raise ValueError("Deeper username collection requires a username investigation.")
    if investigation.extended_username_search:
        raise RetryConflict("This case already has extended username search enabled. Retry individual unsuccessful runs instead.")
    timeouts = get_tool_timeouts(db)
    claimed = db.execute(update(Investigation).where(
        Investigation.id == investigation.id,
        Investigation.status.not_in(("queued", "running")),
        Investigation.extended_username_search.is_(False),
    ).values(status="queued", extended_username_search=True, updated_at=datetime.now(UTC))
      .returning(Investigation.id)).scalar_one_or_none()
    if claimed is None:
        raise RetryConflict("Wait for the active collection to finish before starting a deeper search.")
    runs = []
    for tool_id in ("maigret", "naminter"):
        run = ToolRun(investigation_id=investigation.id, tool_id=tool_id,
                      display_name=REGISTRY[tool_id].display_name, status="queued",
                      timeout_seconds=timeouts[tool_id], tool_commit=commit_for(tool_id),
                      query_value=investigation.query, query_type="username", pivot_depth=0,
                      pivot_automatic=False)
        db.add(run)
        runs.append(run)
    investigation.selected_tools = list(dict.fromkeys([*investigation.selected_tools, "maigret", "naminter"]))
    db.flush()
    db.add(AuditEvent(investigation_id=investigation.id, event_type="username_deep_search_queued",
                      details={"run_ids": [run.id for run in runs], "tools": [run.tool_id for run in runs],
                               "maigret_all_sites": True, "retains_existing_evidence": True}))
    return runs
