from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.db import AuditEvent, Base, Finding, Investigation, ToolRun
from app.services import automatic_wayback
from app.services.automatic_wayback import (
    apply_automatic_wayback_results,
    automatic_wayback_targets,
    discover_automatic_wayback,
)
from app.services.wayback import WaybackCapture


def investigation() -> Investigation:
    item = Investigation(
        title="Automatic Wayback",
        query="example",
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
    url: str,
    *,
    profile: dict[str, object] | None = None,
) -> Finding:
    return Finding(
        id=finding_id,
        query_type="username",
        queried_value="example",
        finding_type="profile",
        platform="Example Social",
        value="example",
        canonical_url=url,
        status="found",
        extra={"profile": profile or {}},
        source_tool="maigret",
        source_tools=["maigret"],
        observed_at=datetime(2026, 7, 20, tzinfo=UTC),
    )


def test_wayback_targets_prioritise_rich_public_profiles_and_skip_api_urls() -> None:
    item = investigation()
    rich = finding(
        "rich",
        "https://social.example/example",
        profile={"display_name": "Example Person", "avatar_url": "https://cdn.example/a.jpg"},
    )
    plain = finding("plain", "https://other.example/example")
    api = finding("api", "https://api.example/users/example")
    checked = finding("checked", "https://checked.example/example")
    checked.extra = {
        "profile": {},
        "wayback": {"automatic_checked_at": "2026-07-20T12:00:00+00:00"},
    }
    item.findings.extend([plain, api, checked, rich])

    targets = automatic_wayback_targets(item)

    assert [target.finding_id for target in targets] == ["rich", "plain"]


@pytest.mark.asyncio
async def test_automatic_wayback_discovers_and_persists_first_latest_capture(monkeypatch) -> None:
    captures = [
        WaybackCapture(
            "20200102030405", "https://social.example/example", 200, "text/html", "a", 10
        ),
        WaybackCapture(
            "20250607080910", "https://social.example/example", 200, "text/html", "b", 20
        ),
    ]

    async def fake_discover(target: str, *, limit: int = 50):
        assert target == "https://social.example/example"
        assert limit == 50
        return captures

    monkeypatch.setattr(automatic_wayback, "discover_captures", fake_discover)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        item = investigation()
        profile = finding("profile", "https://social.example/example")
        item.findings.append(profile)
        item.runs[0].findings.append(profile)
        db.add(item)
        db.flush()

        targets = automatic_wayback_targets(item)
        results = await discover_automatic_wayback(targets)
        summary = apply_automatic_wayback_results(db, item, results)
        db.commit()

        stored = db.get(Finding, "profile")
        event = db.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "wayback_automatic_discovery")
        )

    assert summary == {
        "targets_checked": 1,
        "targets_archived": 1,
        "captures_found": 2,
        "targets_failed": 0,
    }
    assert stored is not None
    metadata = stored.extra["wayback"]
    assert metadata["capture_count"] == 2
    assert metadata["first_capture"]["timestamp"] == "20200102030405"
    assert metadata["latest_capture"]["timestamp"] == "20250607080910"
    assert [record["timestamp"] for record in metadata["cdx_records"]] == [
        "20200102030405",
        "20250607080910",
    ]
    assert metadata["cdx_endpoint"] == "https://web.archive.org/cdx/search/cdx"
    assert metadata["capture_count_is_distinct"] is True
    assert metadata["capture_limit"] == 50
    assert event is not None
    assert event.details["captures_found"] == 2
