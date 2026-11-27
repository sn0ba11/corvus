from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.egress_policy import read_bounded_response, safe_client

ENDPOINT = "https://api.gleif.org/api/v1/lei-records"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_RESULTS = 20
USER_AGENT = "Corvus/0.1 GLEIF LEI collector"


def _text(value: object, limit: int = 1000) -> str | None:
    cleaned = " ".join(str(value or "").split())[:limit]
    return cleaned or None


def _name(value: object) -> str | None:
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("value"), 300)
    return _text(value, 300)


def parse_gleif_records(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("GLEIF returned an unexpected JSON:API shape")
    records: list[dict[str, Any]] = []
    for item in payload["data"][:MAX_RESULTS]:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes")
        if not isinstance(attrs, dict):
            continue
        entity = attrs.get("entity")
        registration = attrs.get("registration")
        if not isinstance(entity, dict):
            continue
        legal_name = _name(entity.get("legalName"))
        lei = _text(attrs.get("lei") or item.get("id"), 30)
        if not legal_name or not lei:
            continue
        address_raw = entity.get("legalAddress")
        address: dict[str, Any] = address_raw if isinstance(address_raw, dict) else {}
        registration_data: dict[str, Any] = registration if isinstance(registration, dict) else {}
        records.append({
            "lei": lei,
            "legal_name": legal_name,
            "status": _text(entity.get("status"), 50),
            "jurisdiction": _text(entity.get("jurisdiction"), 50),
            "category": _text(entity.get("category"), 80),
            "registered_as": _text(entity.get("registeredAs"), 200),
            "registration_authority_id": _text(entity.get("registeredAt"), 100),
            "address_lines": [str(line)[:300] for line in address.get("addressLines", [])[:8]] if isinstance(address.get("addressLines"), list) else [],
            "city": _text(address.get("city"), 120),
            "region": _text(address.get("region"), 120),
            "country": _text(address.get("country"), 20),
            "postal_code": _text(address.get("postalCode"), 40),
            "initial_registration_date": _text(registration_data.get("initialRegistrationDate"), 80),
            "last_update_date": _text(registration_data.get("lastUpdateDate"), 80),
            "registration_status": _text(registration_data.get("status"), 80),
            "gleif_url": f"https://lei.bloomberg.com/leis/view/{lei}",
        })
    return records


class GLEIFAdapter(Adapter):
    tool_id = "gleif"
    display_name = "GLEIF LEI Search"
    executable = "gleif-search"
    capabilities = frozenset({"company_search", "lei", "official_registry_api"})
    supported_query_types = frozenset({QueryType.NAME, QueryType.DOMAIN})
    default_selected = False
    default_timeout = 45
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "GLEIF API")

    def build_command(self, query: str, query_type: QueryType, output_dir: Path, timeout: int, proxy: str | None = None) -> list[str]:
        if query_type not in self.supported_query_types:
            raise ValueError("GLEIF supports name and domain investigations only")
        command = [self.executable, query, "--output", str(output_dir / "gleif.json"), "--timeout", str(timeout)]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    async def execute(self, command: list[str], output_dir: Path, timeout: int, query: str, log: LogCallback | None = None) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if len(command) < 6 or command[0] != self.executable or command[1] != query:
            return ExecutionResult(None, "", "", error_code="invalid_command", error_message="GLEIF command did not match the bounded adapter contract")
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        try:
            async with asyncio.timeout(timeout):
                client_context = httpx.AsyncClient(transport=self.transport, timeout=min(20.0, float(timeout)), follow_redirects=False, trust_env=False, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.api+json"}) if self.transport is not None else safe_client(timeout=min(20.0, float(timeout)), user_agent=USER_AGENT, proxy=proxy)
                async with client_context as client:
                    request = client.build_request("GET", ENDPOINT, params={"filter[entity.names]": query, "page[size]": str(MAX_RESULTS)})
                    response = await client.send(request, stream=True)
                    try:
                        if response.status_code == 429:
                            return ExecutionResult(None, "", "", rate_limited=True, error_code="rate_limited", error_message="GLEIF rate limited the request")
                        response.raise_for_status()
                        raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
                    finally:
                        await response.aclose()
            records = parse_gleif_records(json.loads(raw))
            payload = {"source": "GLEIF API", "query": query, "retrieved_at": datetime.now(UTC).isoformat(), "results": records}
            path = output_dir / "gleif.json"
            path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            path.chmod(0o600)
            return ExecutionResult(0, json.dumps({"status": "found" if records else "unknown", "results": len(records)}), "", output_files=[path])
        except TimeoutError:
            return ExecutionResult(None, "", "", timed_out=True, error_code="timeout", error_message=f"GLEIF search exceeded {timeout}s")
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            return ExecutionResult(None, "", "", error_code="upstream_error", error_message=f"GLEIF search failed: {exc}")

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "gleif.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return list(payload.get("results", [])) if isinstance(payload, dict) and isinstance(payload.get("results"), list) else []

    def normalise_results(self, records: list[dict[str, Any]], query: str, query_type: QueryType, raw_reference: str | None) -> list[FindingRecord]:
        findings: list[FindingRecord] = []
        for record in records[:MAX_RESULTS]:
            lei = str(record.get("lei") or "")
            name = str(record.get("legal_name") or "")
            if not lei or not name:
                continue
            location = ", ".join(str(record.get(key)) for key in ("city", "region", "country") if record.get(key)) or None
            findings.append(FindingRecord(query_type=query_type, queried_value=query, finding_type="legal_entity", platform="GLEIF", value=name, canonical_url=f"https://www.gleif.org/lei/{lei}", display_url=f"https://www.gleif.org/lei/{lei}", status=FindingStatus.LIKELY, confidence=0.72, title=name, description=location, metadata={key: value for key, value in record.items() if key not in {"legal_name", "gleif_url"}}, source_tool=self.tool_id, source_record_id=lei, raw_output_reference=raw_reference))
        return findings
