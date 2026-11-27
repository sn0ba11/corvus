from pathlib import Path

import pytest

from app.models.db import Investigation, ToolRun
from app.services.tool_run_logs import (
    MAX_TOOL_LOG_BYTES,
    ToolRunLogError,
    load_tool_run_log,
)


def investigation_with_run() -> tuple[Investigation, ToolRun]:
    investigation = Investigation(
        id="inv-tool-log",
        title="Tool log case",
        query="example",
        query_type="username",
        status="completed",
    )
    run = ToolRun(
        id="run-sherlock",
        investigation_id=investigation.id,
        tool_id="sherlock",
        display_name="Sherlock",
        status="timed_out",
        timeout_seconds=180,
    )
    investigation.runs.append(run)
    return investigation, run


def test_loads_case_scoped_execution_log(tmp_path: Path) -> None:
    investigation, run = investigation_with_run()
    root = tmp_path / "raw" / investigation.id / run.id
    root.mkdir(parents=True)
    content = "STDOUT\nChecking sites\n\nSTDERR\nOne request timed out"
    (root / "process-output.txt").write_text(content, encoding="utf-8")

    log = load_tool_run_log(tmp_path, investigation, run)

    assert log.content == content
    assert log.size_bytes == len(content.encode())
    assert log.filename == "process-output.txt"


def test_rejects_wrong_run_missing_and_oversized_logs(tmp_path: Path) -> None:
    investigation, run = investigation_with_run()
    other = ToolRun(
        id="other-run",
        investigation_id="another-investigation",
        tool_id="sherlock",
        display_name="Sherlock",
        status="failed",
        timeout_seconds=180,
    )
    with pytest.raises(ToolRunLogError, match="does not belong"):
        load_tool_run_log(tmp_path, investigation, other)

    with pytest.raises(ToolRunLogError, match="no execution log"):
        load_tool_run_log(tmp_path, investigation, run)

    root = tmp_path / "raw" / investigation.id / run.id
    root.mkdir(parents=True)
    (root / "process-output.txt").write_bytes(b"x" * (MAX_TOOL_LOG_BYTES + 1))
    with pytest.raises(ToolRunLogError, match="5 MiB"):
        load_tool_run_log(tmp_path, investigation, run)


def test_rejects_run_directory_symlink_escape(tmp_path: Path) -> None:
    investigation, run = investigation_with_run()
    outside = tmp_path / "outside-run"
    outside.mkdir()
    (outside / "process-output.txt").write_text("outside evidence", encoding="utf-8")
    run_parent = tmp_path / "raw" / investigation.id
    run_parent.mkdir(parents=True)
    (run_parent / run.id).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ToolRunLogError, match="no execution log"):
        load_tool_run_log(tmp_path, investigation, run)
