from datetime import UTC, datetime

from app.models.db import BrowserRun, Finding, Investigation, LocationObservation, ToolRun
from app.services.investigation_accounts import build_investigation_account_view


def finding(
    finding_id: str,
    *,
    platform: str,
    status: str,
    source_tool: str,
    source_tools: list[str] | None = None,
    profile: dict[str, object] | None = None,
    details: list[dict[str, str]] | None = None,
    timeline: list[dict[str, object]] | None = None,
) -> Finding:
    return Finding(
        id=finding_id,
        tool_run_id=f"run-{finding_id}",
        query_type="username",
        queried_value="example",
        finding_type="profile",
        platform=platform,
        value="example",
        canonical_url=f"https://{platform.casefold()}.example/example",
        status=status,
        confidence=0.9 if status == "found" else 0.1,
        extra={
            "profile": profile or {},
            "platform_details": details or [],
            "timeline_events": timeline or [],
        },
        source_tool=source_tool,
        source_tools=source_tools or [source_tool],
        source_record_id=finding_id,
        observed_at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
    )


def test_unified_account_view_prioritises_rich_and_collapses_technical_leads() -> None:
    investigation = Investigation(
        title="Example",
        query="example",
        query_type="username",
        selected_tools=["maigret", "sherlock", "nexfil"],
        tags=[],
        status="completed",
    )
    investigation.findings.extend(
        [
            finding(
                "github",
                platform="GitHub",
                status="found",
                source_tool="maigret",
                source_tools=["maigret", "sherlock"],
                profile={
                    "account_id": "example",
                    "display_name": "Example Person",
                    "location": "Dublin, Ireland",
                    "avatar_url": "https://images.example/avatar.jpg",
                    "followers": 12,
                },
                details=[
                    {"label": "Account ID", "value": "123"},
                    {"label": "Public email", "value": "e***@example.test"},
                ],
                timeline=[
                    {
                        "type": "registered",
                        "label": "Account registered",
                        "timestamp": "2010-01-02T03:04:05+00:00",
                        "source_field": "created_at",
                        "raw_value": "2010-01-02T03:04:05Z",
                        "semantic": "account_creation",
                        "confidence": "high",
                        "extractor": "GitHub API",
                    }
                ],
            ),
            finding(
                "nexfil",
                platform="Candidate Site",
                status="unknown",
                source_tool="nexfil",
                profile={"account_id": "example"},
            ),
        ]
    )

    view = build_investigation_account_view(investigation)

    assert view["enabled"] is True
    assert view["summary"]["account_count"] == 2
    assert view["summary"]["priority_account_count"] == 1
    assert view["summary"]["technical_account_count"] == 1
    assert view["groups"]["priority"][0]["platform"] == "GitHub"
    assert view["groups"]["priority"][0]["sources"] == ["maigret", "sherlock"]
    assert view["groups"]["technical"][0]["platform"] == "Candidate Site"
    assert view["timeline"][0]["label"] == "Account registered"
    assert view["timeline"][0]["extractor"] == "GitHub API"
    assert view["entities"]["names"][0]["value"] == "Example Person"
    assert view["entities"]["ids"][0]["value"] == "123"
    assert view["entities"]["emails"][0]["value"] == "e***@example.test"


def test_unified_account_view_preserves_source_specific_timeline_provenance() -> None:
    investigation = Investigation(
        title="Example",
        query="example",
        query_type="username",
        selected_tools=["maigret", "sherlock"],
        tags=[],
        status="completed",
    )
    item = finding(
        "merged",
        platform="GitHub",
        status="found",
        source_tool="maigret",
        source_tools=["maigret", "sherlock"],
        profile={"account_id": "example", "display_name": "Example Person"},
    )
    item.extra["source_metadata"] = {
        "maigret": {
            "timeline_events": [
                {
                    "type": "registered",
                    "label": "Account registered",
                    "timestamp": "2010-01-02T03:04:05+00:00",
                    "source_field": "created_at",
                    "raw_value": "2010-01-02T03:04:05Z",
                    "extractor": "GitHub API",
                }
            ]
        },
        "sherlock": {"sherlock_unverified_claim": True},
    }
    investigation.findings.append(item)

    view = build_investigation_account_view(investigation)

    assert view["accounts"][0]["sources"] == ["maigret", "sherlock"]
    assert view["timeline"][0]["sources"] == ["maigret"]
    assert view["timeline"][0]["extractor"] == "GitHub API"


def test_case_timeline_includes_tool_browser_location_and_archive_observations() -> None:
    investigation = Investigation(
        title="Timeline",
        query="example",
        query_type="username",
        selected_tools=["maigret"],
        tags=[],
        status="completed",
    )
    investigation.runs.append(
        ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
            query_value="example",
            query_type="username",
            started_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
            finished_at=datetime(2026, 8, 1, 10, 1, tzinfo=UTC),
        )
    )
    investigation.browser_runs.append(
        BrowserRun(
            collector_id="generic_capture",
            session_name="sock-default",
            requested_url="https://example.org/profile",
            final_url="https://example.org/profile",
            status="completed",
            page_title="Example profile",
            observed_at=datetime(2026, 8, 1, 10, 2, tzinfo=UTC),
        )
    )
    investigation.location_observations.append(
        LocationObservation(
            place_name="Dublin",
            evidence_type="declared",
            source_platform="Example",
            account_identifier="example",
            confidence=0.35,
            privacy_masked=False,
            raw_evidence={},
            occurred_at=datetime(2026, 7, 31, 9, 0, tzinfo=UTC),
        )
    )
    item = finding(
        "archive",
        platform="GitHub",
        status="found",
        source_tool="maigret",
        profile={"account_id": "example"},
    )
    item.extra["wayback"] = {
        "first_capture": {"captured_at": "2020-01-02T03:04:05+00:00"},
        "latest_capture": {"captured_at": "2025-06-07T08:09:10+00:00"},
    }
    investigation.findings.append(item)

    view = build_investigation_account_view(investigation)
    event_types = {event.get("event_type") for event in view["timeline"]}
    assert {"tool_run", "browser_capture", "location_observation", "archive_observation"} <= event_types
    assert view["summary"]["timeline_event_count"] == len(view["timeline"])
    assert view["summary"]["account_timeline_event_count"] == 0
    assert view["timeline"] == sorted(view["timeline"], key=lambda event: event["timestamp"])


def test_case_timeline_includes_telegram_export_message_events() -> None:
    investigation = Investigation(
        title="Telegram timeline",
        query="example_user",
        query_type="username",
        selected_tools=[],
        tags=[],
        status="completed",
    )
    item = finding(
        "telegram-message",
        platform="Telegram",
        status="found",
        source_tool="telegram_export_import",
        profile={},
    )
    item.finding_type = "telegram_message"
    item.extra = {
        "telegram_sender_name": "Alice",
        "telegram_sender_id": "user123",
        "timeline_events": [
            {
                "label": "Telegram message",
                "timestamp": "2026-08-17T09:00:00+00:00",
                "source_field": "date",
            }
        ],
    }
    investigation.findings.append(item)

    view = build_investigation_account_view(investigation)

    telegram = next(event for event in view["timeline"] if event["event_type"] == "telegram_message")
    assert telegram["platform"] == "Telegram"
    assert telegram["display_name"] == "Alice"
    assert telegram["username"] == "user123"


def test_unified_account_view_retains_but_excludes_ambiguous_dates() -> None:
    investigation = Investigation(
        title="Example",
        query="example",
        query_type="username",
        selected_tools=["maigret"],
        tags=[],
        status="completed",
    )
    investigation.findings.append(
        finding(
            "medium",
            platform="Medium",
            status="found",
            source_tool="maigret",
            profile={"account_id": "example"},
            timeline=[
                {
                    "type": "feed_generated",
                    "label": "RSS feed generated",
                    "timestamp": "2026-08-05T11:02:46+00:00",
                    "source_field": "latest_activity_at",
                    "raw_value": "2026-08-05 11:02:46 UTC",
                    "include_in_timeline": False,
                    "note": "Feed generation time is not user activity.",
                    "extractor": "Medium RSS",
                }
            ],
        )
    )

    view = build_investigation_account_view(investigation)

    assert view["timeline"] == []
    assert len(view["excluded_timeline_observations"]) == 1
    assert view["excluded_timeline_observations"][0]["label"] == "RSS feed generated"


def _case_with(*findings: Finding, query_type: str = "username") -> Investigation:
    investigation = Investigation(
        title="Example", query="example", query_type=query_type,
        selected_tools=["maigret"], tags=[], status="completed",
    )
    investigation.findings.extend(findings)
    return investigation


def _two_source_finding(finding_id: str = "github", account_id: str = "12345") -> Finding:
    item = finding(
        finding_id,
        platform="GitHub",
        status="found",
        source_tool="username_public_profiles",
        source_tools=["username_public_profiles", "maigret"],
        profile={"account_id": account_id, "display_name": "Example Person"},
    )
    item.extra = {
        **item.extra,
        "source_metadata": {
            "username_public_profiles": {"status": "found", "id": account_id},
            "maigret": {"status": "found", "username": "example"},
        },
    }
    return item


def _github_accounts(investigation: Investigation) -> list[dict]:
    return [
        account
        for account in build_investigation_account_view(investigation)["accounts"]
        if account["platform"] == "GitHub"
    ]


def test_one_stored_finding_with_an_id_source_and_a_handle_source_is_one_card() -> None:
    accounts = _github_accounts(_case_with(_two_source_finding()))

    assert len(accounts) == 1
    assert set(accounts[0]["sources"]) == {
        "username_public_profiles", "maigret"}
    assert accounts[0]["profile"]["account_id"] == "12345"
    assert accounts[0]["value"] == "example"


def test_a_handle_only_finding_joins_the_same_username_account() -> None:
    handle_only = finding("github-2", platform="GitHub", status="unknown", source_tool="sherlock")
    for ordering in ([_two_source_finding(), handle_only], [handle_only, _two_source_finding()]):
        accounts = _github_accounts(_case_with(*ordering))
        assert len(accounts) == 1
        assert set(accounts[0]["sources"]) >= {"maigret", "sherlock"}


def test_different_account_ids_on_one_platform_stay_separate() -> None:
    first = _two_source_finding("github-a", "12345")
    second = finding(
        "github-b", platform="GitHub", status="found", source_tool="username_public_profiles",
        profile={"account_id": "67890", "display_name": "Someone Else"},
    )

    assert len(_github_accounts(_case_with(first, second))) == 2


def test_a_name_search_does_not_join_other_findings_through_the_search_term() -> None:
    first = _two_source_finding("github-a", "12345")
    other = finding("github-b", platform="GitHub", status="unknown", source_tool="sherlock")
    for item in (first, other):
        item.query_type = "name"
        item.finding_type = "profile"

    accounts = _github_accounts(_case_with(first, other, query_type="name"))

    assert len(accounts) == 2
    assert sorted(len(account["sources"]) for account in accounts) == [1, 2]
