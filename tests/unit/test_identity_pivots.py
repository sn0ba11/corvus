from datetime import UTC, datetime

from app.models.db import Finding, Investigation, ToolRun
from app.services.identity_pivots import discover_display_name_pivots, normalise_display_name


def investigation() -> Investigation:
    item = Investigation(
        title="Identity pivot test",
        query="corvus_fixture_nq7az",
        query_type="username",
        selected_tools=["maigret"],
        tags=[],
        status="completed",
    )
    item.runs.append(
        ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
    )
    return item


def finding(
    finding_id: str,
    display_name: str,
    *,
    platform: str = "Instagram",
    source_tool: str = "maigret",
) -> Finding:
    return Finding(
        id=finding_id,
        query_type="username",
        queried_value="corvus_fixture_nq7az",
        finding_type="profile",
        platform=platform,
        value="corvus_fixture_nq7az",
        status="found",
        extra={"profile": {"display_name": display_name}},
        source_tool=source_tool,
        source_tools=[source_tool],
        observed_at=datetime(2026, 7, 20, tzinfo=UTC),
    )


def test_display_name_normalisation_rejects_platform_labels_and_handles() -> None:
    assert normalise_display_name("  Nova   Example ") == "Nova Example"
    assert normalise_display_name("Instagram") is None
    assert normalise_display_name("corvus_fixture_nq7az") is None
    assert normalise_display_name("person@example.org") is None


def test_positive_profile_full_names_become_deduplicated_pivots() -> None:
    item = investigation()
    first = finding("one", "Nova Example")
    second = finding("two", " nova  example ", platform="TikTok", source_tool="blackbird")
    item.findings.extend([first, second])
    item.runs[0].findings.extend([first, second])

    pivots = discover_display_name_pivots(item)

    assert len(pivots) == 1
    assert pivots[0].value == "Nova Example"
    assert pivots[0].finding_ids == ("one", "two")
    assert pivots[0].platforms == ("Instagram", "TikTok")
    assert pivots[0].source_tools == ("blackbird", "maigret")
