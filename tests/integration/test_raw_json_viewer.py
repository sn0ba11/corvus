import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.db import Base, Finding, Investigation, ToolRun


def test_json_syntax_highlighter_is_local_text_safe_and_lazy() -> None:
    source = (Path(__file__).parents[2] / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "document.createTextNode" in source
    assert "code.replaceChildren(fragment)" in source
    assert 'container.closest(".json-dialog")' in source
    assert "innerHTML" not in source
    assert (
        "https://"
        not in source.split("const jsonTokenPattern", 1)[1].split("const avatarDialog", 1)[0]
    )


def test_investigation_raw_json_viewer_and_download() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        investigation = Investigation(
            title="Raw JSON investigation",
            query="duolingo",
            query_type="username",
            selected_tools=["maigret"],
            status="completed",
        )
        run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
        finding = Finding(
            query_type="username",
            queried_value="duolingo",
            finding_type="profile",
            platform="Duolingo",
            value="duolingo",
            canonical_url="https://www.duolingo.com/profile/duolingo",
            display_url="https://www.duolingo.com/profile/duolingo",
            status="unknown",
            extra={
                "profile": {
                    "account_id": "Duolingo",
                    "display_name": "Duolingo",
                    "followers": 12,
                    "following": 3,
                },
                "platform_details": [
                    {"label": "Streak", "value": "42"},
                    {"label": "Current course", "value": "English (en) → Irish (ga)"},
                ],
            },
            source_tool="maigret",
            source_tools=["maigret"],
        )
        empty_run = ToolRun(
            tool_id="sherlock",
            display_name="Sherlock",
            status="timed_out",
            timeout_seconds=180,
            error_message="collector exceeded its timeout",
        )
        investigation.runs.extend([run, empty_run])
        investigation.findings.append(finding)
        run.findings.append(finding)
        db.add(investigation)
        db.flush()
        investigation_id = investigation.id
        run_id = run.id
        empty_run_id = empty_run.id

    root = get_settings().data_dir / "raw" / investigation_id / run_id
    root.mkdir(parents=True, exist_ok=True)
    artifact = root / "report_duolingo_simple.json"
    artifact.write_text(
        json.dumps({"Duolingo": {"streak": 42, "fromLanguage": "en"}}),
        encoding="utf-8",
    )
    empty_root = get_settings().data_dir / "raw" / investigation_id / empty_run_id
    empty_root.mkdir(parents=True, exist_ok=True)
    (empty_root / "process-output.txt").write_text(
        "STDOUT\nChecking sites...\n\nSTDERR\nRequest timed out on example.invalid",
        encoding="utf-8",
    )

    with TestClient(app) as client:
        case_page = client.get(f"/investigations/{investigation_id}")
        assert case_page.status_code == 200
        assert "View raw JSON" in case_page.text
        assert "Execution log" in case_page.text
        assert "12 followers" in case_page.text
        assert "3 following" in case_page.text
        assert "Streak:" in case_page.text
        assert "English (en) → Irish (ga)" in case_page.text
        assert f'data-json-dialog="finding-json-{finding.id}"' in case_page.text
        assert "json-record-code" in case_page.text
        assert "data-json-syntax" in case_page.text

        report = client.get(f"/investigations/{investigation_id}/report")
        assert report.status_code == 200
        assert "12 followers" in report.text
        assert "3 following" in report.text
        assert "Current course:" in report.text
        assert "English (en) → Irish (ga)" in report.text
        assert "unknown" in report.text

        viewer = client.get(f"/investigations/{investigation_id}/raw-json")
        assert viewer.status_code == 200
        assert "Raw JSON viewer" in viewer.text
        assert "report_duolingo_simple.json" in viewer.text
        assert "streak" in viewer.text
        assert "42" in viewer.text
        assert 'id="raw-json-document"' in viewer.text
        assert "raw-json-code" in viewer.text
        assert 'data-copy-json-target="raw-json-document"' in viewer.text
        assert 'data-toggle-json-wrap-target="raw-json-document"' in viewer.text

        empty_viewer = client.get(
            f"/investigations/{investigation_id}/raw-json",
            params={"run_id": empty_run_id},
        )
        assert empty_viewer.status_code == 200
        assert "No JSON for this run" in empty_viewer.text
        assert "Sherlock" in empty_viewer.text
        assert "timed out" in empty_viewer.text
        assert "report_duolingo_simple.json" not in empty_viewer.text
        assert "will not fall back to another collector" in empty_viewer.text
        assert f"/runs/{empty_run_id}/log" in empty_viewer.text

        log_viewer = client.get(f"/investigations/{investigation_id}/runs/{empty_run_id}/log")
        assert log_viewer.status_code == 200
        assert "Sherlock log" in log_viewer.text
        assert "Checking sites..." in log_viewer.text
        assert "Request timed out on example.invalid" in log_viewer.text
        assert "collector exceeded its timeout" in log_viewer.text
        assert 'data-copy-json-target="tool-run-log"' in log_viewer.text

        log_download = client.get(
            f"/investigations/{investigation_id}/runs/{empty_run_id}/log",
            params={"download": "true"},
        )
        assert log_download.status_code == 200
        assert log_download.headers["content-type"].startswith("text/plain")
        assert "attachment" in log_download.headers["content-disposition"]
        assert "STDERR" in log_download.text

        downloaded = client.get(
            f"/investigations/{investigation_id}/raw-json",
            params={
                "run_id": run_id,
                "artifact": "report_duolingo_simple.json",
                "download": "true",
            },
        )
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"].startswith("application/json")
        assert "attachment" in downloaded.headers["content-disposition"]
        assert downloaded.json()["Duolingo"]["streak"] == 42
