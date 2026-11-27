import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.db import AuditEvent, Base, Finding, Investigation, ToolRun
from app.services.search_imports import (
    SearchResultItem,
    import_search_result_items,
    import_search_results,
    parse_search_results,
    unwrap_search_url,
)


def test_parses_lines_json_and_google_redirects_while_rejecting_private_urls() -> None:
    raw = "\n".join(
        [
            "https://www.google.com/url?q=https%3A%2F%2Fexample.org%2Fprofile%3Futm_source%3Dsearch\tProfile\tSnippet",
            "http://127.0.0.1/admin\tPrivate",
            "not a URL",
        ]
    )
    items, rejected = parse_search_results(raw)
    assert len(items) == 1
    assert items[0].url == "https://example.org/profile"
    assert items[0].title == "Profile"
    assert items[0].snippet == "Snippet"
    assert rejected == 2

    json_items, json_rejected = parse_search_results(
        json.dumps(
            [
                {"link": "https://example.net/a", "name": "A", "description": "One"},
                "https://example.net/b",
            ]
        )
    )
    assert [item.url for item in json_items] == [
        "https://example.net/a",
        "https://example.net/b",
    ]
    assert json_rejected == 0


def test_unwraps_bing_click_redirects() -> None:
    encoded = "a1aHR0cHM6Ly93d3cuaW5zdGFncmFtLmNvbS9jb3J2dXNfZml4dHVyZV9ucTdhei8="
    assert unwrap_search_url(f"https://www.bing.com/ck/a?u={encoded}&ntb=1") == (
        "https://www.instagram.com/corvus_fixture_nq7az/"
    )


def test_rejects_empty_oversized_and_invalid_result_sets() -> None:
    with pytest.raises(ValueError, match="paste at least one"):
        parse_search_results("")
    with pytest.raises(ValueError, match="64 KiB"):
        parse_search_results("https://example.org/" + "a" * (65 * 1024))
    with pytest.raises(ValueError, match="no valid public"):
        parse_search_results("http://localhost/private")


def test_imports_new_results_and_merges_duplicate_provenance() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        investigation = Investigation(
            title="Search import",
            query="johnsmith",
            query_type="username",
            selected_tools=["maigret"],
            tags=[],
            status="completed",
        )
        source_run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
        existing = Finding(
            query_type="username",
            queried_value="johnsmith",
            finding_type="profile",
            platform="example.org",
            value="johnsmith",
            canonical_url="https://example.org/profile",
            display_url="https://example.org/profile",
            status="unknown",
            extra={"profile": {}, "provenance": [{"tool": "maigret"}]},
            source_tool="maigret",
            source_tools=["maigret"],
            observed_at=datetime(2026, 7, 20, tzinfo=UTC),
        )
        investigation.runs.append(source_run)
        investigation.findings.append(existing)
        source_run.findings.append(existing)
        db.add(investigation)
        db.flush()

        summary = import_search_results(
            db,
            investigation,
            raw=(
                "https://example.org/profile\tExisting profile\tDuplicate snippet\n"
                "https://new.example.net/user?utm_source=google\tNew result\tPublic snippet\n"
                "http://127.0.0.1/private"
            ),
            engine="google",
            query='"johnsmith" site:example.net',
            query_id="google-template-1",
            query_label="Example search",
        )
        db.commit()

        assert summary.imported_count == 1
        assert summary.duplicate_count == 1
        assert summary.rejected_count == 1
        findings = list(
            db.scalars(select(Finding).where(Finding.investigation_id == investigation.id))
        )
        assert len(findings) == 2
        merged = next(
            item for item in findings if item.canonical_url == "https://example.org/profile"
        )
        assert merged.source_tools == ["maigret", "search_import"]
        assert merged.status == "likely"
        assert merged.title == "Existing profile"
        assert len(merged.extra["provenance"]) == 2
        assert [item["status"] for item in merged.extra["status_observations"]] == [
            "unknown",
            "likely",
        ]
        imported = next(item for item in findings if item.source_tool == "search_import")
        assert imported.canonical_url == "https://new.example.net/user"
        assert imported.status == "likely"
        assert imported.title == "New result"
        run = db.get(ToolRun, summary.run_id)
        assert run is not None
        assert run.tool_id == "search_import"
        assert run.query_value == '"johnsmith" site:example.net'
        event = db.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "search_results_imported")
        )
        assert event is not None
        assert event.details["imported_count"] == 1
        assert event.details["duplicate_count"] == 1


def test_import_search_result_items_honours_non_default_source_tool() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        investigation = Investigation(
            title="Search import",
            query="johnsmith",
            query_type="username",
            selected_tools=[],
            tags=[],
            status="completed",
        )
        db.add(investigation)
        db.flush()

        summary = import_search_result_items(
            db,
            investigation,
            [SearchResultItem(url="https://example.org/johnsmith", title="John Smith", snippet=None)],
            0,
            engine="google",
            query="johnsmith",
            query_id="browser-google-abc123",
            query_label="Google search (sock browser): johnsmith",
            source_tool="search_browser_capture",
            tool_display_name="Search result (managed browser)",
            event_type="search_results_captured",
        )
        db.commit()

        assert summary.imported_count == 1
        finding = db.scalar(select(Finding))
        assert finding is not None
        assert finding.source_tool == "search_browser_capture"
        assert finding.source_tools == ["search_browser_capture"]
        assert "search_browser_capture" in finding.extra
        assert "search_import" not in finding.extra
        run = db.get(ToolRun, summary.run_id)
        assert run is not None
        assert run.tool_id == "search_browser_capture"
        assert run.display_name == "Search result (managed browser)"
        event = db.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "search_results_captured")
        )
        assert event is not None
        assert event.details["engine"] == "google"


def test_account_page_results_keep_a_handle_and_snippet_details() -> None:
    from app.services.search_imports import snippet_profile_details

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        investigation = Investigation(title="t", query="Example Person", query_type="name")
        db.add(investigation)
        db.flush()
        import_search_result_items(
            db, investigation,
            [
                SearchResultItem(
                    url="https://www.instagram.com/example_user/",
                    title="Example Person (@example_user) • Instagram photos and videos",
                    snippet="1,204 Followers, 310 Following, 88 Posts - See Instagram photos",
                ),
                SearchResultItem(url="https://www.instagram.com/p/Cabc123/", title="A post", snippet="5 likes"),
            ],
            0, engine="duckduckgo", query='"Example Person"', query_id="q1", query_label="label",
        )
        db.flush()
        findings = {finding.canonical_url: finding for finding in db.scalars(select(Finding))}
    profile = findings["https://www.instagram.com/example_user/"]
    assert profile.extra["linked_account"] == {
        "platform": "Instagram", "username": "example_user", "source": "search_result_url"}
    assert {"label": "Followers (search snippet)", "value": "1,204"} in profile.extra["platform_details"]
    assert {"label": "Display name (search result title)", "value": "Example Person"} in profile.extra["platform_details"]
    post = findings["https://www.instagram.com/p/Cabc123/"]
    assert "linked_account" not in post.extra and "platform_details" not in post.extra
    assert snippet_profile_details(SearchResultItem(url="https://x.com/a", title=None, snippet="2.5M followers · 12 following")) == [
        {"label": "Followers (search snippet)", "value": "2.5M"},
        {"label": "Following (search snippet)", "value": "12"},
    ]
