from app.models.db import Finding, Investigation
from app.services.toolkit_recommendations import (
    find_toolkit_recommendation,
    toolkit_recommendations,
)


def name_case() -> Investigation:
    return Investigation(
        id="case-toolkit",
        title="Company lead",
        query="Example Holdings Limited",
        query_type="name",
        selected_tools=["bing_name"],
        tags=[],
        status="completed",
    )


def test_name_case_recommends_official_registers_and_edgar_with_provenance() -> None:
    investigation = name_case()
    recommendations = toolkit_recommendations(investigation)
    by_key = {item.key: item for item in recommendations}

    assert {"cro-core", "companies-house", "eu-bris", "sec-edgar"} <= set(by_key)
    assert by_key["cro-core"].jurisdiction == "Ireland"
    assert by_key["companies-house"].search_value == "Example Holdings Limited"
    assert by_key["sec-edgar"].sends_case_value is True
    assert by_key["sec-edgar"].provenance == "investigation.query"
    assert "Example+Holdings+Limited" in by_key["sec-edgar"].destination_url


def test_archive_today_is_explicit_bounded_and_only_for_public_positive_urls() -> None:
    investigation = name_case()
    investigation.findings.extend(
        [
            Finding(
                id="public-finding",
                query_type="name",
                queried_value=investigation.query,
                finding_type="profile",
                platform="Example",
                value="Example",
                canonical_url="https://example.org/profile?a=1",
                status="found",
                source_tool="bing_name",
                source_tools=["bing_name"],
            ),
            Finding(
                id="private-finding",
                query_type="name",
                queried_value=investigation.query,
                finding_type="profile",
                platform="Internal",
                value="Internal",
                canonical_url="http://127.0.0.1/admin",
                status="found",
                source_tool="bing_name",
                source_tools=["bing_name"],
            ),
            Finding(
                id="unknown-finding",
                query_type="name",
                queried_value=investigation.query,
                finding_type="profile",
                platform="Unknown",
                value="Unknown",
                canonical_url="https://example.net/candidate",
                status="unknown",
                source_tool="bing_name",
                source_tools=["bing_name"],
            ),
        ]
    )

    recommendations = toolkit_recommendations(investigation)
    archive = [item for item in recommendations if item.key.startswith("archive-today:")]
    assert len(archive) == 1
    assert archive[0].finding_id == "public-finding"
    assert archive[0].sends_case_value is True
    assert archive[0].search_value == "https://example.org/profile?a=1"
    assert "127.0.0.1" not in archive[0].destination_url


def test_recommendation_lookup_is_case_scoped() -> None:
    investigation = name_case()
    assert find_toolkit_recommendation(investigation, "cro-core") is not None
    assert find_toolkit_recommendation(investigation, "not-present") is None
