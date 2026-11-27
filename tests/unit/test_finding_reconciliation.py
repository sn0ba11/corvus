from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from app.db.session import SessionLocal, engine
from app.models.db import AuditEvent, Base, Finding, Investigation, LocationObservation, ToolRun
from app.services.finding_reconciliation import reconcile_profile_duplicates


def test_reconcile_profile_duplicates_merges_duolingo_api_alias_and_references() -> None:
    Base.metadata.create_all(engine)
    query = f"duo-{uuid4()}"
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Legacy Duolingo duplicate",
            query=query,
            query_type="username",
            selected_tools=["maigret", "blackbird"],
            tags=[],
            status="completed",
        )
        maigret_run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
        blackbird_run = ToolRun(
            tool_id="blackbird",
            display_name="Blackbird",
            status="completed",
            timeout_seconds=60,
        )
        pivot_run = ToolRun(
            tool_id="sherlock",
            display_name="Sherlock",
            status="completed",
            timeout_seconds=60,
        )
        public = Finding(
            query_type="username",
            queried_value=query,
            finding_type="profile",
            platform="Duolingo",
            value=query,
            canonical_url=f"https://www.duolingo.com/profile/{query}",
            display_url=f"https://www.duolingo.com/profile/{query}",
            status="found",
            confidence=0.95,
            extra={
                "profile": {
                    "account_id": query,
                    "display_name": query,
                    "avatar_url": "https://cdn.example/avatar.png",
                },
                "verification": {
                    "state": "identifier_mismatch",
                    "matched_identifier": False,
                },
                "source_metadata": {"maigret": {"status": "Claimed", "streak": 1393}},
                "provenance": [{"tool": "maigret", "record_id": "168911392"}],
            },
            source_tool="maigret",
            source_tools=["maigret"],
            source_record_id="168911392",
            observed_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        api = Finding(
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
                "source_metadata": {"blackbird": {"status": "FOUND"}},
                "provenance": [{"tool": "blackbird", "record_id": "2"}],
            },
            source_tool="blackbird",
            source_tools=["blackbird"],
            source_record_id="2",
            observed_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        investigation.runs.extend([maigret_run, blackbird_run, pivot_run])
        investigation.findings.extend([public, api])
        maigret_run.findings.append(public)
        blackbird_run.findings.append(api)
        db.add(investigation)
        db.flush()
        pivot_run.pivot_source_finding_id = api.id
        location = LocationObservation(
            investigation_id=investigation.id,
            finding_id=api.id,
            place_name="Dublin",
            evidence_type="profile_declared",
            source_platform="Duolingo",
            account_identifier=query,
            privacy_masked=False,
            confidence=0.7,
            raw_evidence={},
        )
        db.add(location)
        db.flush()
        investigation_id = investigation.id
        public_id = public.id
        api_id = api.id
        pivot_id = pivot_run.id
        location_id = location.id

    assert reconcile_profile_duplicates() == 1
    assert reconcile_profile_duplicates() == 0

    with SessionLocal() as db:
        findings = list(
            db.scalars(select(Finding).where(Finding.investigation_id == investigation_id))
        )
        assert len(findings) == 1
        stored = findings[0]
        assert stored.id == public_id
        assert stored.canonical_url == f"https://www.duolingo.com/profile/{query}"
        assert stored.source_tools == ["maigret", "blackbird"]
        assert set(stored.extra["source_metadata"]) == {"maigret", "blackbird"}
        assert [item["tool"] for item in stored.extra["provenance"]] == [
            "maigret",
            "blackbird",
        ]
        assert stored.extra["verification"]["state"] == "corroborated_account"
        assert stored.extra["verification"]["matched_identifier"] is True
        assert db.get(Finding, api_id) is None
        assert db.get(LocationObservation, location_id).finding_id == public_id
        assert db.get(ToolRun, pivot_id).pivot_source_finding_id == public_id
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation_id,
                AuditEvent.event_type == "duplicate_profiles_reconciled",
            )
        )
        assert event is not None
        assert event.details["removed_finding_ids"] == [api_id]
