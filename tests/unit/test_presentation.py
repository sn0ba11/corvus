from datetime import UTC, datetime

from app.models.db import Finding, Investigation, LocationObservation, ToolRun
from app.services.presentation import dashboard_summary, investigation_summary


def test_investigation_summary_exposes_profile_validation_coverage() -> None:
    investigation = Investigation(
        title="Validation coverage",
        query="example",
        query_type="username",
        selected_tools=["sherlock"],
        tags=[],
        status="completed",
    )
    states = [
        ("confirmed_account", "found", "profile"),
        ("unverified_candidate", "likely", "profile"),
        ("dead_profile", "not_found", "dead_profile"),
        ("retrieval_error", "unknown", "unverified_profile_lead"),
        (None, "unknown", "unverified_profile_lead"),
    ]
    for index, (state, status, finding_type) in enumerate(states):
        extra = {"verification": {"state": state}} if state else {}
        investigation.findings.append(
            Finding(
                query_type="username",
                queried_value="example",
                finding_type=finding_type,
                platform=f"Platform {index}",
                value="example",
                canonical_url=f"https://social{index}.example/example",
                status=status,
                extra=extra,
                source_tool="sherlock",
                source_tools=["sherlock"],
                observed_at=datetime.now(UTC),
            )
        )

    summary = investigation_summary(investigation)

    assert summary["profile_claim_count"] == 5
    assert summary["verification_attempt_count"] == 4
    assert summary["unvalidated_profile_count"] == 1
    assert summary["verified_account_count"] == 1
    assert summary["likely_account_count"] == 1
    assert summary["dead_profile_count"] == 1
    assert summary["unresolved_verification_count"] == 1


def test_positive_findings_exclude_unresolved_verification_states() -> None:
    investigation = Investigation(
        title="Unresolved verification",
        query="example",
        query_type="username",
        selected_tools=["maigret"],
        tags=[],
        status="completed",
    )
    # A collector's raw "found" claim that Corvus's own verification pass could
    # not independently confirm (e.g. the site errored, was blocked or rate
    # limited) must not inflate the positive-findings count or platform summary.
    unresolved = Finding(
        query_type="username",
        queried_value="example",
        finding_type="profile",
        platform="ReverbNation",
        value="example",
        status="found",
        extra={"verification": {"state": "retrieval_error"}},
        source_tool="maigret",
        source_tools=["maigret"],
        observed_at=datetime.now(UTC),
    )
    confirmed = Finding(
        query_type="username",
        queried_value="example",
        finding_type="profile",
        platform="GitHub",
        value="example",
        status="found",
        extra={"verification": {"state": "confirmed_account"}},
        source_tool="maigret",
        source_tools=["maigret"],
        observed_at=datetime.now(UTC),
    )
    unverified = Finding(
        query_type="username",
        queried_value="example",
        finding_type="profile",
        platform="Steam",
        value="example",
        status="found",
        extra={},
        source_tool="steam_public",
        source_tools=["steam_public"],
        observed_at=datetime.now(UTC),
    )
    investigation.findings.extend([unresolved, confirmed, unverified])

    summary = investigation_summary(investigation)

    assert summary["findings_total"] == 3
    assert summary["positive_findings"] == 2
    platform_positive = {row["platform"]: row["positive_count"] for row in summary["platform_summary"]}
    assert platform_positive["ReverbNation"] == 0
    assert platform_positive["GitHub"] == 1
    assert platform_positive["Steam"] == 1


def test_investigation_summary_counts_evidence_execution_and_decisions() -> None:
    investigation = Investigation(
        title="Presentation test",
        query="example",
        query_type="username",
        selected_tools=["maigret", "sherlock"],
        tags=[],
        status="completed",
    )
    completed = ToolRun(
        tool_id="maigret",
        display_name="Maigret",
        status="completed",
        timeout_seconds=60,
    )
    failed = ToolRun(
        tool_id="sherlock",
        display_name="Sherlock",
        status="failed",
        timeout_seconds=60,
        rate_limited=True,
    )
    positive = Finding(
        query_type="username",
        queried_value="example",
        finding_type="profile",
        platform="GitHub",
        value="example",
        status="found",
        extra={
            "profile": {"display_name": "Example Person"},
            "image_cache": {"sha256": "a" * 64},
            "account_correlation": {
                "matches": [
                    {"peer_id": "one", "decision": "accepted"},
                    {"peer_id": "two", "decision": "unreviewed"},
                ]
            },
            "image_correlation": {"matches": [{"peer_id": "three", "decision": "accepted"}]},
        },
        source_tool="maigret",
        source_tools=["maigret"],
        observed_at=datetime.now(UTC),
    )
    unknown = Finding(
        query_type="username",
        queried_value="example",
        finding_type="profile",
        platform="Reddit",
        value="example",
        status="unknown",
        extra={},
        source_tool="sherlock",
        source_tools=["sherlock"],
        observed_at=datetime.now(UTC),
    )
    location = LocationObservation(
        place_name="Dublin",
        locality="Dublin",
        country="Ireland",
        evidence_type="declared",
        first_observed_at=datetime.now(UTC),
        last_observed_at=datetime.now(UTC),
        source_platform="GitHub",
        confidence=0.35,
        privacy_masked=False,
        raw_evidence={},
    )
    investigation.runs.extend([completed, failed])
    investigation.findings.extend([positive, unknown])
    investigation.location_observations.append(location)
    completed.findings.append(positive)
    failed.findings.append(unknown)

    summary = investigation_summary(investigation)

    assert summary["findings_total"] == 2
    assert summary["positive_findings"] == 1
    assert summary["platform_count"] == 2
    assert summary["rich_profile_count"] == 1
    assert summary["cached_image_count"] == 1
    assert summary["location_observation_count"] == 1
    assert summary["failed_run_count"] == 1
    assert summary["rate_limited_run_count"] == 1
    assert summary["account_match_count"] == 2
    assert summary["accepted_account_match_count"] == 1
    assert summary["image_match_count"] == 1
    assert summary["accepted_image_match_count"] == 1
    assert summary["platform_summary"][0]["positive_count"] == 1

    dashboard = dashboard_summary([investigation])
    assert dashboard == {
        "investigation_count": 1,
        "active_investigations": 0,
        "completed_investigations": 1,
        "positive_findings": 1,
        "location_observations": 1,
        "failed_runs": 1,
    }
