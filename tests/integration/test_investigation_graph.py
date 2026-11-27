import re
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db.session import SessionLocal, engine
from app.main import app
from app.models.db import ApplicationSetting, AuditEvent, Base, Finding, Investigation, ToolRun
from app.services.investigation_graph import graph_layout_setting_key


def test_investigation_graph_page_data_and_layout_persistence() -> None:
    Base.metadata.create_all(engine)
    investigation_id = "91111111-1111-1111-1111-111111111111"
    run_id = "92222222-2222-2222-2222-222222222222"
    finding_id = "93333333-3333-3333-3333-333333333333"
    observed_at = datetime(2026, 8, 1, 19, 0, tzinfo=UTC)
    with SessionLocal.begin() as db:
        db.execute(delete(AuditEvent).where(AuditEvent.investigation_id == investigation_id))
        existing = db.get(Investigation, investigation_id)
        if existing is not None:
            db.delete(existing)
        setting = db.get(ApplicationSetting, graph_layout_setting_key(investigation_id))
        if setting is not None:
            db.delete(setting)
        investigation = Investigation(
            id=investigation_id,
            title="Graph integration case",
            query="graph_example",
            query_type="username",
            selected_tools=["maigret"],
            tags=[],
            status="completed",
            created_at=observed_at,
        )
        run = ToolRun(
            id=run_id,
            investigation_id=investigation_id,
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
        finding = Finding(
            id=finding_id,
            investigation_id=investigation_id,
            tool_run_id=run_id,
            query_type="username",
            queried_value="graph_example",
            finding_type="profile",
            platform="GitHub",
            value="graph_example",
            canonical_url="https://github.com/graph_example",
            status="found",
            confidence=0.9,
            extra={
                "profile": {"display_name": "Graph Example"},
                "verification": {"state": "confirmed_account"},
            },
            source_tool="maigret",
            source_tools=["maigret"],
            observed_at=observed_at,
        )
        investigation.runs.append(run)
        investigation.findings.append(finding)
        run.findings.append(finding)
        db.add(investigation)

    try:
        with TestClient(app) as client:
            page = client.get(f"/investigations/{investigation_id}/graph")
            assert page.status_code == 200
            assert "Investigation graph" in page.text
            assert 'src="/static/graph.js"' in page.text
            assert 'name="htmx-config"' in page.text
            assert '"includeIndicatorStyles": false' in page.text
            assert "Include selected only" in page.text
            assert "Exclude selected" in page.text
            assert "Restore excluded" in page.text
            assert page.text.count("graph-help") >= 15
            assert page.text.count("data-help=") >= 15
            graph_script = client.get("/static/graph.js")
            assert graph_script.status_code == 200
            assert '["snapchat", "👻"]' in graph_script.text
            assert "platformIcon" in graph_script.text
            assert "favicon" not in graph_script.text.casefold()
            assert "img-src 'self' data: blob:" in page.headers["content-security-policy"]
            csrf_token = re.search(r'"csrf":\s*"([^"]+)"', page.text).group(1)

            graph_data = client.get(f"/investigations/{investigation_id}/graph/data")
            assert graph_data.status_code == 200
            payload = graph_data.json()
            assert payload["investigation"]["id"] == investigation_id
            assert any(node["data"]["id"] == "subject" for node in payload["nodes"])
            assert any(
                node["data"]["id"] == f"finding:{finding_id}"
                and node["data"]["state"] == "confirmed"
                for node in payload["nodes"]
            )
            assert any(edge["data"]["relation"] == "found_by" for edge in payload["edges"])

            saved = client.post(
                f"/investigations/{investigation_id}/graph/layout",
                json={
                    "csrf": csrf_token,
                    "positions": {
                        "subject": {"x": 123.45, "y": -67.89},
                        "unknown-node": {"x": 1, "y": 2},
                    },
                },
            )
            assert saved.status_code == 200
            assert saved.json() == {"status": "saved", "saved_position_count": 1}

            updated = client.get(f"/investigations/{investigation_id}/graph/data").json()
            subject = next(node for node in updated["nodes"] if node["data"]["id"] == "subject")
            assert subject["position"] == {"x": 123.45, "y": -67.89}

            reset = client.post(
                f"/investigations/{investigation_id}/graph/layout",
                json={"csrf": csrf_token, "action": "reset"},
            )
            assert reset.status_code == 200
            assert reset.json() == {"status": "reset", "saved_position_count": 0}

        with SessionLocal() as db:
            assert db.get(ApplicationSetting, graph_layout_setting_key(investigation_id)) is None
            event_types = set(
                db.scalars(
                    select(AuditEvent.event_type).where(
                        AuditEvent.investigation_id == investigation_id
                    )
                )
            )
            assert {
                "investigation_graph_layout_saved",
                "investigation_graph_layout_reset",
            } <= event_types
    finally:
        with SessionLocal.begin() as db:
            db.execute(delete(AuditEvent).where(AuditEvent.investigation_id == investigation_id))
            item = db.get(Investigation, investigation_id)
            if item is not None:
                db.delete(item)
            setting = db.get(ApplicationSetting, graph_layout_setting_key(investigation_id))
            if setting is not None:
                db.delete(setting)
