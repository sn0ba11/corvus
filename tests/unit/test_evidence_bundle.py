import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from app.models.db import Finding, Investigation, ToolRun
from app.services.evidence_bundle import BundleOptions, build_evidence_bundle


def _investigation() -> Investigation:
    investigation = Investigation(
        id="case-bundle",
        title="Bundle case",
        query="sensitive_subject",
        query_type="username",
        selected_tools=["maigret"],
        tags=[],
        notes="Sensitive analyst note",
        status="completed",
        created_at=datetime(2026, 8, 17, tzinfo=UTC),
    )
    run = ToolRun(
        id="run-one",
        investigation_id=investigation.id,
        tool_id="maigret",
        display_name="Maigret",
        status="completed",
        timeout_seconds=60,
    )
    first = Finding(
        id="finding-one",
        investigation_id=investigation.id,
        tool_run_id=run.id,
        query_type="username",
        queried_value="sensitive_subject",
        finding_type="profile",
        platform="Example",
        value="sensitive_subject",
        canonical_url="https://example.org/sensitive_subject",
        status="found",
        confidence=0.9,
        extra={"profile": {"display_name": "Sensitive Person"}},
        source_tool="maigret",
        source_tools=["maigret"],
        observed_at=datetime(2026, 8, 17, tzinfo=UTC),
    )
    second = Finding(
        id="finding-two",
        investigation_id=investigation.id,
        tool_run_id=run.id,
        query_type="username",
        queried_value="sensitive_subject",
        finding_type="profile",
        platform="Second",
        value="other",
        canonical_url="https://second.example/other",
        status="likely",
        confidence=0.6,
        extra={},
        source_tool="maigret",
        source_tools=["maigret"],
        observed_at=datetime(2026, 8, 17, tzinfo=UTC),
    )
    investigation.runs.append(run)
    investigation.findings.extend([first, second])
    run.findings.extend([first, second])
    return investigation


def test_bundle_selects_redacts_and_hashes_raw_evidence(tmp_path: Path) -> None:
    investigation = _investigation()
    raw_dir = tmp_path / "raw" / investigation.id / "run-one"
    raw_dir.mkdir(parents=True)
    raw_content = b"retained collector evidence"
    (raw_dir / "result.json").write_bytes(raw_content)

    content = build_evidence_bundle(
        investigation,
        tmp_path,
        BundleOptions(
            finding_ids=frozenset({"finding-one"}),
            redact_identifiers=True,
            redact_notes=True,
            redact_urls=True,
            include_raw=True,
        ),
    )

    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = set(archive.namelist())
        assert {"case.json", "manifest.json", "raw/run-one/result.json"} <= names
        case = json.loads(archive.read("case.json"))
        assert [item["id"] for item in case["findings"]] == ["finding-one"]
        assert case["investigation"]["query"] == "[REDACTED]"
        assert case["investigation"]["notes"] == "[REDACTED]"
        assert case["findings"][0]["canonical_url"] == "[REDACTED]"
        manifest = json.loads(archive.read("manifest.json"))
        raw_entry = next(item for item in manifest["files"] if item["path"] == "raw/run-one/result.json")
        assert raw_entry["sha256"] == hashlib.sha256(raw_content).hexdigest()
        assert manifest["selection_applied"] is True


def test_bundle_without_selection_keeps_complete_finding_register(tmp_path: Path) -> None:
    investigation = _investigation()
    content = build_evidence_bundle(
        investigation,
        tmp_path,
        BundleOptions(include_raw=False),
    )
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        case = json.loads(archive.read("case.json"))
        assert {item["id"] for item in case["findings"]} == {"finding-one", "finding-two"}
        assert case["summary"]["bundle_selection_applied"] is False
