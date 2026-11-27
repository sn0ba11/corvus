from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.db import AuditEvent, Base, Finding, Investigation, ToolRun
from app.services.evidence_quality import reconcile_legacy_weak_claims, reconcile_platform_metadata


def _finding(tool: str, *, status: str = "found", extra: dict | None = None) -> Finding:
    return Finding(
        query_type="username",
        queried_value="example",
        finding_type="profile",
        platform=f"{tool}-platform",
        value="example",
        canonical_url=f"https://{tool}.example/example",
        display_url=f"https://{tool}.example/example",
        status=status,
        extra=extra or {},
        source_tool=tool,
        source_tools=[tool],
    )


def test_reconciles_legacy_weak_claims_without_downgrading_concrete_or_corroborated() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        investigation = Investigation(
            title="Evidence quality",
            query="example",
            query_type="username",
            selected_tools=["sherlock", "blackbird", "maigret"],
            tags=[],
            status="completed",
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
            status="completed",
            timeout_seconds=60,
        )
        maigret_run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
        sherlock = _finding("sherlock")
        blackbird_bare = _finding("blackbird", extra={"metadata": None})
        blackbird_concrete = _finding(
            "blackbird",
            extra={
                "metadata": [
                    {
                        "schema": "HTML",
                        "type": "Text",
                        "name": "Full Name",
                        "value": "Example Person",
                    }
                ]
            },
        )
        blackbird_verified = _finding(
            "blackbird",
            extra={
                "blackbird_public_verification": {
                    "source": "https://users.roblox.com/v1/usernames/users",
                    "result": "exact_username_match",
                },
                "platform_id": 1761042963,
            },
        )
        corroborated = _finding("maigret")
        corroborated.source_tools = ["maigret", "blackbird"]
        investigation.runs.extend([sherlock_run, blackbird_run, maigret_run])
        investigation.findings.extend(
            [sherlock, blackbird_bare, blackbird_concrete, blackbird_verified, corroborated]
        )
        sherlock_run.findings.append(sherlock)
        blackbird_run.findings.extend([blackbird_bare, blackbird_concrete, blackbird_verified])
        maigret_run.findings.append(corroborated)
        db.add(investigation)
        db.flush()

        totals = reconcile_legacy_weak_claims(db)
        db.flush()

        assert totals == {"sherlock": 1, "blackbird": 1}
        assert sherlock.status == "unknown"
        assert sherlock.confidence == 0.15
        assert sherlock.finding_type == "unverified_profile_lead"
        assert blackbird_bare.status == "unknown"
        assert blackbird_bare.confidence == 0.10
        assert blackbird_bare.extra["blackbird_evidence_strength"] == "bare_url_assertion"
        assert blackbird_concrete.status == "found"
        assert blackbird_verified.status == "found"
        assert corroborated.status == "found"
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation.id,
                AuditEvent.event_type == "evidence_quality_reconciled",
            )
        )
        assert event is not None
        assert event.details["sherlock_downgraded"] == 1
        assert event.details["blackbird_downgraded"] == 1


def test_reconciles_platform_details_from_legacy_source_metadata_idempotently() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        investigation = Investigation(
            title="Legacy profile metadata",
            query="example",
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
        finding = _finding(
            "maigret",
            extra={
                "profile": {"location": "Dublin", "followers": 34},
                "source_metadata": {
                    "maigret": {
                        "created_at": "2011-08-09 21:52:00+00:00",
                        "favourites_count": "241",
                        "is_protected": "True",
                    }
                },
            },
        )
        investigation.runs.append(run)
        investigation.findings.append(finding)
        run.findings.append(finding)
        db.add(investigation)
        db.flush()

        assert reconcile_platform_metadata(db) == 1
        db.flush()
        details = {item["label"]: item["value"] for item in finding.extra["platform_details"]}
        assert details == {
            "Account created": "2011-08-09 21:52 UTC",
            "Likes / favourites": "241",
            "Protected account": "Yes",
        }
        assert reconcile_platform_metadata(db) == 0
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.investigation_id == investigation.id,
                AuditEvent.event_type == "platform_metadata_reconciled",
            )
        )
        assert event is not None
        assert event.details["finding_count"] == 1
