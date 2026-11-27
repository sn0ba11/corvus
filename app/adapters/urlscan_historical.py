from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.adapters.bing_name import _public_result_url
from app.db.session import SessionLocal
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.egress_policy import read_bounded_response, safe_client
from app.services.urlscan_settings import get_urlscan_api_key

SEARCH_ENDPOINT = "https://urlscan.io/api/v1/search/"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESULTS = 100
USER_AGENT = "Corvus/0.1 URLScan historical discovery"


def _clean(value: Any, limit: int = 1000) -> str | None:
    if value in (None, ""):
        return None
    text = " ".join(str(value).split())[:limit]
    return text or None


def parse_search_results(payload: Any, domain: str) -> list[dict[str, Any]]:
    target = domain.casefold().strip().rstrip(".")
    values = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values[: MAX_RESULTS * 2]:
        if len(records) >= MAX_RESULTS or not isinstance(item, dict):
            continue
        task_value = item.get("task")
        page_value = item.get("page")
        task: dict[str, Any] = task_value if isinstance(task_value, dict) else {}
        page: dict[str, Any] = page_value if isinstance(page_value, dict) else {}
        raw_url = page.get("url") or task.get("url")
        url = _public_result_url(str(raw_url or ""))
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
                "scan_id": _clean(item.get("_id") or task.get("uuid"), 128),
                "observed_at": _clean(task.get("time"), 64),
                "task_domain": _clean(task.get("domain"), 255),
                "page_domain": _clean(page.get("domain"), 255),
                "page_ip": _clean(page.get("ip"), 128),
                "page_status": _clean(page.get("status"), 32),
                "page_title": _clean(page.get("title"), 500),
                "country": _clean(page.get("country"), 16),
                "server": _clean(page.get("server"), 200),
            }
        )
    return records


class URLScanHistoricalAdapter(Adapter):
    tool_id = "urlscan_historical"
    display_name = "URLScan Historical URLs"
    executable = "urlscan-historical"
    capabilities = frozenset({"historical_url_discovery", "infrastructure_enrichment"})
    supported_query_types = frozenset({QueryType.DOMAIN})
    default_selected = False
    default_timeout = 45
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        with SessionLocal() as db:
            configured = get_urlscan_api_key(db) is not None
        if not configured:
            return HealthResult(False, "URLScan API key is not configured", None)
        return HealthResult(True, "ready", "URLScan Search API")

    def build_command(self, query: str, query_type: QueryType, output_dir: Path, timeout: int, proxy: str | None = None) -> list[str]:
        if query_type != QueryType.DOMAIN:
            raise ValueError("URLScan historical discovery accepts domain investigations only")
        command = [self.executable, query, "--output", str(output_dir / "urlscan-historical.json"), "--timeout", str(timeout)]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    async def execute(self, command: list[str], output_dir: Path, timeout: int, query: str, log: LogCallback | None = None) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if len(command) < 6 or command[0] != self.executable or command[1] != query:
            return ExecutionResult(None, "", "", error_code="invalid_command", error_message="URLScan command did not match the bounded adapter contract")
        with SessionLocal() as db:
            api_key = get_urlscan_api_key(db)
        if not api_key:
            return ExecutionResult(None, "", "", error_code="not_configured", error_message="URLScan API key is not configured")
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        try:
            async with asyncio.timeout(timeout):
                headers = {"Accept": "application/json", "api-key": api_key}
                client_context = httpx.AsyncClient(transport=self.transport, timeout=min(20.0, float(timeout)), follow_redirects=False, trust_env=False, headers={**headers, "User-Agent": USER_AGENT}) if self.transport is not None else safe_client(timeout=min(20.0, float(timeout)), user_agent=USER_AGENT, proxy=proxy)
                async with client_context as client:
                    request = client.build_request("GET", SEARCH_ENDPOINT, params={"q": f"domain:{query}", "size": str(MAX_RESULTS)}, headers=headers)
                    response = await client.send(request, stream=True)
                    try:
                        if response.status_code == 429:
                            return ExecutionResult(None, "", "", rate_limited=True, error_code="rate_limited", error_message="URLScan rate limited the search request")
                        if response.status_code in {401, 403}:
                            return ExecutionResult(None, "", "", error_code="authentication_failed", error_message="URLScan rejected the configured API key")
                        response.raise_for_status()
                        raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
                    finally:
                        await response.aclose()
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("URLScan returned an unexpected JSON shape")
            records = parse_search_results(payload, query)
            stored = {
                "schema_version": "1.0",
                "source": "URLScan Search API",
                "queried_domain": query,
                "result_limit": MAX_RESULTS,
                "results": records,
                "source_scope": "historical_scan_metadata_no_new_scan_submission",
            }
            path = output_dir / "urlscan-historical.json"
            path.write_text(json.dumps(stored, indent=2, sort_keys=True), encoding="utf-8")
            path.chmod(0o600)
            if log:
                await log("stdout", f"URLScan retained {len(records)} historical URL observation(s)")
            return ExecutionResult(0, json.dumps({"results": len(records)}), "", output_files=[path])
        except TimeoutError:
            return ExecutionResult(None, "", "", timed_out=True, error_code="timeout", error_message=f"URLScan historical discovery exceeded {timeout}s")
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            return ExecutionResult(None, "", "", error_code="upstream_error", error_message=f"URLScan historical discovery failed: {exc}")

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "urlscan-historical.json"
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
            observed = _clean(record.get("observed_at"), 64)
            metadata = dict(record)
            metadata["source_scope"] = "historical_scan_metadata_no_new_scan_submission"
            findings.append(FindingRecord(query_type=query_type, queried_value=query, finding_type="historical_url", platform="URLScan", value=url, display_url=url, status=FindingStatus.FOUND, confidence=0.85, title=record.get("page_title") or urlsplit(url).hostname or query, description=f"Observed in a URLScan historical scan{f' at {observed}' if observed else ''}", metadata=metadata, source_tool=self.tool_id, source_record_id=str(record.get("scan_id") or url), raw_output_reference=raw_reference))
        return findings
