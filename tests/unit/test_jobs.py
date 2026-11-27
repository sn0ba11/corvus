import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.adapters.base import ExecutionResult, HealthResult
from app.core.config import Settings
from app.db.session import SessionLocal, engine
from app.models.db import AuditEvent, Base, Finding, Investigation, LocationObservation, ToolRun
from app.models.schemas import FindingRecord, FindingStatus, ProfileEvidence, QueryType
from app.services import jobs
from app.services.browser_agent import BrowserSessionBundle, BrowserSessionCookie
from app.services.jobs import JobManager


@pytest.mark.asyncio
async def test_live_event_backpressure_is_reported_after_queue_recovers() -> None:
    manager = JobManager()
    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=2)
    manager.subscribers["case-1"].add(queue)  # type: ignore[arg-type]
    queue.put_nowait({"event": "old-1", "data": {}})
    queue.put_nowait({"event": "old-2", "data": {}})

    await manager.emit("case-1", "status", {"status": "running"})

    assert manager.dropped_live_events["case-1"] == 1
    queue.get_nowait()
    queue.get_nowait()

    await manager.emit("case-1", "status", {"status": "completed"})

    warning = queue.get_nowait()
    current = queue.get_nowait()
    assert warning["event"] == "warning"
    assert warning["data"]["code"] == "live_event_backpressure"  # type: ignore[index]
    assert warning["data"]["dropped_events"] == 1  # type: ignore[index]
    assert current == {"event": "status", "data": {"status": "completed"}}
    assert manager.dropped_live_events["case-1"] == 0


def test_live_snapshot_returns_persisted_authoritative_run_state() -> None:
    Base.metadata.create_all(engine)
    started = datetime.now(UTC)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Live snapshot",
            query=f"snapshot-{uuid4()}",
            query_type="username",
            selected_tools=["nexfil", "github_discovery"],
            tags=[],
            status="running",
        )
        running = ToolRun(
            tool_id="nexfil",
            display_name="Nexfil",
            status="running",
            timeout_seconds=90,
            started_at=started,
        )
        queued = ToolRun(
            tool_id="github_discovery",
            display_name="GitHub Public Discovery",
            status="queued",
            timeout_seconds=30,
        )
        investigation.runs.extend([running, queued])
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id
        running_id = running.id
        queued_id = queued.id

    snapshot = JobManager().live_snapshot(investigation_id)

    assert snapshot is not None
    assert snapshot["status"] == "running"
    by_id = {item["run_id"]: item for item in snapshot["runs"]}
    assert by_id[running_id]["status"] == "running"
    assert by_id[running_id]["started_at"] == started.isoformat()
    assert by_id[running_id]["timeout_seconds"] == 90
    assert by_id[queued_id]["status"] == "queued"
    assert by_id[queued_id]["started_at"] is None


@pytest.mark.asyncio
async def test_run_exception_reaches_failed_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Terminal failure guarantee",
            query=f"failure-{uuid4()}@example.org",
            query_type="email",
            selected_tools=[],
            tags=[],
            status="queued",
        )
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id

    manager = JobManager()
    emitted: list[tuple[str, dict[str, object]]] = []

    async def fail_run(_investigation_id: str) -> None:
        raise RuntimeError("post-collection failure")

    async def fake_emit(_investigation_id: str, event: str, data: dict[str, object]) -> None:
        emitted.append((event, data))

    monkeypatch.setattr(manager, "_run_inner", fail_run)
    monkeypatch.setattr(manager, "emit", fake_emit)

    await manager._run(investigation_id)

    with SessionLocal() as db:
        stored = db.get(Investigation, investigation_id)
        assert stored is not None
        assert stored.status == "failed"
        audit = db.scalars(
            select(AuditEvent).where(AuditEvent.investigation_id == investigation_id)
        ).all()
    assert any(event.event_type == "execution_failed" for event in audit)
    assert emitted[-1][0] == "complete"
    assert emitted[-1][1]["status"] == "failed"
    assert investigation_id not in manager.cancelled
    assert investigation_id not in manager.adapters


@pytest.mark.asyncio
async def test_restart_executes_only_newly_queued_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Queued-only execution",
            query=f"queued-{uuid4()}@example.org",
            query_type="email",
            selected_tools=["blackbird"],
            tags=[],
            status="completed",
        )
        completed = ToolRun(
            tool_id="blackbird",
            display_name="Blackbird",
            status="completed",
            timeout_seconds=60,
        )
        queued = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="queued",
            timeout_seconds=60,
            query_value="pivot_handle",
            query_type="username",
            pivot_depth=1,
        )
        investigation.runs.extend([completed, queued])
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id
        completed_id = completed.id
        queued_id = queued.id

    manager = JobManager()
    called: list[str] = []
    emitted: list[tuple[str, dict[str, object]]] = []

    async def fake_run_tool(investigation_id: str, run_id: str) -> None:
        called.append(run_id)
        with SessionLocal.begin() as db:
            run = db.get(ToolRun, run_id)
            assert run is not None
            run.status = "completed"

    async def fake_emit(investigation_id: str, event: str, data: dict[str, object]) -> None:
        emitted.append((event, data))

    monkeypatch.setattr(manager, "_run_tool", fake_run_tool)
    monkeypatch.setattr(manager, "_queue_automatic_pivots", lambda investigation_id: [])
    monkeypatch.setattr(manager, "emit", fake_emit)

    await manager._run(investigation_id)

    assert called == [queued_id]
    assert [data["phase"] for event, data in emitted if event == "phase"] == [
        "collection",
        "profile_verification",
        "historical_checks",
    ]
    with SessionLocal() as db:
        completed_run = db.get(ToolRun, completed_id)
        queued_run = db.get(ToolRun, queued_id)
        stored_investigation = db.get(Investigation, investigation_id)
        assert completed_run is not None
        assert queued_run is not None
        assert stored_investigation is not None
        assert completed_run.status == "completed"
        assert queued_run.status == "completed"
        assert stored_investigation.status == "completed"


def test_corroborating_collector_promotes_sherlock_unknown_to_found() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Sherlock corroboration",
            query=f"corroborate-{uuid4()}",
            query_type="username",
            selected_tools=["sherlock", "blackbird"],
            tags=[],
            status="running",
        )
        sherlock_run = ToolRun(
            tool_id="sherlock",
            display_name="Sherlock",
            status="completed",
            timeout_seconds=60,
        )
        blackbird_run = ToolRun(
            tool_id="blackbird",
            display_name="Blackbird",
            status="running",
            timeout_seconds=60,
        )
        existing = Finding(
            query_type="username",
            queried_value=investigation.query,
            finding_type="profile",
            platform="Example Social",
            value=investigation.query,
            canonical_url="https://social.example/profile",
            status="unknown",
            confidence=0.15,
            extra={"profile": {"display_name": "Nova Example"}, "provenance": []},
            source_tool="sherlock",
            source_tools=["sherlock"],
            observed_at=datetime(2026, 7, 20, tzinfo=UTC),
        )
        investigation.runs.extend([sherlock_run, blackbird_run])
        investigation.findings.append(existing)
        sherlock_run.findings.append(existing)
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id
        run_id = blackbird_run.id
        finding_id = existing.id
        query = investigation.query

    JobManager._persist_results(
        run_id,
        investigation_id,
        jobs.RunStatus.COMPLETED,
        ExecutionResult(0, "", ""),
        [
            FindingRecord(
                query_type=QueryType.USERNAME,
                queried_value=query,
                finding_type="profile",
                platform="Example Social",
                value=query,
                canonical_url="https://social.example/profile",
                status=FindingStatus.FOUND,
                confidence=0.9,
                profile=ProfileEvidence(display_name="Example Social"),
                source_tool="blackbird",
                source_record_id="confirmed",
            )
        ],
        "/data/raw/confirmed.txt",
        10,
    )

    with SessionLocal() as db:
        stored = db.get(Finding, finding_id)
        assert stored is not None
        assert stored.status == "found"
        assert stored.confidence == 0.9
        assert stored.extra["profile"]["display_name"] == "Nova Example"
        assert stored.source_tools == ["sherlock", "blackbird"]


def test_duolingo_api_and_public_profile_merge_into_one_account() -> None:
    Base.metadata.create_all(engine)
    query = f"duolingo-{uuid4()}"
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Duolingo merge",
            query=query,
            query_type="username",
            selected_tools=["blackbird", "maigret"],
            tags=[],
            status="running",
        )
        blackbird_run = ToolRun(
            tool_id="blackbird",
            display_name="Blackbird",
            status="completed",
            timeout_seconds=60,
        )
        maigret_run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="running",
            timeout_seconds=60,
        )
        existing = Finding(
            query_type="username",
            queried_value=query,
            finding_type="profile",
            platform="Duolingo",
            value=query,
            canonical_url=(
                f"https://www.duolingo.com/2017-06-30/users?username={query}&_=1628308619574"
            ),
            display_url=(
                f"https://www.duolingo.com/2017-06-30/users?username={query}&_=1628308619574"
            ),
            status="found",
            confidence=0.8,
            extra={
                "profile": {"avatar_url": "https://cdn.example/avatar.png"},
                "provenance": [{"tool": "blackbird", "record_id": "2", "raw": "blackbird"}],
                "source_metadata": {"blackbird": {"status": "FOUND"}},
            },
            source_tool="blackbird",
            source_tools=["blackbird"],
            observed_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        investigation.runs.extend([blackbird_run, maigret_run])
        investigation.findings.append(existing)
        blackbird_run.findings.append(existing)
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id
        run_id = maigret_run.id
        existing_id = existing.id

    JobManager._persist_results(
        run_id,
        investigation_id,
        jobs.RunStatus.COMPLETED,
        ExecutionResult(0, "", ""),
        [
            FindingRecord(
                query_type=QueryType.USERNAME,
                queried_value=query,
                finding_type="profile",
                platform="Duolingo",
                value=query,
                canonical_url=f"https://www.duolingo.com/profile/{query}",
                display_url=f"https://www.duolingo.com/profile/{query}",
                status=FindingStatus.FOUND,
                confidence=0.95,
                metadata={"status": "Claimed", "streak": 1393},
                profile=ProfileEvidence(
                    account_id=query,
                    display_name=query,
                    avatar_url="https://cdn.example/avatar.png",
                ),
                source_tool="maigret",
                source_record_id="168911392",
            )
        ],
        "/data/raw/maigret.txt",
        10,
    )

    with SessionLocal() as db:
        findings = list(
            db.scalars(select(Finding).where(Finding.investigation_id == investigation_id))
        )
        assert len(findings) == 1
        stored = findings[0]
        assert stored.id == existing_id
        assert stored.canonical_url == f"https://www.duolingo.com/profile/{query}"
        assert stored.source_tools == ["blackbird", "maigret"]
        assert [item["tool"] for item in stored.extra["provenance"]] == [
            "blackbird",
            "maigret",
        ]
        assert set(stored.extra["source_metadata"]) == {"blackbird", "maigret"}
        assert stored.extra["profile"]["account_id"] == query


def test_instaloader_metadata_survives_merge_into_existing_profile() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Instaloader merge",
            query=f"instagram-{uuid4()}",
            query_type="username",
            selected_tools=["maigret", "instaloader"],
            tags=[],
            status="running",
        )
        maigret_run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
        instaloader_run = ToolRun(
            tool_id="instaloader",
            display_name="Instaloader",
            status="running",
            timeout_seconds=60,
        )
        existing = Finding(
            query_type="username",
            queried_value=investigation.query,
            finding_type="profile",
            platform="Instagram",
            value=investigation.query,
            canonical_url="https://www.instagram.com/example",
            status="found",
            confidence=0.8,
            extra={"profile": {"display_name": "Example Person"}, "provenance": []},
            source_tool="maigret",
            source_tools=["maigret"],
        )
        investigation.runs.extend([maigret_run, instaloader_run])
        investigation.findings.append(existing)
        maigret_run.findings.append(existing)
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id
        run_id = instaloader_run.id
        finding_id = existing.id
        query = investigation.query

    JobManager._persist_results(
        run_id,
        investigation_id,
        jobs.RunStatus.COMPLETED,
        ExecutionResult(0, "", ""),
        [
            FindingRecord(
                query_type=QueryType.USERNAME,
                queried_value=query,
                finding_type="instagram_profile",
                platform="Instagram",
                value=query,
                canonical_url="https://www.instagram.com/example",
                status=FindingStatus.FOUND,
                confidence=0.95,
                metadata={
                    "avatar_local_path": "/data/raw/case/run/profile_pic.jpg",
                    "avatar_quality": "hd",
                    "session_loaded": True,
                    "image_cache": {
                        "source": "collector_local_file",
                        "sha256": "a" * 64,
                    },
                },
                profile=ProfileEvidence(
                    account_id="12345",
                    avatar_url="https://cdn.example/profile.jpg",
                ),
                source_tool="instaloader",
                source_record_id="12345",
            )
        ],
        "/data/raw/instaloader.txt",
        10,
    )

    with SessionLocal() as db:
        stored = db.get(Finding, finding_id)
        assert stored is not None
        assert stored.status == "found"
        assert stored.confidence == 0.95
        assert stored.source_tools == ["maigret", "instaloader"]
        assert stored.extra["avatar_local_path"].endswith("profile_pic.jpg")
        assert stored.extra["avatar_quality"] == "hd"
        assert stored.extra["image_cache"]["source"] == "collector_local_file"
        assert stored.extra["source_metadata"]["instaloader"]["session_loaded"] is True
        assert stored.extra["profile"]["display_name"] == "Example Person"
        assert stored.extra["profile"]["account_id"] == "12345"


def test_completed_workflow_promotes_declared_profile_locations() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Automatic declared location",
            query=f"location-{uuid4()}",
            query_type="username",
            selected_tools=["maigret"],
            tags=[],
            status="completed",
        )
        run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
        finding = Finding(
            query_type="username",
            queried_value=investigation.query,
            finding_type="profile",
            platform="Example Social",
            value=investigation.query,
            status="found",
            extra={"profile": {"location": "Dublin", "account_id": "location-user"}},
            source_tool="maigret",
            source_tools=["maigret"],
            observed_at=datetime(2026, 7, 20, tzinfo=UTC),
        )
        investigation.runs.append(run)
        investigation.findings.append(finding)
        run.findings.append(finding)
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id

    assert JobManager._seed_declared_location_evidence(investigation_id) == 1
    assert JobManager._seed_declared_location_evidence(investigation_id) == 0
    with SessionLocal() as db:
        observations = list(
            db.scalars(
                select(LocationObservation).where(
                    LocationObservation.investigation_id == investigation_id
                )
            )
        )
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "declared_locations_promoted",
            )
        )
    assert len(observations) == 1
    assert observations[0].place_name == "Dublin"
    assert event is not None
    assert event.details["observation_count"] == 1


@pytest.mark.asyncio
async def test_pivot_run_uses_its_username_query_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Pivot query override",
            query=f"source-{uuid4()}@example.org",
            query_type="email",
            selected_tools=["blackbird"],
            tags=[],
            status="running",
        )
        run = ToolRun(
            tool_id="blackbird",
            display_name="Blackbird",
            status="queued",
            timeout_seconds=60,
            query_value="pivot_handle",
            query_type="username",
            pivot_depth=1,
        )
        investigation.runs.append(run)
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id
        run_id = run.id

    class DummyAdapter:
        def __init__(self) -> None:
            self.built: tuple[str, QueryType] | None = None

        async def healthcheck(self) -> HealthResult:
            return HealthResult(True, "ready", "test")

        def build_command(
            self,
            query: str,
            query_type: QueryType,
            output_dir: Path,
            timeout: int,
            proxy: str | None,
        ) -> list[str]:
            self.built = (query, query_type)
            return ["dummy", query]

        async def execute(
            self,
            command: list[str],
            output_dir: Path,
            timeout: int,
            query: str,
            log: object,
        ) -> ExecutionResult:
            return ExecutionResult(0, "", "")

        def parse_results(
            self, result: ExecutionResult, output_dir: Path
        ) -> list[dict[str, object]]:
            return []

        def normalise_results(
            self,
            records: list[dict[str, object]],
            query: str,
            query_type: QueryType,
            raw_reference: str,
        ) -> list[object]:
            return []

    adapter = DummyAdapter()
    manager = JobManager()

    async def fake_emit(investigation_id: str, event: str, data: dict[str, object]) -> None:
        return None

    monkeypatch.setattr(jobs, "get_adapter", lambda tool_id: adapter)
    monkeypatch.setattr(manager, "emit", fake_emit)

    await manager._run_tool(investigation_id, run_id)

    assert adapter.built == ("pivot_handle", QueryType.USERNAME)
    with SessionLocal() as db:
        stored = db.get(ToolRun, run_id)
        assert stored is not None
        assert stored.status == "completed"


@pytest.mark.asyncio
async def test_command_build_failure_finalises_run_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Collector setup failure",
            query=f"plain-{uuid4().hex[:12]}",
            query_type="username",
            selected_tools=["federated_identity"],
            tags=[],
            status="running",
        )
        run = ToolRun(
            tool_id="federated_identity",
            display_name="Federated Identity Resolver",
            status="queued",
            timeout_seconds=30,
        )
        investigation.runs.append(run)
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id
        run_id = run.id

    class RejectingAdapter:
        async def healthcheck(self) -> HealthResult:
            return HealthResult(True, "ready", "test")

        def build_command(
            self,
            query: str,
            query_type: QueryType,
            output_dir: Path,
            timeout: int,
            proxy: str | None,
        ) -> list[str]:
            raise ValueError("federated identity requires user@domain")

    emitted: list[dict[str, object]] = []
    manager = JobManager()

    async def fake_emit(investigation_id: str, event: str, data: dict[str, object]) -> None:
        if event == "tool":
            emitted.append(data)

    monkeypatch.setattr(jobs, "get_adapter", lambda tool_id: RejectingAdapter())
    monkeypatch.setattr(manager, "emit", fake_emit)

    await manager._run_tool(investigation_id, run_id)

    with SessionLocal() as db:
        stored = db.get(ToolRun, run_id)
        assert stored is not None
        assert stored.status == "failed"
        assert stored.finished_at is not None
        assert stored.error_code == "invalid_query"
        assert "requires user@domain" in str(stored.error_message)
    assert emitted[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_extended_username_search_adds_maigret_all_sites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Extended username",
            query="torvalds",
            query_type="username",
            selected_tools=["maigret"],
            tags=[],
            status="running",
            extended_username_search=True,
        )
        run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="queued",
            timeout_seconds=240,
        )
        investigation.runs.append(run)
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id
        run_id = run.id

    class ExtendedAdapter:
        def __init__(self) -> None:
            self.executed: list[str] | None = None

        async def healthcheck(self) -> HealthResult:
            return HealthResult(True, "ready", "test")

        def build_command(
            self,
            query: str,
            query_type: QueryType,
            output_dir: Path,
            timeout: int,
            proxy: str | None,
        ) -> list[str]:
            return ["maigret", query, "--no-autoupdate"]

        async def execute(
            self,
            command: list[str],
            output_dir: Path,
            timeout: int,
            query: str,
            log: object,
        ) -> ExecutionResult:
            self.executed = list(command)
            return ExecutionResult(0, "", "")

        def parse_results(
            self, result: ExecutionResult, output_dir: Path
        ) -> list[dict[str, object]]:
            return []

        def normalise_results(
            self,
            records: list[dict[str, object]],
            query: str,
            query_type: QueryType,
            raw_reference: str,
        ) -> list[object]:
            return []

    adapter = ExtendedAdapter()
    manager = JobManager()

    async def fake_emit(investigation_id: str, event: str, data: dict[str, object]) -> None:
        return None

    monkeypatch.setattr(jobs, "get_adapter", lambda tool_id: adapter)
    monkeypatch.setattr(manager, "emit", fake_emit)

    await manager._run_tool(investigation_id, run_id)

    assert adapter.executed is not None
    assert adapter.executed[-1] == "--all-sites"


@pytest.mark.asyncio
async def test_successful_rate_limited_run_is_not_recorded_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Bounded rate limit",
            query=f"limited-{uuid4()}",
            query_type="username",
            selected_tools=["maigret"],
            tags=[],
            status="running",
        )
        run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="queued",
            timeout_seconds=60,
        )
        investigation.runs.append(run)
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id
        run_id = run.id

    class LimitedAdapter:
        async def healthcheck(self) -> HealthResult:
            return HealthResult(True, "ready", "test")

        def build_command(
            self,
            query: str,
            query_type: QueryType,
            output_dir: Path,
            timeout: int,
            proxy: str | None,
        ) -> list[str]:
            return ["instaloader-osint", "--username", query]

        async def execute(
            self,
            command: list[str],
            output_dir: Path,
            timeout: int,
            query: str,
            log: object,
        ) -> ExecutionResult:
            return ExecutionResult(
                0,
                "",
                "",
                rate_limited=True,
                error_code="rate_limited",
                error_message="Instagram rate limited the profile lookup",
            )

        def parse_results(
            self, result: ExecutionResult, output_dir: Path
        ) -> list[dict[str, object]]:
            return []

        def normalise_results(
            self,
            records: list[dict[str, object]],
            query: str,
            query_type: QueryType,
            raw_reference: str,
        ) -> list[FindingRecord]:
            return []

    manager = JobManager()

    async def fake_emit(investigation_id: str, event: str, data: dict[str, object]) -> None:
        return None

    monkeypatch.setattr(jobs, "get_adapter", lambda tool_id: LimitedAdapter())
    monkeypatch.setattr(manager, "emit", fake_emit)

    await manager._run_tool(investigation_id, run_id)

    with SessionLocal() as db:
        stored = db.get(ToolRun, run_id)
        assert stored is not None
        assert stored.status == "completed"
        assert stored.rate_limited is True
        assert stored.error_message == "Instagram rate limited the profile lookup"


@pytest.mark.asyncio
async def test_partial_browser_cookies_remain_anonymous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Partial browser session",
            query=f"partial-{uuid4()}",
            query_type="username",
            selected_tools=["instaloader"],
            tags=[],
            status="running",
        )
        run = ToolRun(
            tool_id="instaloader",
            display_name="Instaloader",
            status="queued",
            timeout_seconds=60,
        )
        investigation.runs.append(run)
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id
        run_id = run.id

    class AnonymousAdapter:
        async def healthcheck(self) -> HealthResult:
            return HealthResult(True, "ready", "test")

        def set_session_context(
            self,
            cookies: list[dict[str, object]],
            *,
            user_agent: str,
            accept_language: str,
        ) -> None:
            raise AssertionError("partial browser cookies must not be supplied")

        def build_command(
            self,
            query: str,
            query_type: QueryType,
            output_dir: Path,
            timeout: int,
            proxy: str | None,
        ) -> list[str]:
            return ["instaloader-osint", "--username", query]

        async def execute(
            self,
            command: list[str],
            output_dir: Path,
            timeout: int,
            query: str,
            log: object,
        ) -> ExecutionResult:
            return ExecutionResult(0, "", "")

        def parse_results(
            self, result: ExecutionResult, output_dir: Path
        ) -> list[dict[str, object]]:
            return []

        def normalise_results(
            self,
            records: list[dict[str, object]],
            query: str,
            query_type: QueryType,
            raw_reference: str,
        ) -> list[FindingRecord]:
            return []

    class PartialSessionBrowser:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def export_session(self, collector_id: str) -> BrowserSessionBundle:
            assert collector_id == "instaloader"
            return BrowserSessionBundle(
                collector_id="instaloader",
                exported_at=datetime.now(UTC),
                scope_hosts=("instagram.com",),
                authenticated=False,
                user_agent="Mozilla/5.0 Synthetic Chrome/149.0",
                accept_language="en-IE,en;q=0.9",
                cookies=(
                    BrowserSessionCookie(
                        name="csrftoken",
                        value="synthetic-partial-cookie",
                        domain=".instagram.com",
                        path="/",
                        expires=1_900_000_000,
                        secure=True,
                        http_only=False,
                    ),
                ),
            )

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_dir=tmp_path,
        browser_enabled=True,
        browser_agent_token="token-" + "x" * 40,
        browser_share_sessions=True,
    )
    emitted: list[dict[str, object]] = []
    manager = JobManager()

    async def fake_emit(investigation_id: str, event: str, data: dict[str, object]) -> None:
        if event == "tool":
            emitted.append(data)

    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    monkeypatch.setattr(jobs, "get_adapter", lambda tool_id: AnonymousAdapter())
    monkeypatch.setattr(jobs, "BrowserAgentClient", PartialSessionBrowser)
    monkeypatch.setattr(manager, "emit", fake_emit)

    await manager._run_tool(investigation_id, run_id)

    running_event = next(item for item in emitted if item.get("status") == "running")
    assert running_event["session_mode"] == "anonymous"
    with SessionLocal() as db:
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "collector_session_unavailable",
            )
        )
    assert event is not None
    assert event.details["error_code"] == "browser_not_authenticated"
    assert event.details["cookie_count"] == 1
    assert "synthetic-partial-cookie" not in str(event.details)


@pytest.mark.asyncio
async def test_managed_browser_session_is_scoped_to_one_collector_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Managed session",
            query=f"session-{uuid4()}",
            query_type="username",
            selected_tools=["instaloader"],
            tags=[],
            status="running",
        )
        run = ToolRun(
            tool_id="instaloader",
            display_name="Instaloader",
            status="queued",
            timeout_seconds=60,
        )
        investigation.runs.append(run)
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id
        run_id = run.id

    class SessionAdapter:
        def __init__(self) -> None:
            self.cookies: list[dict[str, object]] = []
            self.user_agent = ""
            self.accept_language = ""

        async def healthcheck(self) -> HealthResult:
            return HealthResult(True, "ready", "test")

        def set_session_context(
            self,
            cookies: list[dict[str, object]],
            *,
            user_agent: str,
            accept_language: str,
        ) -> None:
            self.cookies = cookies
            self.user_agent = user_agent
            self.accept_language = accept_language

        def build_command(
            self,
            query: str,
            query_type: QueryType,
            output_dir: Path,
            timeout: int,
            proxy: str | None,
        ) -> list[str]:
            return ["instaloader-osint", "--username", query]

        async def execute(
            self,
            command: list[str],
            output_dir: Path,
            timeout: int,
            query: str,
            log: object,
        ) -> ExecutionResult:
            assert self.cookies[0]["domain"] == ".instagram.com"
            assert self.user_agent == "Mozilla/5.0 Synthetic Chrome/149.0"
            assert self.accept_language == "en-IE,en;q=0.9"
            return ExecutionResult(0, "", "")

        def parse_results(
            self, result: ExecutionResult, output_dir: Path
        ) -> list[dict[str, object]]:
            return []

        def normalise_results(
            self,
            records: list[dict[str, object]],
            query: str,
            query_type: QueryType,
            raw_reference: str,
        ) -> list[FindingRecord]:
            return []

    class SessionBrowser:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def export_session(self, collector_id: str) -> BrowserSessionBundle:
            assert collector_id == "instaloader"
            return BrowserSessionBundle(
                collector_id="instaloader",
                exported_at=datetime.now(UTC),
                scope_hosts=("instagram.com",),
                authenticated=True,
                user_agent="Mozilla/5.0 Synthetic Chrome/149.0",
                accept_language="en-IE,en;q=0.9",
                cookies=(
                    BrowserSessionCookie(
                        name="sessionid",
                        value="synthetic-session-secret",
                        domain=".instagram.com",
                        path="/",
                        expires=1_900_000_000,
                        secure=True,
                        http_only=True,
                    ),
                ),
            )

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_dir=tmp_path,
        browser_enabled=True,
        browser_agent_token="token-" + "x" * 40,
        browser_share_sessions=True,
    )
    adapter = SessionAdapter()
    emitted: list[dict[str, object]] = []
    manager = JobManager()

    async def fake_emit(investigation_id: str, event: str, data: dict[str, object]) -> None:
        if event == "tool":
            emitted.append(data)

    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    monkeypatch.setattr(jobs, "get_adapter", lambda tool_id: adapter)
    monkeypatch.setattr(jobs, "BrowserAgentClient", SessionBrowser)
    monkeypatch.setattr(manager, "emit", fake_emit)

    await manager._run_tool(investigation_id, run_id)

    running_event = next(item for item in emitted if item.get("status") == "running")
    assert running_event["session_mode"] == "managed_browser"
    assert running_event["timeout_seconds"] == 60
    assert isinstance(running_event["started_at"], str)
    with SessionLocal() as db:
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "collector_session_supplied",
            )
        )
    assert event is not None
    assert event.details["cookie_count"] == 1
    assert event.details["scope_hosts"] == ["instagram.com"]
    assert event.details["browser_identity_supplied"] is True
    assert "synthetic-session-secret" not in str(event.details)


@pytest.mark.asyncio
async def test_instaloader_without_session_is_not_executed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="No session",
            query=f"nosession-{uuid4()}",
            query_type="username",
            selected_tools=["instaloader"],
            tags=[],
            status="running",
        )
        run = ToolRun(
            tool_id="instaloader",
            display_name="Instaloader",
            status="queued",
            timeout_seconds=60,
        )
        investigation.runs.append(run)
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id
        run_id = run.id

    class NoSessionAdapter:
        executed = False

        async def healthcheck(self) -> HealthResult:
            return HealthResult(True, "ready", "test")

        def build_command(
            self,
            query: str,
            query_type: QueryType,
            output_dir: Path,
            timeout: int,
            proxy: str | None,
        ) -> list[str]:
            return ["instaloader-osint", "--username", query]

        async def execute(
            self,
            command: list[str],
            output_dir: Path,
            timeout: int,
            query: str,
            log: object,
        ) -> ExecutionResult:
            self.executed = True
            return ExecutionResult(0, "", "")

        def parse_results(
            self, result: ExecutionResult, output_dir: Path
        ) -> list[dict[str, object]]:
            return []

        def normalise_results(
            self,
            records: list[dict[str, object]],
            query: str,
            query_type: QueryType,
            raw_reference: str,
        ) -> list[FindingRecord]:
            return []

    settings = Settings(_env_file=None, data_dir=tmp_path)  # type: ignore[call-arg]
    adapter = NoSessionAdapter()
    emitted: list[dict[str, object]] = []
    manager = JobManager()

    async def fake_emit(investigation_id: str, event: str, data: dict[str, object]) -> None:
        if event == "tool":
            emitted.append(data)

    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    monkeypatch.setattr(jobs, "get_adapter", lambda tool_id: adapter)
    monkeypatch.setattr(manager, "emit", fake_emit)

    await manager._run_tool(investigation_id, run_id)

    assert adapter.executed is False
    with SessionLocal() as db:
        saved = db.get(ToolRun, run_id)
        assert saved is not None
        assert saved.status == "failed"
        assert saved.error_code == "session_required"
        assert "browser lookup does not require signing in" in (saved.error_message or "")
    assert emitted[-1]["status"] == "failed"
    assert emitted[-1]["findings"] == 0


@pytest.mark.asyncio
async def test_instaloader_without_session_attempts_public_browser_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Public browser fallback", query="lazeredwithlove",
            query_type="username", selected_tools=["instaloader"],
            tags=[], status="running",
        )
        run = ToolRun(
            tool_id="instaloader", display_name="Instaloader",
            status="queued", timeout_seconds=60,
        )
        investigation.runs.append(run)
        db.add(investigation)
        db.flush()
        investigation_id, run_id = investigation.id, run.id

    class NoSessionAdapter:
        async def healthcheck(self) -> HealthResult:
            return HealthResult(True, "ready", "test")

        def build_command(
            self, query: str, query_type: QueryType, output_dir: Path,
            timeout: int, proxy: str | None,
        ) -> list[str]:
            return ["instaloader-osint", "--username", query]

        async def execute(self, *args: object, **kwargs: object) -> ExecutionResult:
            raise AssertionError("Unauthenticated Instaloader API must not run")

        def parse_results(
            self, result: ExecutionResult, output_dir: Path,
        ) -> list[dict[str, object]]:
            return []

        def normalise_results(
            self, records: list[dict[str, object]], query: str,
            query_type: QueryType, raw_reference: str,
        ) -> list[FindingRecord]:
            return []

    settings = Settings(_env_file=None, data_dir=tmp_path, browser_enabled=True,
                        browser_share_sessions=False)  # type: ignore[call-arg]
    manager = JobManager()
    fallbacks: list[tuple[str, str, str]] = []

    async def fake_fallback(
        case_id: str, *, run_id: str, username: str,
    ) -> None:
        fallbacks.append((case_id, run_id, username))

    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    monkeypatch.setattr(jobs, "get_adapter", lambda tool_id: NoSessionAdapter())
    monkeypatch.setattr(manager, "_capture_instagram_browser_fallback", fake_fallback)
    await manager._run_tool(investigation_id, run_id)
    assert fallbacks == [(investigation_id, run_id, "lazeredwithlove")]


@pytest.mark.asyncio
async def test_manual_profile_validation_records_and_emits_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Validation failure",
            query=f"validation-{uuid4()}",
            query_type="username",
            selected_tools=["sherlock"],
            tags=[],
            status="completed",
        )
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id

    manager = JobManager()
    emitted: list[tuple[str, dict[str, object]]] = []

    async def fail_validation(investigation_id: str) -> dict[str, object]:
        raise RuntimeError("synthetic validation failure")

    async def fake_emit(investigation_id: str, event: str, data: dict[str, object]) -> None:
        emitted.append((event, data))

    monkeypatch.setattr(manager, "_run_profile_verification", fail_validation)
    monkeypatch.setattr(manager, "emit", fake_emit)

    await manager._validate_profiles(investigation_id)

    assert [data["status"] for event, data in emitted if event == "phase"] == [
        "running",
        "failed",
    ]
    with SessionLocal() as db:
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "profile_validation_failed",
            )
        )
    assert event is not None
    assert event.details == {"error_type": "RuntimeError"}


def test_live_process_log_survives_interruption_marker(tmp_path: Path) -> None:
    output_dir = tmp_path / "raw" / "case-one" / "run-one"
    path = JobManager._initialise_live_log(output_dir, "instaloader")

    JobManager._append_live_log(path, "stdout", "Loaded browser session")
    JobManager._append_live_log(path, "stderr", "Waiting for Instagram response")

    content = path.read_text(encoding="utf-8")
    assert "LIVE PROCESS OUTPUT" in content
    assert "[STDOUT] Loaded browser session" in content
    assert "[STDERR] Waiting for Instagram response" in content


@pytest.mark.asyncio
async def test_recover_stale_marks_runs_failed_and_preserves_partial_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Base.metadata.create_all(engine)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'unused.db'}",
        browser_enabled=False,
    )
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Interrupted collector",
            query=f"interrupted-{uuid4()}",
            query_type="username",
            selected_tools=["instaloader"],
            tags=[],
            status="running",
        )
        run = ToolRun(
            tool_id="instaloader",
            display_name="Instaloader",
            status="running",
            timeout_seconds=180,
        )
        investigation.runs.append(run)
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id
        run_id = run.id

    log_path = JobManager._initialise_live_log(
        tmp_path / "raw" / investigation_id / run_id,
        "instaloader",
    )
    JobManager._append_live_log(log_path, "stdout", "Profile request started")
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)

    await JobManager().recover_stale()

    with SessionLocal() as db:
        run = db.get(ToolRun, run_id)
        investigation = db.get(Investigation, investigation_id)
    assert run is not None
    assert investigation is not None
    assert run.status == "failed"
    assert run.error_code == "interrupted"
    assert run.error_message == "application restarted during execution"
    assert investigation.status == "failed"
    content = log_path.read_text(encoding="utf-8")
    assert "Profile request started" in content
    assert "last process output received before interruption" in content


@pytest.mark.asyncio
async def test_follow_ups_are_looked_for_again_after_profile_verification(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Follow-ups after verification", query=f"verify-{uuid4()}", query_type="username",
            selected_tools=["maigret"],
        )
        investigation.runs.append(
            ToolRun(tool_id="maigret", display_name="Maigret", status="queued", timeout_seconds=60)
        )
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id

    manager = JobManager()
    order: list[str] = []
    pivot_batches = [[], ["found-by-verification"]]

    async def fake_run_tool(investigation_id: str, run_id: str) -> None:
        order.append(f"run:{run_id if run_id == 'found-by-verification' else 'collector'}")

    async def fake_verification(investigation_id: str, *, limit: int | None = None) -> dict[str, object]:
        order.append("verification")
        return {"targets_checked": 0, "states": {}}

    def fake_queue(investigation_id: str) -> list[str]:
        order.append("look-for-follow-ups")
        return pivot_batches.pop(0)

    async def fake_emit(investigation_id: str, event: str, data: dict[str, object]) -> None:
        return None

    async def fake_wayback(investigation_id: str) -> dict[str, int]:
        order.append("history")
        return {"targets_checked": 0}

    monkeypatch.setattr(manager, "_run_tool", fake_run_tool)
    monkeypatch.setattr(manager, "_run_profile_verification", fake_verification)
    monkeypatch.setattr(manager, "_queue_automatic_pivots", fake_queue)
    monkeypatch.setattr(manager, "_run_automatic_wayback", fake_wayback)
    monkeypatch.setattr(manager, "emit", fake_emit)

    await manager._run(investigation_id)

    assert order == [
        "run:collector", "look-for-follow-ups", "verification", "look-for-follow-ups",
        "run:found-by-verification", "history",
    ]


def _blackbird_record(**metadata: object) -> FindingRecord:
    return FindingRecord(
        query_type=QueryType.USERNAME,
        queried_value="example",
        finding_type="profile",
        platform="Example",
        value="example",
        canonical_url="https://example.test/example",
        status=FindingStatus.FOUND,
        metadata=dict(metadata),
        profile=ProfileEvidence(),
        source_tool="blackbird",
        source_record_id="0",
    )


def test_sparse_retry_keeps_fields_an_earlier_run_found_and_marks_them() -> None:
    first = jobs._merge_source_metadata(
        {},
        _blackbird_record(
            status="found", reported_email="a@example.test", created_at="2020-01-02",
            followers=10, blackbird_unverified_claim=True,
            platform_details=[{"label": "Account created", "value": "2020-01-02"}],
        ),
    )
    second = jobs._merge_source_metadata(
        first,
        _blackbird_record(
            status="unknown", followers=12,
            platform_details=[{"label": "Followers", "value": "12"}],
        ),
    )

    source = second["source_metadata"]["blackbird"]
    assert source["reported_email"] == "a@example.test" and source["created_at"] == "2020-01-02"
    assert source["followers"] == 12 and source["status"] == "unknown"
    assert source["retained_from_earlier_run"] == ["created_at", "reported_email"]
    # A run's own verdict flags never carry forward.
    assert "blackbird_unverified_claim" not in source
    assert {item["label"] for item in source["platform_details"]} == {"Account created", "Followers"}

    note = {item["label"]: item["value"] for item in second["platform_details"]}
    assert note["Kept from an earlier run"] == "blackbird: created at, reported email"

    third = jobs._merge_source_metadata(
        second, _blackbird_record(status="found", reported_email="b@example.test")
    )
    source = third["source_metadata"]["blackbird"]
    assert source["reported_email"] == "b@example.test"
    assert source["retained_from_earlier_run"] == ["created_at", "followers"]

    fresh = jobs._merge_source_metadata(
        third,
        _blackbird_record(status="found", reported_email="b@example.test", created_at="2020-01-02",
                          followers=13),
    )
    assert "retained_from_earlier_run" not in fresh["source_metadata"]["blackbird"]
    assert all(item["label"] != "Kept from an earlier run" for item in fresh["platform_details"])


def test_another_collector_is_not_affected_by_carry_forward() -> None:
    first = jobs._merge_source_metadata({}, _blackbird_record(status="found", created_at="2020"))
    other = _blackbird_record(status="found")
    other.source_tool = "maigret"
    merged = jobs._merge_source_metadata(first, other)

    assert "created_at" not in merged["source_metadata"]["maigret"]
    assert "retained_from_earlier_run" not in merged["source_metadata"]["blackbird"]
