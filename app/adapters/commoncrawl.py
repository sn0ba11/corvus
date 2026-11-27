from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.adapters.bing_name import _public_result_url
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.egress_policy import read_bounded_response, safe_client

COLLECTIONS_ENDPOINT = "https://index.commoncrawl.org/collinfo.json"
MAX_COLLECTION_RESPONSE_BYTES = 256 * 1024
MAX_INDEX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESULTS = 100


def _latest_collection(payload: Any) -> dict[str, str]:
    if not isinstance(payload, list):
        raise ValueError("Common Crawl collection response was not a list")
    candidates: list[dict[str, str]] = []
    for item in payload[:100]:
        if not isinstance(item, dict):
            continue
        collection_id = str(item.get("id") or "").strip()
        api_url = str(item.get("cdx-api") or "").strip()
        if not collection_id.startswith("CC-MAIN-"):
            continue
        parsed = urlsplit(api_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "index.commoncrawl.org"
            or parsed.username
            or parsed.password
        ):
            continue
        candidates.append({"id": collection_id[:100], "api_url": api_url})
    if not candidates:
        raise ValueError("Common Crawl did not advertise a supported official index")
    return max(candidates, key=lambda item: item["id"])


def parse_index_jsonl(text: str, domain: str) -> list[dict[str, Any]]:
    target = domain.casefold().strip().rstrip(".")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in text.splitlines()[: MAX_RESULTS * 3]:
        if len(records) >= MAX_RESULTS or not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
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
                "timestamp": str(item.get("timestamp") or "")[:32] or None,
                "status": str(item.get("status") or "")[:20] or None,
                "mime": str(item.get("mime") or item.get("mime-detected") or "")[:200] or None,
                "digest": str(item.get("digest") or "")[:200] or None,
                "filename": str(item.get("filename") or "")[:500] or None,
                "offset": str(item.get("offset") or "")[:50] or None,
                "length": str(item.get("length") or "")[:50] or None,
            }
        )
    return records


class CommonCrawlAdapter(Adapter):
    tool_id = "commoncrawl"
    display_name = "Common Crawl"
    executable = "commoncrawl-index"
    capabilities = frozenset({"historical_url_discovery", "official_index_api", "jsonl_output"})
    supported_query_types = frozenset({QueryType.DOMAIN})
    default_timeout = 90
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport
        self.cancel_requested = False

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "official index API")

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        if query_type != QueryType.DOMAIN:
            raise ValueError("Common Crawl accepts domain investigations only")
        command = [
            self.executable,
            query,
            "--output",
            str(output_dir / "commoncrawl-index.json"),
            "--timeout",
            str(timeout),
        ]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    async def cancel(self) -> None:
        self.cancel_requested = True
        await super().cancel()

    async def execute(
        self,
        command: list[str],
        output_dir: Path,
        timeout: int,
        query: str,
        log: LogCallback | None = None,
    ) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if len(command) < 6 or command[0] != self.executable or command[1] != query:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="Common Crawl command did not match the bounded adapter contract",
            )
        self.cancel_requested = False
        started_at = datetime.now(UTC)
        try:
            async with asyncio.timeout(timeout):
                request_timeout = min(30.0, max(5.0, timeout / 2))
                client_context = (
                    httpx.AsyncClient(
                        transport=self.transport,
                        timeout=request_timeout,
                        follow_redirects=False,
                        headers={"Accept": "application/json", "User-Agent": "OSINT-Workbench/0.1"},
                    )
                    if self.transport is not None
                    else safe_client(timeout=request_timeout, user_agent="OSINT-Workbench/0.1")
                )
                async with client_context as client:
                    collections_request = client.build_request("GET", COLLECTIONS_ENDPOINT)
                    collections = await client.send(collections_request, stream=True)
                    try:
                        collection_content = await read_bounded_response(
                            collections, max_bytes=MAX_COLLECTION_RESPONSE_BYTES
                        )
                    finally:
                        await collections.aclose()
                    collections.raise_for_status()
                    collection = _latest_collection(json.loads(collection_content))
                    if self.cancel_requested:
                        return ExecutionResult(
                            None,
                            "",
                            "",
                            cancelled=True,
                            error_code="cancelled",
                            error_message="Common Crawl was cancelled",
                        )
                    if log:
                        await log("stdout", f"Querying Common Crawl index {collection['id']}")
                    index_request = client.build_request(
                        "GET",
                        collection["api_url"],
                        params={
                            "url": f"*.{query}/*",
                            "output": "json",
                            "filter": "status:200",
                            "collapse": "urlkey",
                            "pageSize": str(MAX_RESULTS),
                        },
                    )
                    response = await client.send(index_request, stream=True)
                    try:
                        index_content = await read_bounded_response(
                            response, max_bytes=MAX_INDEX_RESPONSE_BYTES
                        )
                    finally:
                        await response.aclose()
                    if response.status_code == 429:
                        return ExecutionResult(
                            None,
                            "",
                            "Common Crawl returned HTTP 429",
                            rate_limited=True,
                            error_code="rate_limited",
                            error_message="Common Crawl temporarily rate limited the index request",
                        )
                    response.raise_for_status()
                    raw_path = output_dir / "commoncrawl-index.jsonl"
                    raw_path.write_bytes(index_content)
                    raw_path.chmod(0o600)
                    records = parse_index_jsonl(
                        index_content.decode(response.encoding or "utf-8", errors="replace"), query
                    )
                    payload = {
                        "schema_version": "1.0",
                        "source": "Common Crawl index API",
                        "collection_id": collection["id"],
                        "api_url": collection["api_url"],
                        "queried_domain": query,
                        "queried_at": started_at.isoformat(),
                        "result_limit": MAX_RESULTS,
                        "results": records,
                        "raw_jsonl": raw_path.name,
                    }
                    result_path = output_dir / "commoncrawl-index.json"
                    result_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                    result_path.chmod(0o600)
                    if log:
                        await log("stdout", f"Common Crawl retained {len(records)} historical URL record(s)")
                    return ExecutionResult(
                        0,
                        json.dumps(
                            {"collection_id": collection["id"], "results": len(records)},
                            sort_keys=True,
                        ),
                        "",
                        output_files=[raw_path, result_path],
                    )
        except TimeoutError:
            return ExecutionResult(
                None,
                "",
                "",
                timed_out=True,
                error_code="timeout",
                error_message=f"Common Crawl exceeded {timeout}s",
            )
        except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return ExecutionResult(
                None,
                "",
                str(exc),
                error_code="upstream_error",
                error_message=f"Common Crawl index request failed: {exc}",
            )

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "commoncrawl-index.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        values = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            return []
        collection_id = str(payload.get("collection_id") or "")[:100]
        return [
            {**record, "collection_id": collection_id}
            for record in values
            if isinstance(record, dict)
        ]

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        findings: list[FindingRecord] = []
        for record in records[:MAX_RESULTS]:
            url = _public_result_url(str(record.get("url") or ""))
            if not url:
                continue
            host = urlsplit(url).hostname or query
            timestamp = str(record.get("timestamp") or "")
            observed = timestamp
            if len(timestamp) == 14 and timestamp.isdigit():
                observed = (
                    f"{timestamp[0:4]}-{timestamp[4:6]}-{timestamp[6:8]}T"
                    f"{timestamp[8:10]}:{timestamp[10:12]}:{timestamp[12:14]}Z"
                )
            metadata = dict(record)
            metadata["crawl_observed_at"] = observed or None
            metadata["source_scope"] = "index_metadata_only_no_page_download"
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="historical_url",
                    platform="Common Crawl",
                    value=url,
                    display_url=url,
                    status=FindingStatus.FOUND,
                    confidence=0.85,
                    title=host,
                    description=(f"Indexed by {record.get('collection_id')} at {observed}" if observed else "Indexed by Common Crawl"),
                    metadata=metadata,
                    source_tool=self.tool_id,
                    source_record_id=f"{record.get('collection_id')}:{record.get('digest') or url}",
                    raw_output_reference=raw_reference,
                )
            )
        return findings
