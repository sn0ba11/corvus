import asyncio
import contextlib
import re
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from app.adapters import REGISTRY, get_adapter
from app.adapters.base import Adapter, ExecutionResult
from app.core.config import get_settings
from app.core.security import redact
from app.db.session import SessionLocal
from app.models.db import AuditEvent, BrowserRun, Finding, Investigation, ToolRun
from app.models.schemas import FindingRecord, FindingStatus, QueryType, RunStatus
from app.services.automatic_wayback import (
    apply_automatic_wayback_results,
    automatic_wayback_targets,
    discover_automatic_wayback,
)
from app.services.browser_agent import BrowserAgentClient, BrowserAgentError
from app.services.deduplication import canonical_url_score, dedupe_key
from app.services.location_intelligence import seed_declared_locations
from app.services.pivots import queue_automatic_identity_pivots
from app.services.profile_verification import (
    ProfileVerifier,
    apply_verification_results,
    verification_targets,
)
from app.services.runtime_settings import get_runtime_options


def _generic_display_name(value: object, platform: str | None) -> bool:
    if not isinstance(value, str) or not value.strip():
        return True
    candidate = value.strip().casefold()
    return bool(platform and candidate == platform.strip().casefold())


def _merge_profile_evidence(
    existing: dict[str, Any], incoming: dict[str, Any], platform: str | None
) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if value in (None, ""):
            continue
        current = merged.get(key)
        if key == "display_name":
            if _generic_display_name(value, platform):
                continue
            if current in (None, "") or _generic_display_name(current, platform):
                merged[key] = value
            continue
        if key in {"followers", "following"}:
            if current is None or (
                isinstance(current, int) and isinstance(value, int) and value > current
            ):
                merged[key] = value
            continue
        if key == "verified":
            merged[key] = bool(current) or bool(value)
            continue
        if current in (None, ""):
            merged[key] = value
    return merged


def _merge_platform_details(existing: object, incoming: object) -> list[dict[str, str]]:
    merged: dict[str, str] = {}
    for collection in (existing, incoming):
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            value = str(item.get("value") or "").strip()
            if label and value:
                merged[label] = value
    return [{"label": label, "value": value} for label, value in merged.items()]


PROMOTED_SOURCE_METADATA = {
    "avatar_local_path",
    "avatar_quality",
    "session_loaded",
    "session_warning",
    "authenticated_profile_warning",
    "avatar_error",
    "avatar_width",
    "avatar_height",
    "image_cache",
    "external_url",
    "is_business",
    "business_category_name",
    "public_email",
    "public_phone_number",
    "obfuscated_email",
    "obfuscated_phone_number",
    "recovery_hint_warning",
    "is_whatsapp_linked",
    "is_memorialized",
    "is_new_to_instagram",
    "authenticated_session",
    "platform_details",
    "timeline_events",
    "relationship",
}


# Fields that describe one run's verdict or handling, not the account. They always come
# from the latest run and are never carried forward from an earlier one.
_RUN_STATE_KEYS = frozenset(
    {
        "status",
        "found",
        "exists",
        "http_status",
        "response_time_s",
        "rank",
        "session_loaded",
        "authenticated_session",
        "rejected_fields",
        "parse_error",
        "candidate_generator",
        "error",
        "retained_from_earlier_run",
    }
)
_RUN_STATE_PREFIXES = ("maigret_", "sherlock_", "blackbird_", "naminter_", "social_analyzer_")
EARLIER_RUN_KEY = "retained_from_earlier_run"


def _carry_forward_earlier_fields(
    previous: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """Keep what an earlier run of the same collector found when a later run returns less.

    A retry is usually made because something went wrong, and a degraded second run used to
    replace the first run's details, so the case showed less after the retry than before it.
    Fields the new run did not return are kept and listed under ``retained_from_earlier_run``
    so they are not mistaken for fresh observations. A field the new run does return always
    wins, and a field carried forward once stays marked until a run returns it again.
    """
    merged = dict(incoming)
    retained: list[str] = []
    for key, value in previous.items():
        if key in _RUN_STATE_KEYS or key.startswith(_RUN_STATE_PREFIXES):
            continue
        if value in (None, "", [], {}):
            continue
        if key == "platform_details":
            merged[key] = _merge_platform_details(value, incoming.get(key))
            continue
        if incoming.get(key) in (None, "", [], {}):
            merged[key] = value
            retained.append(key)
    if retained:
        merged[EARLIER_RUN_KEY] = sorted(retained)
    return merged


EARLIER_RUN_LABEL = "Kept from an earlier run"


def _mark_earlier_run_details(merged: dict[str, Any]) -> None:
    """Show on the finding which details were not returned by a collector's latest run."""
    notes: list[str] = []
    sources = merged.get("source_metadata")
    for tool, metadata in sorted(sources.items()) if isinstance(sources, dict) else []:
        fields = metadata.get(EARLIER_RUN_KEY) if isinstance(metadata, dict) else None
        if isinstance(fields, list) and fields:
            notes.append(f"{tool}: {', '.join(str(field).replace('_', ' ') for field in fields)}")
    details = [
        item
        for item in merged.get("platform_details") or []
        if isinstance(item, dict) and item.get("label") != EARLIER_RUN_LABEL
    ]
    if notes:
        details.append({"label": EARLIER_RUN_LABEL, "value": "; ".join(notes)[:1000]})
    if details or "platform_details" in merged:
        merged["platform_details"] = details


def _merge_source_metadata(existing: dict[str, Any], finding: FindingRecord) -> dict[str, Any]:
    merged = dict(existing)
    source_metadata = dict(merged.get("source_metadata", {}))
    incoming = {
        key: value
        for key, value in finding.metadata.items()
        if key not in {"profile", "provenance", "source_metadata"}
    }
    previous = source_metadata.get(finding.source_tool)
    if isinstance(previous, dict):
        incoming = _carry_forward_earlier_fields(previous, incoming)
    source_metadata[finding.source_tool] = incoming
    merged["source_metadata"] = source_metadata
    for key in PROMOTED_SOURCE_METADATA:
        value = incoming.get(key)
        if value in (None, ""):
            continue
        if key == "platform_details":
            merged[key] = _merge_platform_details(merged.get(key), value)
        else:
            merged[key] = value
    _mark_earlier_run_details(merged)
    return merged


class JobManager:
    def __init__(self) -> None:
        settings = get_settings()
        self.investigation_limit = asyncio.Semaphore(settings.max_concurrent_investigations)
        self.collection_limit = asyncio.Semaphore(settings.max_concurrent_collectors)
        self.tool_limits = {
            key: asyncio.Semaphore(cls.concurrency_limit) for key, cls in REGISTRY.items()
        }
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.validation_tasks: dict[str, asyncio.Task[None]] = {}
        self.adapters: dict[str, list[Adapter]] = defaultdict(list)
        self.subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self.dropped_live_events: dict[str, int] = defaultdict(int)
        self.cancelled: set[str] = set()

    def start(self, investigation_id: str) -> None:
        self.cancelled.discard(investigation_id)
        if investigation_id not in self.tasks:
            task = asyncio.create_task(
                self._run(investigation_id), name=f"investigation:{investigation_id}"
            )
            self.tasks[investigation_id] = task
            task.add_done_callback(lambda _: self.tasks.pop(investigation_id, None))

    def start_profile_validation(self, investigation_id: str) -> bool:
        if investigation_id in self.validation_tasks:
            return False
        task = asyncio.create_task(
            self._validate_profiles(investigation_id),
            name=f"profile-validation:{investigation_id}",
        )
        self.validation_tasks[investigation_id] = task
        task.add_done_callback(lambda _: self.validation_tasks.pop(investigation_id, None))
        return True

    async def recover_stale(self) -> None:
        with SessionLocal.begin() as db:
            stale_runs = list(
                db.execute(
                    select(ToolRun.id, ToolRun.investigation_id, ToolRun.tool_id).where(
                        ToolRun.status == RunStatus.RUNNING
                    )
                )
            )
            for run_id, investigation_id, tool_id in stale_runs:
                self._mark_log_interrupted(investigation_id, run_id, tool_id)
            db.execute(
                update(ToolRun)
                .where(ToolRun.status == RunStatus.RUNNING)
                .values(
                    status=RunStatus.FAILED,
                    error_code="interrupted",
                    error_message="application restarted during execution",
                    finished_at=datetime.now(UTC),
                )
            )
            db.execute(
                update(Investigation)
                .where(Investigation.status == RunStatus.RUNNING)
                .values(status=RunStatus.FAILED, updated_at=datetime.now(UTC))
            )

    async def shutdown(self) -> None:
        investigation_ids = list(self.tasks)
        investigation_tasks = list(self.tasks.values())
        validation_tasks = list(self.validation_tasks.values())
        for investigation_id in investigation_ids:
            await self.cancel(investigation_id)
        for task in validation_tasks:
            task.cancel()
        outstanding = [
            task for task in (*investigation_tasks, *validation_tasks) if not task.done()
        ]
        if not outstanding:
            return
        _, pending = await asyncio.wait(outstanding, timeout=5.0)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=2.0)

    async def cancel(self, investigation_id: str) -> None:
        if investigation_id not in self.tasks:
            self.cancelled.discard(investigation_id)
            return
        self.cancelled.add(investigation_id)
        await asyncio.gather(
            *(a.cancel() for a in self.adapters.get(investigation_id, [])), return_exceptions=True
        )
        await self.emit(investigation_id, "status", {"status": RunStatus.CANCELLED})

    async def subscribe(self, investigation_id: str):  # type: ignore[no-untyped-def]
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self.subscribers[investigation_id].add(queue)
        try:
            yield queue
        finally:
            self.subscribers[investigation_id].discard(queue)

    async def emit(self, investigation_id: str, event: str, data: dict[str, Any]) -> None:
        message = {"event": event, "data": data}
        for queue in self.subscribers[investigation_id]:
            dropped = self.dropped_live_events[investigation_id]
            if dropped:
                warning = {
                    "event": "warning",
                    "data": {
                        "code": "live_event_backpressure",
                        "message": f"{dropped} live update(s) were dropped while the client was behind.",
                        "dropped_events": dropped,
                    },
                }
                try:
                    queue.put_nowait(warning)
                except asyncio.QueueFull:
                    self.dropped_live_events[investigation_id] += 1
                    continue
                else:
                    self.dropped_live_events[investigation_id] = 0
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self.dropped_live_events[investigation_id] += 1

    async def _run(self, investigation_id: str) -> None:
        started = time.monotonic()
        try:
            await self._run_inner(investigation_id)
        except Exception as exc:
            with SessionLocal.begin() as db:
                investigation = db.get(Investigation, investigation_id)
                if investigation:
                    investigation.status = RunStatus.FAILED
                    investigation.duration_ms = int((time.monotonic() - started) * 1000)
                    investigation.updated_at = datetime.now(UTC)
                    db.add(
                        AuditEvent(
                            investigation_id=investigation_id,
                            event_type="execution_failed",
                            details={"status": RunStatus.FAILED, "error": str(exc)},
                        )
                    )
            await self.emit(
                investigation_id,
                "complete",
                {"status": RunStatus.FAILED, "error": str(exc)},
            )
        finally:
            self.cancelled.discard(investigation_id)
            self.adapters.pop(investigation_id, None)

    async def _follow_automatic_pivots(
        self, investigation_id: str, *, after_verification: bool = False
    ) -> int:
        if investigation_id in self.cancelled:
            return 0
        run_ids = self._queue_automatic_pivots(investigation_id)
        if not run_ids:
            return 0
        source = "found during profile verification" if after_verification else "username pivots"
        await self.emit(
            investigation_id,
            "phase",
            {
                "phase": "username_pivots",
                "status": "running",
                "message": f"Following {len(run_ids)} {source}"
                if not after_verification
                else f"Following {len(run_ids)} follow-up searches {source}",
            },
        )
        await self.emit(
            investigation_id,
            "pivot",
            {"status": "queued", "run_count": len(run_ids), "after_verification": after_verification},
        )
        await asyncio.gather(*(self._run_tool(investigation_id, run_id) for run_id in run_ids))
        return len(run_ids)

    async def _run_inner(self, investigation_id: str) -> None:
        async with self.investigation_limit:
            started = time.monotonic()
            with SessionLocal.begin() as db:
                investigation = db.get(Investigation, investigation_id)
                if not investigation:
                    return
                investigation.status = RunStatus.RUNNING
                investigation.updated_at = datetime.now(UTC)
                runs = [run for run in investigation.runs if run.status == RunStatus.QUEUED.value]
                # Surface rich exact-profile results before the long broad sweeps
                # occupy every collection slot in a standard username search.
                runs.sort(key=lambda run: 0 if run.tool_id == "username_public_profiles" else 1)
            await self.emit(investigation_id, "status", {"status": RunStatus.RUNNING})
            await self.emit(
                investigation_id,
                "phase",
                {
                    "phase": "collection",
                    "status": "running",
                    "message": f"Running {len(runs)} selected collectors",
                },
            )
            await asyncio.gather(*(self._run_tool(investigation_id, run.id) for run in runs))
            # Retry after the collectors finish if their concurrent browser work
            # temporarily occupied the single browser capture slot.
            if (
                any(run.tool_id == "instaloader" for run in runs)
                and investigation.query_type == QueryType.USERNAME
                and investigation_id not in self.cancelled
            ):
                await self._capture_instagram_browser_fallback(
                    investigation_id,
                    run_id=next(run.id for run in runs if run.tool_id == "instaloader"),
                    username=investigation.query,
                )
            await self._follow_automatic_pivots(investigation_id)
            await self.emit(
                investigation_id,
                "phase",
                {
                    "phase": "profile_verification",
                    "status": "running",
                    "message": "Verifying the strongest discovered profile claims",
                },
            )
            verification_summary = await self._run_profile_verification(
                investigation_id, limit=24
            )
            if verification_summary.get("targets_checked"):
                await self.emit(investigation_id, "verification", verification_summary)
            # Verification reads the profiles themselves and can add usernames and email
            # addresses the collectors did not report, so look for follow-ups once more.
            # Only identifiers not yet searched are queued, and the depth limit still applies.
            await self._follow_automatic_pivots(investigation_id, after_verification=True)
            self._seed_declared_location_evidence(investigation_id)
            await self.emit(
                investigation_id,
                "phase",
                {
                    "phase": "historical_checks",
                    "status": "running",
                    "message": "Checking high-value profiles for archived history",
                },
            )
            wayback_summary = await self._run_automatic_wayback(investigation_id)
            if wayback_summary["targets_checked"]:
                await self.emit(investigation_id, "wayback", wayback_summary)
            final = (
                RunStatus.CANCELLED if investigation_id in self.cancelled else RunStatus.COMPLETED
            )
            with SessionLocal.begin() as db:
                investigation = db.get(Investigation, investigation_id)
                if investigation:
                    investigation.status = final
                    investigation.duration_ms = int((time.monotonic() - started) * 1000)
                    investigation.updated_at = datetime.now(UTC)
                    db.add(
                        AuditEvent(
                            investigation_id=investigation_id,
                            event_type="execution_completed",
                            details={"status": final},
                        )
                    )
            await self.emit(investigation_id, "complete", {"status": final})
            self.cancelled.discard(investigation_id)
            self.adapters.pop(investigation_id, None)

    async def _run_tool(self, investigation_id: str, run_id: str) -> None:
        with SessionLocal() as db:
            run = db.get(ToolRun, run_id)
            investigation = db.get(Investigation, investigation_id)
            if not run or not investigation:
                return
            tool_id, display_name, timeout = run.tool_id, run.display_name, run.timeout_seconds
            query = run.query_value or investigation.query
            query_type = QueryType(run.query_type or investigation.query_type)
            proxy_name = investigation.proxy_name
            extended_username_search = bool(investigation.extended_username_search)
        async with self.collection_limit, self.tool_limits[tool_id]:
            tool_started = time.monotonic()
            if investigation_id in self.cancelled:
                self._set_run_status(run_id, RunStatus.CANCELLED)
                return
            adapter = get_adapter(tool_id)
            self.adapters[investigation_id].append(adapter)
            health = await adapter.healthcheck()
            started_at = datetime.now(UTC)
            with SessionLocal.begin() as db:
                run = db.get(ToolRun, run_id)
                if run:
                    run.status, run.started_at, run.tool_version = (
                        RunStatus.RUNNING,
                        started_at,
                        health.version,
                    )
            settings = get_settings()
            output_dir = settings.data_dir / "raw" / investigation_id / run_id
            live_log_path = self._initialise_live_log(output_dir, tool_id)
            live_log_lock = asyncio.Lock()
            with SessionLocal() as db:
                runtime_options = get_runtime_options(db)
            session_mode = "anonymous"
            if (
                health.healthy
                and settings.browser_enabled
                and settings.browser_share_sessions
                and runtime_options["browser_share_sessions"]
                and tool_id in {"instaloader", "gallery_dl", "maigret"}
            ):
                try:
                    bundle = await BrowserAgentClient(settings).export_session(tool_id)
                except BrowserAgentError as exc:
                    with SessionLocal.begin() as db:
                        db.add(
                            AuditEvent(
                                investigation_id=investigation_id,
                                event_type="collector_session_unavailable",
                                details={
                                    "tool": tool_id,
                                    "run_id": run_id,
                                    "error_code": exc.code,
                                },
                            )
                        )
                else:
                    cookie_count = len(bundle.cookies)
                    scope_hosts = list(bundle.scope_hosts)
                    if bundle.authenticated and bundle.cookies:
                        adapter.set_session_context(
                            [cookie.runner_payload() for cookie in bundle.cookies],
                            user_agent=bundle.user_agent,
                            accept_language=bundle.accept_language,
                        )
                        browser_identity_supplied = bool(
                            bundle.user_agent and bundle.accept_language
                        )
                        session_mode = "managed_browser"
                        with SessionLocal.begin() as db:
                            db.add(
                                AuditEvent(
                                    investigation_id=investigation_id,
                                    event_type="collector_session_supplied",
                                    details={
                                        "tool": tool_id,
                                        "run_id": run_id,
                                        "cookie_count": cookie_count,
                                        "scope_hosts": scope_hosts,
                                        "browser_identity_supplied": browser_identity_supplied,
                                        "source": "managed_browser",
                                    },
                                )
                            )
                    else:
                        with SessionLocal.begin() as db:
                            db.add(
                                AuditEvent(
                                    investigation_id=investigation_id,
                                    event_type="collector_session_unavailable",
                                    details={
                                        "tool": tool_id,
                                        "run_id": run_id,
                                        "error_code": "browser_not_authenticated",
                                        "cookie_count": cookie_count,
                                        "scope_hosts": scope_hosts,
                                    },
                                )
                            )
                    del bundle
            await self.emit(
                investigation_id,
                "tool",
                {
                    "run_id": run_id,
                    "tool": tool_id,
                    "display_name": display_name,
                    "status": RunStatus.RUNNING,
                    "session_mode": session_mode,
                    "started_at": started_at.isoformat(),
                    "timeout_seconds": timeout,
                },
            )
            proxy = getattr(settings, proxy_name, None) if proxy_name else None
            try:
                command = adapter.build_command(query, query_type, output_dir, timeout, proxy)
                if tool_id == "maigret" and extended_username_search:
                    command.append("--all-sites")
            except Exception as exc:
                # A collector can reject a query during command construction.  The run is already
                # persisted as RUNNING at this point, so every setup failure must be terminalised
                # here rather than escaping and leaving a permanent zombie run in the case UI.
                known_query_error = isinstance(exc, ValueError)
                error_code = "invalid_query" if known_query_error else "collector_setup_error"
                error_message = (
                    redact(str(exc), query)[:1000]
                    if known_query_error and str(exc).strip()
                    else f"collector setup failed ({type(exc).__name__})"
                )
                self._append_live_log(
                    live_log_path,
                    "output",
                    f"Collector setup failed: {error_message}",
                )
                result = ExecutionResult(
                    None,
                    "",
                    "",
                    error_code=error_code,
                    error_message=error_message,
                )
                raw_ref = str(live_log_path)
                self._persist_results(
                    run_id,
                    investigation_id,
                    RunStatus.FAILED,
                    result,
                    [],
                    raw_ref,
                    int((time.monotonic() - tool_started) * 1000),
                )
                await self.emit(
                    investigation_id,
                    "tool",
                    {
                        "run_id": run_id,
                        "tool": tool_id,
                        "display_name": display_name,
                        "status": RunStatus.FAILED,
                        "findings": 0,
                        "rate_limited": False,
                        "session_mode": session_mode,
                        "timeout_seconds": timeout,
                    },
                )
                return

            async def log(channel: str, line: str) -> None:
                clean = redact(line, query)
                async with live_log_lock:
                    self._append_live_log(live_log_path, channel, clean)
                await self.emit(investigation_id, "log", {"tool": tool_id, "message": clean})

            if not health.healthy:
                result = ExecutionResult(
                    None,
                    "",
                    "",
                    error_code="unavailable",
                    error_message=health.message,
                )
            elif tool_id == "instaloader" and session_mode == "anonymous":
                # Instagram refuses logged-out lookups on the first request, so running the
                # collector would only leave a rate-limited run on the case.
                result = ExecutionResult(
                    None,
                    "",
                    "",
                    error_code="session_required",
                    error_message=(
                        "Instaloader API lookup requires an Instagram session and was not run. "
                        "Corvus will separately attempt the public profile with its managed "
                        "browser; the browser lookup does not require signing in."
                    ),
                )
                await log("output", result.error_message or "")
            else:
                result = await adapter.execute(command, output_dir, timeout, query, log)
            self._append_live_log(live_log_path, "output", "Collector execution finished.")
            raw_ref = str(live_log_path)
            if not result.error_code and not result.timed_out:
                coverage_check = getattr(adapter, "coverage_warning", None)
                coverage_note = coverage_check(result) if coverage_check else None
                if coverage_note:
                    result.error_code = "incomplete_coverage"
                    result.error_message = coverage_note
            if investigation_id in self.cancelled:
                status = RunStatus.CANCELLED
            elif result.timed_out:
                status = RunStatus.TIMED_OUT
            elif result.error_code and not (
                result.error_code in {"rate_limited", "incomplete_coverage"}
                and result.returncode == 0
            ):
                status = RunStatus.FAILED
            else:
                status = RunStatus.COMPLETED
            findings: list[FindingRecord] = []
            # An API session is optional for public Instagram profile pages.
            # Instaloader can fail without a login; the managed browser must still
            # attempt the public page and retain an evidence-backed observation.
            browser_fallback_needed = (
                tool_id == "instaloader"
                and session_mode == "anonymous"
                and query_type == QueryType.USERNAME
            )
            if status in {RunStatus.COMPLETED, RunStatus.TIMED_OUT, RunStatus.FAILED}:
                try:
                    findings = adapter.normalise_results(
                        adapter.parse_results(result, output_dir), query, query_type, raw_ref
                    )
                    session_rejected = any(
                        finding.metadata.get("session_loaded") is False for finding in findings
                    )
                    if session_mode == "managed_browser" and session_rejected:
                        browser_fallback_needed = tool_id == "instaloader"
                        session_mode = (
                            "browser_api_unverified" if browser_fallback_needed else "anonymous"
                        )
                        with SessionLocal.begin() as db:
                            db.add(
                                AuditEvent(
                                    investigation_id=investigation_id,
                                    event_type=(
                                        "collector_session_verification_inconclusive"
                                        if browser_fallback_needed else "collector_session_rejected"
                                    ),
                                    details={
                                        "tool": tool_id,
                                        "run_id": run_id,
                                        "error_code": (
                                            "api_login_verification_inconclusive"
                                            if browser_fallback_needed else "collector_rejected_session"
                                        ),
                                        "source": "managed_browser",
                                    },
                                )
                            )
                    elif session_mode == "managed_browser":
                        for finding in findings:
                            finding.metadata["authenticated_session"] = "managed_browser"
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    status, result.error_code, result.error_message = (
                        RunStatus.FAILED,
                        "parse_error",
                        str(exc),
                    )
            self._persist_results(
                run_id,
                investigation_id,
                status,
                result,
                findings,
                raw_ref,
                int((time.monotonic() - tool_started) * 1000),
            )
            await self.emit(
                investigation_id,
                "tool",
                {
                    "run_id": run_id,
                    "tool": tool_id,
                    "display_name": display_name,
                    "status": status,
                    "findings": len(findings),
                    "rate_limited": result.rate_limited,
                    "session_mode": session_mode,
                    "timeout_seconds": timeout,
                },
            )
            if browser_fallback_needed and query_type == QueryType.USERNAME:
                await self._capture_instagram_browser_fallback(
                    investigation_id, run_id=run_id, username=query
                )

    async def _capture_instagram_browser_fallback(
        self, investigation_id: str, *, run_id: str, username: str
    ) -> None:
        """Read a public Instagram profile through the existing managed browser.

        Authentication is optional for public pages. Never bypass a challenge,
        infer a nonexistent account from a login wall, or invent profile details.
        Preserve the browser evidence for any positive observation.
        """
        if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
            return
        if investigation_id in self.cancelled or not get_settings().browser_enabled:
            return
        client = BrowserAgentClient(get_settings())
        target = f"https://www.instagram.com/{username}/"
        try:
            state = await client.status()
            if (
                not state.available
                or not state.profile_ready
                or state.active_job
                or state.pending_job_id
            ):
                reason = "browser_unavailable_or_busy"
                raise BrowserAgentError(reason, "managed browser cannot start a profile capture")
        except BrowserAgentError as exc:
            with SessionLocal.begin() as db:
                db.add(AuditEvent(
                    investigation_id=investigation_id,
                    event_type="instagram_browser_fallback_skipped",
                    details={"source_run_id": run_id, "reason": exc.code},
                ))
            return

        with SessionLocal.begin() as db:
            investigation = db.get(Investigation, investigation_id)
            if investigation is None:
                return
            existing = db.scalar(select(BrowserRun).where(
                BrowserRun.investigation_id == investigation_id,
                BrowserRun.collector_id == "browser_instagram",
                BrowserRun.requested_url == target,
                BrowserRun.status.in_(("completed", "running", "manual_action_required")),
            ))
            if existing is not None:
                return
            browser_run = BrowserRun(
                investigation_id=investigation_id, collector_id="browser_instagram",
                requested_url=target, status="running", started_at=datetime.now(UTC),
            )
            db.add(browser_run)
            db.flush()
            browser_run_id = browser_run.id
            db.add(AuditEvent(
                investigation_id=investigation_id,
                event_type="instagram_browser_fallback_started",
                details={"source_run_id": run_id, "browser_run_id": browser_run_id},
            ))
        try:
            capture = await client.capture(
                job_id=browser_run_id, investigation_id=investigation_id,
                url=target, collector_id="browser_instagram",
            )
            # Shared persistence/normalisation with analyst-initiated browser captures.
            from app.api.routes import apply_browser_capture_result
            from app.services.managed_browser_extraction import social_profile_was_read

            read = capture.status == "completed" and social_profile_was_read(capture)

            with SessionLocal.begin() as db:
                investigation = db.get(Investigation, investigation_id)
                stored_run = db.get(BrowserRun, browser_run_id)
                if investigation is None or stored_run is None:
                    return
                imported = apply_browser_capture_result(
                    db, investigation, stored_run, capture, resumed=False
                )
                if capture.status != "completed":
                    stored_run.status = "cancelled"
                    stored_run.finished_at = datetime.now(UTC)
                    stored_run.error_message = "browser challenge requires analyst action"
                db.add(AuditEvent(
                    investigation_id=investigation_id,
                    event_type="instagram_browser_fallback_result",
                    details={"source_run_id": run_id, "browser_run_id": browser_run_id,
                             "status": capture.status if read else (
                                 "no_profile" if capture.status == "completed" else capture.status
                             ), "imported": imported},
                ))
            if capture.status != "completed":
                # An unattended fallback must never hold the investigator's browser.
                await client.abandon(job_id=browser_run_id, investigation_id=investigation_id)
            await self.emit(investigation_id, "browser_fallback", {
                "source_run_id": run_id, "browser_run_id": browser_run_id,
                "status": capture.status if read else (
                    "no_profile" if capture.status == "completed" else capture.status
                ), "imported": imported,
            })
        except Exception as exc:  # noqa: BLE001 - optional fallback must not fail the case
            error_code = exc.code if isinstance(exc, BrowserAgentError) else type(exc).__name__
            with SessionLocal.begin() as db:
                run = db.get(BrowserRun, browser_run_id)
                if run is not None:
                    run.status = "failed"
                    run.error_code = error_code[:80]
                    run.error_message = str(exc)[:300]
                    run.finished_at = datetime.now(UTC)
                db.add(AuditEvent(
                    investigation_id=investigation_id,
                    event_type="instagram_browser_fallback_failed",
                    details={"source_run_id": run_id, "browser_run_id": browser_run_id,
                             "error_code": error_code},
                ))

    def live_snapshot(self, investigation_id: str) -> dict[str, Any] | None:
        """Return authoritative persisted state for SSE reconnect/resynchronisation."""

        with SessionLocal() as db:
            investigation = db.get(Investigation, investigation_id)
            if investigation is None:
                return None
            runs = list(
                db.scalars(
                    select(ToolRun)
                    .where(ToolRun.investigation_id == investigation_id)
                    .order_by(ToolRun.started_at, ToolRun.id)
                )
            )
            return {
                "status": investigation.status,
                "runs": [
                    {
                        "run_id": run.id,
                        "tool": run.tool_id,
                        "display_name": run.display_name,
                        "status": run.status,
                        "started_at": (
                            (run.started_at.replace(tzinfo=UTC) if run.started_at.tzinfo is None else run.started_at)
                            .astimezone(UTC)
                            .isoformat()
                            if run.started_at
                            else None
                        ),
                        "timeout_seconds": run.timeout_seconds,
                    }
                    for run in runs
                ],
            }

    async def _validate_profiles(self, investigation_id: str) -> None:
        await self.emit(
            investigation_id,
            "phase",
            {
                "phase": "profile_verification",
                "status": "running",
                "message": "Revalidating account existence and identifiers",
            },
        )
        try:
            summary = await self._run_profile_verification(investigation_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_name = type(exc).__name__
            with SessionLocal.begin() as db:
                if db.get(Investigation, investigation_id) is not None:
                    db.add(
                        AuditEvent(
                            investigation_id=investigation_id,
                            event_type="profile_validation_failed",
                            details={"error_type": error_name},
                        )
                    )
            await self.emit(
                investigation_id,
                "phase",
                {
                    "phase": "profile_verification",
                    "status": "failed",
                    "message": f"Account validation failed ({error_name})",
                },
            )
            return
        await self.emit(investigation_id, "verification", summary)
        await self.emit(
            investigation_id,
            "phase",
            {
                "phase": "profile_verification",
                "status": "completed",
                "message": f"Validated {summary.get('targets_checked', 0)} account claims",
            },
        )

    @staticmethod
    async def _run_automatic_wayback(investigation_id: str) -> dict[str, int]:
        with SessionLocal() as db:
            if not get_runtime_options(db)["automatic_wayback"]:
                return {
                    "targets_checked": 0,
                    "targets_archived": 0,
                    "captures_found": 0,
                    "targets_failed": 0,
                }
            investigation = db.get(Investigation, investigation_id)
            if not investigation:
                return {
                    "targets_checked": 0,
                    "targets_archived": 0,
                    "captures_found": 0,
                    "targets_failed": 0,
                }
            targets = automatic_wayback_targets(investigation)
        if not targets:
            return {
                "targets_checked": 0,
                "targets_archived": 0,
                "captures_found": 0,
                "targets_failed": 0,
            }
        results = await discover_automatic_wayback(targets)
        with SessionLocal.begin() as db:
            investigation = db.get(Investigation, investigation_id)
            if not investigation:
                return {
                    "targets_checked": 0,
                    "targets_archived": 0,
                    "captures_found": 0,
                    "targets_failed": 0,
                }
            return apply_automatic_wayback_results(db, investigation, results)

    @staticmethod
    async def _run_profile_verification(
        investigation_id: str, *, limit: int | None = None
    ) -> dict[str, Any]:
        with SessionLocal() as db:
            investigation = db.get(Investigation, investigation_id)
            if investigation is None:
                return {"targets_checked": 0, "states": {}}
            targets = (
                verification_targets(investigation, limit=limit)
                if limit is not None
                else verification_targets(investigation)
            )
        if not targets:
            return {"targets_checked": 0, "states": {}}
        results = await ProfileVerifier().verify_many(targets)
        with SessionLocal.begin() as db:
            investigation = db.get(Investigation, investigation_id)
            if investigation is None:
                return {"targets_checked": 0, "states": {}}
            states = apply_verification_results(investigation, results)
            db.add(
                AuditEvent(
                    investigation_id=investigation_id,
                    event_type="profile_verification_completed",
                    details={
                        "targets_checked": len(results),
                        "states": states,
                        "methods": sorted({result.method for result in results}),
                    },
                )
            )
        return {"targets_checked": len(results), "states": states}

    @staticmethod
    def _seed_declared_location_evidence(investigation_id: str) -> int:
        with SessionLocal.begin() as db:
            investigation = db.get(Investigation, investigation_id)
            if not investigation:
                return 0
            added = seed_declared_locations(db, investigation)
            if added:
                db.add(
                    AuditEvent(
                        investigation_id=investigation_id,
                        event_type="declared_locations_promoted",
                        details={"observation_count": added},
                    )
                )
            return added

    @staticmethod
    def _queue_automatic_pivots(investigation_id: str) -> list[str]:
        with SessionLocal.begin() as db:
            investigation = db.get(Investigation, investigation_id)
            if not investigation:
                return []
            runs = queue_automatic_identity_pivots(db, investigation)
            db.flush()
            return [run.id for run in runs]

    @staticmethod
    def _initialise_live_log(output_dir: Path, tool_id: str) -> Path:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = output_dir / "process-output.txt"
        started = datetime.now(UTC).isoformat()
        path.write_text(
            f"LIVE PROCESS OUTPUT\nTool: {tool_id}\nStarted: {started}\n\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    @staticmethod
    def _append_live_log(path: Path, channel: str, line: str) -> None:
        if not line:
            return
        safe_channel = channel.upper() if channel in {"stdout", "stderr"} else "OUTPUT"
        timestamp = datetime.now(UTC).isoformat()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] [{safe_channel}] {line}\n")
            handle.flush()

    @staticmethod
    def _mark_log_interrupted(investigation_id: str, run_id: str, tool_id: str) -> None:
        output_dir = get_settings().data_dir / "raw" / investigation_id / run_id
        with contextlib.suppress(OSError):
            output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = output_dir / "process-output.txt"
            if not path.exists():
                path.write_text(
                    f"PARTIAL PROCESS OUTPUT\nTool: {tool_id}\n\n",
                    encoding="utf-8",
                )
                path.chmod(0o600)
            JobManager._append_live_log(
                path,
                "output",
                "Corvus restarted while this collector was executing. "
                "The output above is the last process output received before interruption.",
            )

    @staticmethod
    def _set_run_status(run_id: str, status: RunStatus) -> None:
        with SessionLocal.begin() as db:
            run = db.get(ToolRun, run_id)
            if run:
                run.status, run.finished_at = status, datetime.now(UTC)

    @staticmethod
    def _persist_results(
        run_id: str,
        investigation_id: str,
        status: RunStatus,
        result: Any,
        findings: list[FindingRecord],
        raw_ref: str,
        duration_ms: int,
    ) -> None:
        with SessionLocal.begin() as db:
            run = db.get(ToolRun, run_id)
            if not run:
                return
            run.status, run.finished_at = status, datetime.now(UTC)
            run.duration_ms = duration_ms
            run.error_code, run.error_message = result.error_code, result.error_message
            run.rate_limited, run.raw_output_reference = result.rate_limited, raw_ref
            existing = list(
                db.scalars(select(Finding).where(Finding.investigation_id == investigation_id))
            )
            keys = {}
            for item in existing:
                record = FindingRecord(
                    query_type=QueryType(item.query_type),
                    queried_value=item.queried_value,
                    finding_type=item.finding_type,
                    platform=item.platform,
                    value=item.value,
                    canonical_url=item.canonical_url,
                    status=item.status,
                    source_tool=item.source_tool,
                    source_record_id=item.source_record_id,
                )
                keys[dedupe_key(record)] = item
            status_rank = {
                FindingStatus.ERROR.value: 0,
                FindingStatus.RATE_LIMITED.value: 1,
                FindingStatus.NOT_FOUND.value: 2,
                FindingStatus.UNKNOWN.value: 3,
                FindingStatus.LIKELY.value: 4,
                FindingStatus.FOUND.value: 5,
            }
            for finding in findings:
                key = dedupe_key(finding)
                if key in keys:
                    stored = keys[key]
                    if status_rank.get(finding.status.value, 0) > status_rank.get(stored.status, 0):
                        stored.status = finding.status.value
                    if finding.confidence is not None and (
                        stored.confidence is None or finding.confidence > stored.confidence
                    ):
                        stored.confidence = finding.confidence
                    if finding.source_tool not in stored.source_tools:
                        stored.source_tools = [*stored.source_tools, finding.source_tool]
                    if canonical_url_score(finding.canonical_url) > canonical_url_score(
                        stored.canonical_url
                    ):
                        stored.canonical_url = finding.canonical_url
                        stored.display_url = finding.display_url
                    provenance = list(stored.extra.get("provenance", []))
                    provenance.append(
                        {
                            "tool": finding.source_tool,
                            "record_id": finding.source_record_id,
                            "raw": raw_ref,
                        }
                    )
                    existing_profile = dict(stored.extra.get("profile", {}))
                    incoming_profile = finding.profile.model_dump(exclude_none=True)
                    merged_profile = _merge_profile_evidence(
                        existing_profile,
                        incoming_profile,
                        stored.platform or finding.platform,
                    )
                    stored.extra = {
                        **_merge_source_metadata(stored.extra, finding),
                        "profile": merged_profile,
                        "provenance": provenance,
                    }
                    if not stored.title and finding.profile.display_name:
                        stored.title = finding.profile.display_name
                    if not stored.description and finding.profile.biography:
                        stored.description = finding.profile.biography
                    continue
                stored = Finding(
                    investigation_id=investigation_id,
                    tool_run_id=run_id,
                    query_type=finding.query_type,
                    queried_value=finding.queried_value,
                    finding_type=finding.finding_type,
                    platform=finding.platform,
                    value=finding.value,
                    canonical_url=finding.canonical_url,
                    display_url=finding.display_url,
                    status=finding.status,
                    confidence=finding.confidence,
                    title=finding.title,
                    description=finding.description,
                    extra={
                        **_merge_source_metadata({}, finding),
                        **finding.metadata,
                        "profile": finding.profile.model_dump(exclude_none=True),
                        "provenance": [
                            {
                                "tool": finding.source_tool,
                                "record_id": finding.source_record_id,
                                "raw": raw_ref,
                            }
                        ],
                    },
                    source_tool=finding.source_tool,
                    source_tools=[finding.source_tool],
                    source_record_id=finding.source_record_id,
                    observed_at=finding.observed_at,
                    raw_output_reference=raw_ref,
                )
                db.add(stored)
                keys[key] = stored


job_manager = JobManager()
