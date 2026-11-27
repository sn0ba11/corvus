"""Offline example: a synthetic reference catalogue, not a production collector."""

import json
from pathlib import Path
from typing import Any

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.models.schemas import FindingRecord, FindingStatus, QueryType

FIXTURE = Path(__file__).parent / "fixtures" / "sample_response.json"
MAX_BYTES = 16_384


class ExampleAdapter(Adapter):
    tool_id = "example_catalogue"
    display_name = "Example Catalogue (offline)"
    executable = "example-catalogue"
    capabilities = frozenset({"offline_reference"})
    supported_query_types = frozenset({QueryType.NAME})
    default_selected = False
    default_timeout = 10

    async def healthcheck(self) -> HealthResult:
        return HealthResult(FIXTURE.is_file(), "local example fixture", "1")

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        if query_type not in self.supported_query_types:
            raise ValueError("name investigations only")
        if not query.strip() or len(query) > 320 or any(ord(c) < 32 for c in query):
            raise ValueError("invalid query")
        if proxy:
            raise ValueError("offline example does not accept a proxy")
        return [self.executable, query, str(output_dir / "example.json")]

    async def execute(
        self,
        command: list[str],
        output_dir: Path,
        timeout: int,
        query: str,
        log: LogCallback | None = None,
    ) -> ExecutionResult:
        if command != self.build_command(query, QueryType.NAME, output_dir, timeout):
            return ExecutionResult(None, "", "", error_code="invalid_command")
        try:
            with FIXTURE.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("fixture too large")
            payload = json.loads(raw)
            if not isinstance(payload, list):
                raise ValueError("expected list")
            matches = [
                record for record in payload[:50]
                if isinstance(record, dict)
                and isinstance(record.get("name"), str)
                and record["name"].casefold() == query.casefold()
            ]
            output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = output_dir / "example.json"
            path.write_text(json.dumps(matches), encoding="utf-8")
            path.chmod(0o600)
            return ExecutionResult(0, "", "", output_files=[path])
        except (OSError, ValueError):
            return ExecutionResult(None, "", "", error_code="fixture_error")

    def parse_results(
        self, result: ExecutionResult, output_dir: Path
    ) -> list[dict[str, Any]]:
        if result.returncode != 0:
            return []
        try:
            with (output_dir / "example.json").open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return []
            payload = json.loads(raw)
        except (OSError, ValueError):
            return []
        if not isinstance(payload, list):
            return []
        return [row for row in payload[:50] if isinstance(row, dict)]

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        if query_type not in self.supported_query_types:
            return []
        findings = []
        for record in records[:50]:
            name, record_id = record.get("name"), record.get("id")
            if not isinstance(name, str) or name.casefold() != query.casefold():
                continue
            if not isinstance(record_id, str) or not record_id:
                continue
            findings.append(FindingRecord(
                query_type=query_type,
                queried_value=query,
                finding_type="example_reference",
                platform="Synthetic catalogue",
                value=name,
                status=FindingStatus.LIKELY,
                confidence=None,
                metadata={"synthetic": True},
                source_tool=self.tool_id,
                source_record_id=record_id,
                raw_output_reference=raw_reference,
            ))
        return findings
