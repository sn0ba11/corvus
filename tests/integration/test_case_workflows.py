import asyncio
import csv
import io
import re
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.adapters.base import Adapter, ExecutionResult, HealthResult
from app.api import routes
from app.db.session import SessionLocal
from app.main import app
from app.models.db import AuditEvent, Finding, Investigation, ToolRun
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services import jobs
from app.services.jobs import JobManager


def token(client: TestClient) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text)
    assert match
    return match.group(1)


def seed_case(title="Comparison fixture", run_status="completed", url="https://github.com/shared-workflow"):  # type: ignore[no-untyped-def]
    with SessionLocal.begin() as db:
        case = Investigation(title=title, query="workflow-fixture", query_type="username",
                             selected_tools=["github_discovery"], tags=[], status="completed")
        db.add(case)
        db.flush()
        run = ToolRun(investigation_id=case.id, tool_id="github_discovery", display_name="GitHub Public Discovery",
                      status=run_status, timeout_seconds=45, query_value="pivot-workflow",
                      query_type="username", pivot_depth=1, pivot_automatic=False,
                      error_code="connection_error" if run_status == "failed" else None,
                      raw_output_reference="original-retained-log.txt")
        db.add(run)
        db.flush()
        finding = Finding(investigation_id=case.id, tool_run_id=run.id, query_type="username",
                          queried_value="pivot-workflow", finding_type="profile", platform="GitHub",
                          value="shared-workflow", canonical_url=url, status="found", source_tool="github_discovery",
                          source_tools=["github_discovery"], extra={"public_email": "public@example.org"},
                          observed_at=datetime.now(UTC))
        db.add(finding)
        db.flush()
        return case.id, run.id, finding.id


def test_comparison_selection_html_and_exports_are_case_scoped(monkeypatch):  # type: ignore[no-untyped-def]
    started = []
    monkeypatch.setattr(routes.job_manager, "start", started.append)
    with TestClient(app) as client:
        first, _, first_finding = seed_case("=FIRST workflow case")
        second, _, second_finding = seed_case("SECOND <script>alert(1)</script>")
        unrelated, _, unrelated_finding = seed_case("Unselected confidential case")
        csrf = token(client)
        selected = client.post("/compare-cases", data={"csrf": csrf, "investigation_ids": [first, second]}, follow_redirects=False)
        assert selected.status_code == 303
        location = selected.headers["location"]
        page = client.get(location)
        assert page.status_code == 200
        assert "Shared evidence across 2 cases" in page.text
        assert "SECOND &lt;script&gt;" in page.text
        assert "<script>alert(1)</script>" not in page.text
        assert "Unselected confidential case" not in page.text
        assert "Export evidence CSV" in page.text
        response = client.get(location + "&format=json")
        report = response.json()
        assert {case["id"] for case in report["cases"]} == {first, second}
        assert report["total_shared_signals"] == 2
        evidence_ids = {evidence["finding_id"] for signal in report["signals"] for evidence in signal["evidence"]}
        assert evidence_ids == {first_finding, second_finding}
        assert unrelated_finding not in response.text
        assert unrelated not in response.text
        assert response.headers["cache-control"] == "no-store"
        exported = client.get(location + "&format=csv")
        records = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        assert len(records) == 4
        assert any(row["case_title"] == "'=FIRST workflow case" for row in records)
        assert {row["finding_id"] for row in records} == evidence_ids
        assert started == []


def test_comparison_rejects_invalid_active_missing_and_duplicate_selections():  # type: ignore[no-untyped-def]
    with TestClient(app) as client:
        first, _, _ = seed_case()
        second, _, _ = seed_case()
        csrf = token(client)
        assert client.post("/compare-cases", data={"investigation_ids": [first, second]}).status_code == 403
        assert client.get("/compare-cases", params={"case": [first, first]}).status_code == 422
        assert client.get("/compare-cases", params={"case": [first]}).status_code == 422
        assert client.get("/compare-cases", params={"case": [first, str(uuid4())]}).status_code == 404
        assert client.get("/compare-cases", params={"case": [first, second], "format": "exe"}).status_code == 422
        with SessionLocal.begin() as db:
            db.get(Investigation, second).status = "running"
        assert client.get("/compare-cases", params={"case": [first, second]}).status_code == 409
        with SessionLocal.begin() as db:
            db.get(Investigation, second).status = "completed"
        assert client.post("/compare-cases", data={"csrf": csrf, "investigation_ids": [first, second]}, follow_redirects=False).status_code == 303


def test_retry_executes_new_attempt_and_preserves_original_evidence(monkeypatch):  # type: ignore[no-untyped-def]
    started = []
    monkeypatch.setattr(routes.job_manager, "start", started.append)
    with TestClient(app) as client:
        case_id, old_id, finding_id = seed_case(run_status="failed")
        page = client.get(f"/investigations/{case_id}")
        assert f"/runs/{old_id}/retry" in page.text
        response = client.post(f"/investigations/{case_id}/runs/{old_id}/retry",
                               data={"csrf": token(client), "timeout": "120"}, follow_redirects=False)
        assert response.status_code == 303
        assert started == [case_id]
        with SessionLocal() as db:
            audit = db.scalar(select(AuditEvent).where(AuditEvent.investigation_id == case_id,
                                                      AuditEvent.event_type == "collector_retry_queued"))
            retry_id = audit.details["retry_run_id"]
            retry = db.get(ToolRun, retry_id)
            assert retry.timeout_seconds == 120
            assert retry.query_value == "pivot-workflow"
            assert retry.pivot_depth == 1
            assert db.get(ToolRun, old_id).status == "failed"
            assert db.get(Finding, finding_id) is not None
        assert client.post(f"/investigations/{case_id}/runs/{old_id}/retry",
                           data={"csrf": token(client)}).status_code == 409
        assert f'href="#run-{retry_id}"' in client.get(f"/investigations/{case_id}").text

        class FixtureAdapter(Adapter):
            tool_id = "github_discovery"

            async def healthcheck(self):  # type: ignore[no-untyped-def]
                return HealthResult(True, "controlled fixture", "test")

            def build_command(self, query, query_type, output_dir, timeout, proxy=None):  # type: ignore[no-untyped-def]
                assert query == "pivot-workflow" and timeout == 120
                return ["controlled-fixture"]

            async def execute(self, command, output_dir, timeout, query, log):  # type: ignore[no-untyped-def]
                return ExecutionResult(0, "fixture result", "")

            def parse_results(self, result, output_dir):  # type: ignore[no-untyped-def]
                return [{"email": "retry-result@example.org"}]

            def normalise_results(self, records, query, query_type, raw_reference):  # type: ignore[no-untyped-def]
                return [FindingRecord(query_type=QueryType.USERNAME, queried_value=query,
                                      finding_type="public_email", value=records[0]["email"],
                                      status=FindingStatus.FOUND, source_tool=self.tool_id)]

        monkeypatch.setattr(jobs, "get_adapter", lambda _: FixtureAdapter())
        asyncio.run(JobManager()._run_tool(case_id, retry_id))
        with SessionLocal() as db:
            assert db.get(ToolRun, retry_id).status == "completed"
            original = db.get(ToolRun, old_id)
            assert original.status == "failed"
            assert original.raw_output_reference == "original-retained-log.txt"
            assert db.get(Finding, finding_id) is not None
            result = db.scalar(select(Finding).where(Finding.tool_run_id == retry_id))
            assert result.value == "retry-result@example.org"


@pytest.mark.parametrize("state,rate_limited,expected", [("completed", False, 422), ("failed", True, 422), ("timed_out", False, 303), ("cancelled", False, 303)])
def test_retry_eligibility_and_csrf(monkeypatch, state, rate_limited, expected):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes.job_manager, "start", lambda _: None)
    with TestClient(app) as client:
        case_id, run_id, _ = seed_case(run_status=state)
        with SessionLocal.begin() as db:
            db.get(ToolRun, run_id).rate_limited = rate_limited
        url = f"/investigations/{case_id}/runs/{run_id}/retry"
        assert client.post(url, data={}).status_code == 403
        assert client.post(url, data={"csrf": token(client), "timeout": 99999}).status_code == 422
        assert client.post(url, data={"csrf": token(client)}, follow_redirects=False).status_code == expected


def test_retry_cannot_target_another_case_or_an_active_case(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes.job_manager, "start", lambda _: None)
    with TestClient(app) as client:
        first, first_run, _ = seed_case(run_status="failed")
        second, other_run, _ = seed_case(run_status="failed")
        csrf = token(client)
        assert client.post(f"/investigations/{first}/runs/{other_run}/retry", data={"csrf": csrf}).status_code == 404
        with SessionLocal.begin() as db:
            db.get(Investigation, first).status = "running"
        assert client.post(f"/investigations/{first}/runs/{first_run}/retry", data={"csrf": csrf}).status_code == 409
        with SessionLocal() as db:
            assert len(list(db.scalars(select(ToolRun).where(ToolRun.investigation_id.in_([first, second]))))) == 2


def test_deeper_username_search_extends_the_existing_case_once(monkeypatch):  # type: ignore[no-untyped-def]
    started = []
    monkeypatch.setattr(routes.job_manager, "start", started.append)
    with TestClient(app) as client:
        case_id, original_id, finding_id = seed_case()
        page = client.get(f"/investigations/{case_id}")
        assert "Run deeper username search" in page.text
        url = f"/investigations/{case_id}/deeper-username-search"
        assert client.post(url, data={}).status_code == 403
        response = client.post(url, data={"csrf": token(client)}, follow_redirects=False)
        assert response.status_code == 303
        assert started == [case_id]
        with SessionLocal() as db:
            case = db.get(Investigation, case_id)
            assert case.extended_username_search
            assert case.status == "queued"
            new_runs = [run for run in case.runs if run.id != original_id]
            assert {run.tool_id for run in new_runs} == {"maigret", "naminter"}
            assert all(run.query_value == case.query and run.query_type == "username" for run in new_runs)
            assert db.get(Finding, finding_id) is not None
        assert client.post(url, data={"csrf": token(client)}).status_code == 409
        assert started == [case_id]


def test_deeper_search_rejects_active_and_non_username_cases(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes.job_manager, "start", lambda _: pytest.fail("No collection may start"))
    with TestClient(app) as client:
        case_id, _, _ = seed_case()
        url = f"/investigations/{case_id}/deeper-username-search"
        csrf = token(client)
        with SessionLocal.begin() as db:
            db.get(Investigation, case_id).status = "running"
        assert client.post(url, data={"csrf": csrf}).status_code == 409
        with SessionLocal.begin() as db:
            case = db.get(Investigation, case_id)
            case.status = "completed"
            case.query_type = "email"
        assert client.post(url, data={"csrf": csrf}).status_code == 422


def test_explicit_username_type_handles_dotted_names_and_pivot_setting(monkeypatch):  # type: ignore[no-untyped-def]
    started = []
    monkeypatch.setattr(routes.job_manager, "start", started.append)
    with TestClient(app) as client:
        response = client.post("/investigations", data={
            "csrf": token(client), "query": "fixture.name", "query_type": "username",
            "query_type_override": "on", "tools": "username_public_profiles",
            "auto_follow_username_pivots": "on",
        }, follow_redirects=False)
        assert response.status_code == 303
        case_id = response.headers["location"].rsplit("/", 1)[-1]
        with SessionLocal() as db:
            case = db.get(Investigation, case_id)
            assert case.query_type == "username"
            assert case.auto_follow_username_pivots
            assert case.selected_tools == ["username_public_profiles"]
        assert started == [case_id]
