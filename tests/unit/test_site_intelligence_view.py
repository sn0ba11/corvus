from app.models.db import Finding, Investigation, ToolRun
from app.services.site_intelligence_view import build_site_intelligence_view


def _finding(kind: str, value: str, extra: dict[str, object] | None = None) -> Finding:
    return Finding(
        investigation_id="case-1",
        tool_run_id="run-1",
        query_type="domain",
        queried_value="example.org",
        finding_type=kind,
        platform="Live Site",
        value=value,
        canonical_url=value if value.startswith("http") else None,
        display_url=value if value.startswith("http") else None,
        status="found",
        confidence=0.9,
        title=value,
        description=None,
        extra=extra or {},
        source_tool="site_intelligence",
        source_tools=["site_intelligence"],
        source_record_id=value,
        raw_output_reference="raw/ref",
    )


def test_site_intelligence_view_groups_current_site_observations() -> None:
    investigation = Investigation(
        id="case-1",
        query="example.org",
        query_type="domain",
        title="Example",
        status="completed",
    )
    investigation.runs = [ToolRun(id="run-1", investigation_id="case-1", tool_id="site_intelligence", display_name="Live Site Intelligence", status="completed", timeout_seconds=120)]
    investigation.findings = [
        _finding("email", "ops@example.org", {"source_url": "https://example.org/contact"}),
        _finding("tracking_identifier", "G-ABCDEF12", {"tracking_type": "google_analytics_ga4", "source_url": "https://example.org/"}),
        _finding("javascript_endpoint", "https://example.org/api/v1", {"script_url": "https://example.org/app.js", "source_url": "https://example.org/"}),
    ]

    view = build_site_intelligence_view(investigation)
    assert view["enabled"] is True
    assert view["total"] == 3
    assert view["counts"]["tracking_identifier"] == 1
    assert view["groups"]["tracking_identifier"][0]["tracking_type"] == "google_analytics_ga4"
    assert view["groups"]["javascript_endpoint"][0]["script_url"] == "https://example.org/app.js"
