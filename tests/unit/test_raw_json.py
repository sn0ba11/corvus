import json
from pathlib import Path

import pytest

from app.models.db import Investigation, ToolRun
from app.services.raw_json import (
    MAX_RAW_JSON_ARTIFACTS_PER_RUN,
    MAX_RAW_JSON_BYTES,
    RawJsonError,
    list_raw_json_artifacts,
    load_raw_json_artifact,
)


def investigation_with_run() -> tuple[Investigation, ToolRun]:
    investigation = Investigation(
        id="inv-raw-json",
        title="Raw JSON case",
        query="example",
        query_type="username",
        status="completed",
    )
    run = ToolRun(
        id="run-maigret",
        investigation_id=investigation.id,
        tool_id="maigret",
        display_name="Maigret",
        status="completed",
        timeout_seconds=60,
    )
    investigation.runs.append(run)
    return investigation, run


def test_lists_and_loads_only_case_scoped_json(tmp_path: Path) -> None:
    investigation, run = investigation_with_run()
    root = tmp_path / "raw" / investigation.id / run.id
    root.mkdir(parents=True)
    result = root / "report_example_simple.json"
    result.write_text(json.dumps({"Duolingo": {"streak": 42}}), encoding="utf-8")
    (root / "process-output.txt").write_text("not json", encoding="utf-8")

    artifacts = list_raw_json_artifacts(tmp_path, investigation)
    assert [(item.filename, item.category) for item in artifacts] == [
        ("report_example_simple.json", "collector result")
    ]

    raw, pretty, payload = load_raw_json_artifact(
        tmp_path, investigation, run, "report_example_simple.json"
    )
    assert json.loads(raw) == payload
    assert '"streak": 42' in pretty


def test_raw_json_listing_is_bounded_per_run(tmp_path: Path) -> None:
    investigation, run = investigation_with_run()
    root = tmp_path / "raw" / investigation.id / run.id
    root.mkdir(parents=True)
    for index in range(MAX_RAW_JSON_ARTIFACTS_PER_RUN + 25):
        (root / f"artifact-{index:04d}.json").write_text("{}", encoding="utf-8")

    artifacts = list_raw_json_artifacts(tmp_path, investigation)

    assert len(artifacts) == MAX_RAW_JSON_ARTIFACTS_PER_RUN
    assert artifacts[0].filename == "artifact-0000.json"
    assert artifacts[-1].filename == f"artifact-{MAX_RAW_JSON_ARTIFACTS_PER_RUN - 1:04d}.json"


def test_raw_json_rejects_traversal_wrong_run_and_large_files(tmp_path: Path) -> None:
    investigation, run = investigation_with_run()
    root = tmp_path / "raw" / investigation.id / run.id
    root.mkdir(parents=True)
    (root / "result.json").write_text("{}", encoding="utf-8")

    with pytest.raises(RawJsonError, match="invalid raw JSON path"):
        load_raw_json_artifact(tmp_path, investigation, run, "../result.json")

    other = ToolRun(
        id="other-run",
        investigation_id="another-investigation",
        tool_id="maigret",
        display_name="Maigret",
        status="completed",
        timeout_seconds=60,
    )
    with pytest.raises(RawJsonError, match="does not belong"):
        load_raw_json_artifact(tmp_path, investigation, other, "result.json")

    oversized = root / "large.json"
    oversized.write_bytes(b" " * (MAX_RAW_JSON_BYTES + 1))
    with pytest.raises(RawJsonError, match="5 MiB"):
        load_raw_json_artifact(tmp_path, investigation, run, "large.json")
