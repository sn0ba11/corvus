from pathlib import Path

import pytest
from adapter import ExampleAdapter

from app.adapters.base import ExecutionResult
from app.models.schemas import FindingStatus, QueryType


@pytest.mark.asyncio
async def test_round_trip(tmp_path: Path) -> None:
    adapter = ExampleAdapter()
    query = "Example Organisation"
    assert (await adapter.healthcheck()).healthy
    assert not adapter.default_selected
    command = adapter.build_command(query, QueryType.NAME, tmp_path, 10)
    result = await adapter.execute(command, tmp_path, 10, query)
    assert result.returncode == 0
    findings = adapter.normalise_results(
        adapter.parse_results(result, tmp_path), query, QueryType.NAME, "raw/example.json"
    )
    assert len(findings) == 1
    assert findings[0].status == FindingStatus.LIKELY
    assert findings[0].confidence is None
    assert findings[0].source_tool == adapter.tool_id
    assert findings[0].source_record_id == "sample-001"
    assert findings[0].raw_output_reference == "raw/example.json"


@pytest.mark.asyncio
async def test_no_match(tmp_path: Path) -> None:
    adapter = ExampleAdapter()
    query = "Unknown example"
    result = await adapter.execute(
        adapter.build_command(query, QueryType.NAME, tmp_path, 10), tmp_path, 10, query
    )
    assert result.returncode == 0
    assert adapter.parse_results(result, tmp_path) == []


@pytest.mark.asyncio
async def test_reject_modified_command(tmp_path: Path) -> None:
    result = await ExampleAdapter().execute(["other"], tmp_path, 10, "Example")
    assert result.error_code == "invalid_command"


def test_invalid_type_and_proxy(tmp_path: Path) -> None:
    adapter = ExampleAdapter()
    with pytest.raises(ValueError):
        adapter.build_command("example", QueryType.EMAIL, tmp_path, 10)
    with pytest.raises(ValueError):
        adapter.build_command("example", QueryType.NAME, tmp_path, 10, "proxy")


def test_malformed_and_failed_output(tmp_path: Path) -> None:
    adapter = ExampleAdapter()
    (tmp_path / "example.json").write_text("{", encoding="utf-8")
    assert adapter.parse_results(ExecutionResult(0, "", ""), tmp_path) == []
    assert adapter.parse_results(ExecutionResult(1, "", ""), tmp_path) == []
    assert adapter.normalise_results(
        [{"name": "wrong", "id": "1"}], "Example", QueryType.NAME, None
    ) == []
