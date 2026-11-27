from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

ENDPOINT = "https://opendata.cro.ie/api/3/action/datastore_search"
RESOURCE_ID = "3fef41bc-b8f4-4b10-8434-ce51c29b1bba"
MAX_RESULTS = 20
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
USER_AGENT = "Corvus/0.1 CRO Open Data collector"


def _clean(value: Any, limit: int = 1000) -> str | None:
    if value in (None, ""):
        return None
    text = " ".join(str(value).split())[:limit]
    return text or None


def _normalised_name(value: str) -> str:
    return " ".join(value.casefold().replace("&", " and ").split())


class CROOpenDataAdapter(Adapter):
    tool_id = "cro_open_data"
    display_name = "Irish CRO Open Data"
    executable = "cro-open-data"
    capabilities = frozenset({"company_search", "official_registry_api", "ireland"})
    supported_query_types = frozenset({QueryType.NAME})
    default_selected = False
    default_timeout = 30
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "CRO CKAN Data API")

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        if query_type != QueryType.NAME:
            raise ValueError("CRO Open Data search accepts name investigations only")
        target = " ".join(query.split())
        if len(target) < 2 or len(target) > 200:
            raise ValueError("invalid CRO company search")
        command = [self.executable, "--query", target, "--output", str(output_dir / "cro-open-data.json")]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    async def execute(
        self,
        command: list[str],
        output_dir: Path,
        timeout: int,
        query: str,
        log: LogCallback | None = None,
    ) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        expected_prefix = [self.executable, "--query", " ".join(query.split()), "--output", str(output_dir / "cro-open-data.json")]
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        expected = [*expected_prefix, "--proxy", proxy] if proxy else expected_prefix
        if command != expected:
            return ExecutionResult(None, "", "", error_code="invalid_command", error_message="CRO command did not match the bounded adapter contract")
        request_timeout = min(20.0, max(5.0, float(timeout)))
        client_context = (
            httpx.AsyncClient(transport=self.transport, timeout=request_timeout, follow_redirects=False, trust_env=False, headers={"User-Agent": USER_AGENT})
            if self.transport is not None
            else safe_client(timeout=request_timeout, user_agent=USER_AGENT, proxy=proxy)
        )
        try:
            async with asyncio.timeout(timeout):
                async with client_context as client:
                    request = client.build_request(
                        "GET",
                        ENDPOINT,
                        params={"resource_id": RESOURCE_ID, "limit": str(MAX_RESULTS), "q": " ".join(query.split())},
                        headers={"Accept": "application/json"},
                    )
                    response = await client.send(request, stream=True)
                    try:
                        if response.status_code == 429:
                            return ExecutionResult(None, "", "", rate_limited=True, error_code="rate_limited", error_message="CRO Open Data rate limited the request")
                        response.raise_for_status()
                        raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
                    finally:
                        await response.aclose()
            payload = json.loads(raw)
            result = payload.get("result") if isinstance(payload, dict) else None
            records = result.get("records") if isinstance(result, dict) else None
            if not isinstance(records, list):
                raise ValueError("CRO Open Data returned an unexpected response")
            retained = [item for item in records[:MAX_RESULTS] if isinstance(item, dict)]
            artifact = {
                "source": "CRO Open Data CKAN Data API",
                "source_resource_id": RESOURCE_ID,
                "source_url": ENDPOINT,
                "licence": "CC-BY-4.0",
                "query": " ".join(query.split()),
                "retrieved_at": datetime.now(UTC).isoformat(),
                "total_matches": result.get("total") if isinstance(result, dict) else None,
                "records": retained,
            }
            path = output_dir / "cro-open-data.json"
            path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
            path.chmod(0o600)
            if log:
                await log("stdout", f"CRO Open Data retained {len(retained)} company record(s)")
            return ExecutionResult(0, json.dumps({"status": "found" if retained else "not_found", "results": len(retained)}), "", output_files=[path])
        except TimeoutError:
            return ExecutionResult(None, "", "", timed_out=True, error_code="timeout", error_message=f"CRO Open Data search exceeded {timeout}s")
        except (httpx.HTTPError, EgressPolicyError, ValueError, json.JSONDecodeError) as exc:
            return ExecutionResult(None, "", "", error_code="upstream_error", error_message=f"CRO Open Data search failed: {exc}")

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "cro-open-data.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        records = payload.get("records") if isinstance(payload, dict) else None
        return [item for item in records[:MAX_RESULTS] if isinstance(item, dict)] if isinstance(records, list) else []

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        findings: list[FindingRecord] = []
        target = _normalised_name(query)
        for record in records[:MAX_RESULTS]:
            company_name = _clean(record.get("company_name"), 300)
            company_num = _clean(record.get("company_num"), 40)
            if not company_name or not company_num:
                continue
            exact = _normalised_name(company_name) == target
            address_parts = [
                _clean(record.get(key), 200)
                for key in ("company_address_1", "company_address_2", "company_address_3", "company_address_4", "eircode")
            ]
            address = ", ".join(part for part in address_parts if part) or None
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="legal_entity",
                    platform="Irish CRO",
                    value=company_name,
                    canonical_url="https://core.cro.ie/",
                    display_url="https://core.cro.ie/",
                    status=FindingStatus.FOUND if exact else FindingStatus.LIKELY,
                    confidence=0.95 if exact else 0.72,
                    title=company_name,
                    description=address,
                    metadata={
                        "company_number": company_num,
                        "company_status": _clean(record.get("company_status"), 100),
                        "company_type": _clean(record.get("company_type"), 200),
                        "registration_date": record.get("company_reg_date"),
                        "last_annual_return_date": record.get("last_ar_date"),
                        "next_annual_return_date": record.get("nard"),
                        "last_accounts_date": record.get("last_accounts_date"),
                        "dissolved_date": record.get("comp_dissolved_date"),
                        "nace_v2_code": record.get("nace_v2_code"),
                        "principal_object_code": record.get("princ_object_code"),
                        "address": address,
                        "source_scope": "cro_daily_open_data_snapshot",
                        "licence": "CC-BY-4.0",
                    },
                    source_tool=self.tool_id,
                    source_record_id=f"cro:{company_num}",
                    raw_output_reference=raw_reference,
                )
            )
        return findings
