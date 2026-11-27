from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.adapters.bing_name import _public_result_url
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.egress_policy import read_bounded_response, safe_client

API_BASE = "https://otx.alienvault.com/api/v1"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESULTS = 100
USER_AGENT = "Corvus/0.1 OTX historical URL discovery"


def _clean(value: Any, limit: int = 1000) -> str | None:
    if value in (None, ""):
        return None
    text = " ".join(str(value).split())[:limit]
    return text or None


def parse_url_list(payload: Any, domain: str) -> list[dict[str, Any]]:
    target = domain.casefold().strip().rstrip(".")
    values = payload.get("url_list") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values[: MAX_RESULTS * 2]:
        if len(records) >= MAX_RESULTS or not isinstance(item, dict):
            continue
        url = _public_result_url(str(item.get("url") or ""))
        if not url:
            continue
        host = (urlsplit(url).hostname or "").casefold().rstrip(".")
        if host != target and not host.endswith(f".{target}"):
            continue
        if url in seen:
            continue
        seen.add(url)
        records.append(
            {
                "url": url,
                "hostname": _clean(item.get("hostname"), 255),
                "httpcode": _clean(item.get("httpcode"), 20),
                "date": _clean(item.get("date"), 64),
                "result": item.get("result") if isinstance(item.get("result"), dict) else None,
            }
        )
    return records


class OTXHistoricalURLAdapter(Adapter):
    tool_id = "otx_historical"
    display_name = "AlienVault OTX Historical URLs"
    executable = "otx-historical-urls"
    capabilities = frozenset({"historical_url_discovery", "passive_threat_intelligence"})
    supported_query_types = frozenset({QueryType.DOMAIN})
    default_selected = False
    default_timeout = 45
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "AlienVault OTX public API")

    def build_command(self, query: str, query_type: QueryType, output_dir: Path, timeout: int, proxy: str | None = None) -> list[str]:
        if query_type != QueryType.DOMAIN:
            raise ValueError("OTX historical URL discovery accepts domain investigations only")
        command = [self.executable, query, "--output", str(output_dir / "otx-historical.json"), "--timeout", str(timeout)]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    async def execute(self, command: list[str], output_dir: Path, timeout: int, query: str, log: LogCallback | None = None) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if len(command) < 6 or command[0] != self.executable or command[1] != query:
            return ExecutionResult(None, "", "", error_code="invalid_command", error_message="OTX command did not match the bounded adapter contract")
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        endpoint = f"{API_BASE}/indicators/domain/{quote(query, safe='')}/url_list"
        try:
            async with asyncio.timeout(timeout):
                client_context = httpx.AsyncClient(transport=self.transport, timeout=min(20.0, float(timeout)), follow_redirects=False, trust_env=False, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) if self.transport is not None else safe_client(timeout=min(20.0, float(timeout)), user_agent=USER_AGENT, proxy=proxy)
                async with client_context as client:
                    request = client.build_request("GET", endpoint, params={"limit": str(MAX_RESULTS), "page": "1"})
                    response = await client.send(request, stream=True)
                    try:
                        if response.status_code == 429:
                            return ExecutionResult(None, "", "", rate_limited=True, error_code="rate_limited", error_message="AlienVault OTX rate limited the request")
                        response.raise_for_status()
                        raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
                    finally:
                        await response.aclose()
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("AlienVault OTX returned an unexpected JSON shape")
            records = parse_url_list(payload, query)
            stored = {
                "schema_version": "1.0",
                "source": "AlienVault OTX domain URL list",
                "queried_domain": query,
                "queried_at": datetime.now(UTC).isoformat(),
                "result_limit": MAX_RESULTS,
                "results": records,
                "source_scope": "passive_public_url_observations_no_target_fetch",
            }
            path = output_dir / "otx-historical.json"
            path.write_text(json.dumps(stored, indent=2, sort_keys=True), encoding="utf-8")
            path.chmod(0o600)
            if log:
                await log("stdout", f"OTX retained {len(records)} historical URL observation(s)")
            return ExecutionResult(0, json.dumps({"results": len(records)}), "", output_files=[path])
        except TimeoutError:
            return ExecutionResult(None, "", "", timed_out=True, error_code="timeout", error_message=f"OTX historical URL discovery exceeded {timeout}s")
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            return ExecutionResult(None, "", "", error_code="upstream_error", error_message=f"OTX historical URL discovery failed: {exc}")

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "otx-historical.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        values = payload.get("results") if isinstance(payload, dict) else None
        return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []

    def normalise_results(self, records: list[dict[str, Any]], query: str, query_type: QueryType, raw_reference: str | None) -> list[FindingRecord]:
        findings: list[FindingRecord] = []
        for record in records[:MAX_RESULTS]:
            url = _public_result_url(str(record.get("url") or ""))
            if not url:
                continue
            observed = _clean(record.get("date"), 64)
            metadata = dict(record)
            metadata["otx_observed_at"] = observed
            metadata["source_scope"] = "passive_public_url_observations_no_target_fetch"
            findings.append(FindingRecord(query_type=query_type, queried_value=query, finding_type="historical_url", platform="AlienVault OTX", value=url, display_url=url, status=FindingStatus.FOUND, confidence=0.75, title=urlsplit(url).hostname or query, description=f"Observed by AlienVault OTX{f' at {observed}' if observed else ''}", metadata=metadata, source_tool=self.tool_id, source_record_id=url, raw_output_reference=raw_reference))
        return findings
