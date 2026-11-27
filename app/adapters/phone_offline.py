from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.phone_intelligence import PhoneNormalisationError, normalise_phone


class PhoneOfflineAdapter(Adapter):
    tool_id = "phone_offline"
    display_name = "Phone Number (offline)"
    executable = "corvus-phone-offline"
    capabilities = frozenset({"phone_normalisation", "offline_metadata"})
    supported_query_types = frozenset({QueryType.PHONE})
    default_timeout = 10
    concurrency_limit = 4

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "built-in offline numbering metadata")

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        if query_type != QueryType.PHONE:
            raise ValueError("offline phone normaliser accepts phone investigations only")
        metadata = normalise_phone(query)
        return [
            self.executable,
            "--number",
            metadata.e164,
            "--output",
            str(output_dir / "phone-offline.json"),
        ]

    async def execute(
        self,
        command: list[str],
        output_dir: Path,
        timeout: int,
        query: str,
        log: LogCallback | None = None,
    ) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            metadata = normalise_phone(query)
        except PhoneNormalisationError as exc:
            return ExecutionResult(
                2,
                "",
                str(exc),
                error_code="invalid_phone_number",
                error_message=str(exc),
            )
        expected = [
            self.executable,
            "--number",
            metadata.e164,
            "--output",
            str(output_dir / "phone-offline.json"),
        ]
        if command != expected:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="phone command did not match the bounded adapter contract",
            )
        output_path = output_dir / "phone-offline.json"
        record = metadata.as_dict()
        record["status"] = "found"
        output_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        output_path.chmod(0o600)
        if log:
            await log("stdout", f"Normalised {metadata.e164} using offline numbering metadata")
        return ExecutionResult(0, json.dumps(record), "", output_files=[output_path])

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "phone-offline.json"
        if result.returncode != 0 or not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [payload] if isinstance(payload, dict) else []

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        if query_type != QueryType.PHONE:
            return []
        findings: list[FindingRecord] = []
        for record in records[:1]:
            e164 = str(record.get("e164") or "").strip()
            if not e164.startswith("+"):
                continue
            region = str(record.get("numbering_region") or "").strip() or None
            line_type = str(record.get("line_type") or "").strip() or None
            description_parts = [part for part in (region, line_type) if part]
            findings.append(
                FindingRecord(
                    query_type=QueryType.PHONE,
                    queried_value=query,
                    finding_type="phone_number",
                    platform="Telephone numbering plan",
                    value=e164,
                    status=FindingStatus.FOUND,
                    confidence=1.0,
                    title=e164,
                    description=" · ".join(description_parts) or "Offline E.164 normalisation",
                    metadata={
                        key: value
                        for key, value in record.items()
                        if key not in {"status", "input_value", "e164"} and value not in (None, "")
                    },
                    source_tool=self.tool_id,
                    source_record_id=e164,
                    raw_output_reference=raw_reference,
                )
            )
        return findings
