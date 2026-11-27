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

ENDPOINT = "https://api.duckduckgo.com/"
MAX_RESPONSE_BYTES = 512 * 1024
MAX_RELATED = 12
USER_AGENT = "Corvus/0.1 DuckDuckGo Instant Answer enrichment"


def _text(value: object, limit: int = 1000) -> str | None:
    cleaned = " ".join(str(value or "").split())[:limit]
    return cleaned or None


def _related(payload: object) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    if not isinstance(payload, list):
        return output
    for item in payload:
        if len(output) >= MAX_RELATED:
            break
        if isinstance(item, dict) and isinstance(item.get("Topics"), list):
            for nested in item["Topics"]:
                if len(output) >= MAX_RELATED:
                    break
                if not isinstance(nested, dict):
                    continue
                text = _text(nested.get("Text"), 500)
                url = _text(nested.get("FirstURL"), 1000)
                if text and url and url.startswith("https://"):
                    output.append({"text": text, "url": url})
            continue
        if not isinstance(item, dict):
            continue
        text = _text(item.get("Text"), 500)
        url = _text(item.get("FirstURL"), 1000)
        if text and url and url.startswith("https://"):
            output.append({"text": text, "url": url})
    return output


class DuckDuckGoInstantAdapter(Adapter):
    tool_id = "duckduckgo_instant"
    display_name = "DuckDuckGo Instant Answer"
    executable = "duckduckgo-instant"
    capabilities = frozenset({"instant_answer", "public_reference_enrichment"})
    supported_query_types = frozenset({QueryType.NAME, QueryType.DOMAIN})
    default_selected = False
    default_timeout = 30
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "DuckDuckGo Instant Answer API")

    def build_command(self, query: str, query_type: QueryType, output_dir: Path, timeout: int, proxy: str | None = None) -> list[str]:
        if query_type not in self.supported_query_types:
            raise ValueError("DuckDuckGo Instant Answer supports name and domain investigations only")
        command = [self.executable, query, "--output", str(output_dir / "duckduckgo-instant.json"), "--timeout", str(timeout)]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    async def execute(self, command: list[str], output_dir: Path, timeout: int, query: str, log: LogCallback | None = None) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if len(command) < 6 or command[0] != self.executable or command[1] != query:
            return ExecutionResult(None, "", "", error_code="invalid_command", error_message="DuckDuckGo command did not match the bounded adapter contract")
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        try:
            async with asyncio.timeout(timeout):
                client_context = httpx.AsyncClient(transport=self.transport, timeout=min(15.0, float(timeout)), follow_redirects=False, trust_env=False, headers={"User-Agent": USER_AGENT}) if self.transport is not None else safe_client(timeout=min(15.0, float(timeout)), user_agent=USER_AGENT, proxy=proxy)
                async with client_context as client:
                    request = client.build_request("GET", ENDPOINT, params={"q": query, "format": "json", "no_html": "1", "no_redirect": "1", "skip_disambig": "0"})
                    response = await client.send(request, stream=True)
                    try:
                        if response.status_code == 429:
                            return ExecutionResult(None, "", "", rate_limited=True, error_code="rate_limited", error_message="DuckDuckGo rate limited the request")
                        response.raise_for_status()
                        raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
                    finally:
                        await response.aclose()
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("DuckDuckGo returned an unexpected JSON shape")
            record = {
                "status": "found" if any(payload.get(key) for key in ("AbstractText", "Heading", "RelatedTopics")) else "unknown",
                "query": query,
                "heading": _text(payload.get("Heading"), 300),
                "abstract": _text(payload.get("AbstractText"), 3000),
                "abstract_url": _text(payload.get("AbstractURL"), 1000),
                "abstract_source": _text(payload.get("AbstractSource"), 200),
                "answer": _text(payload.get("Answer"), 1000),
                "definition": _text(payload.get("Definition"), 2000),
                "definition_url": _text(payload.get("DefinitionURL"), 1000),
                "related_topics": _related(payload.get("RelatedTopics")),
                "retrieved_at": datetime.now(UTC).isoformat(),
                "source_scope": "duckduckgo_instant_answer_api",
            }
            path = output_dir / "duckduckgo-instant.json"
            path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
            path.chmod(0o600)
            return ExecutionResult(0, json.dumps({"status": record["status"]}), "", output_files=[path])
        except TimeoutError:
            return ExecutionResult(None, "", "", timed_out=True, error_code="timeout", error_message=f"DuckDuckGo enrichment exceeded {timeout}s")
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            return ExecutionResult(None, "", "", error_code="upstream_error", error_message=f"DuckDuckGo enrichment failed: {exc}")

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "duckduckgo-instant.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [payload] if isinstance(payload, dict) else []

    def normalise_results(self, records: list[dict[str, Any]], query: str, query_type: QueryType, raw_reference: str | None) -> list[FindingRecord]:
        if not records:
            return []
        record = records[0]
        if record.get("status") != "found":
            return []
        canonical = str(record.get("abstract_url") or record.get("definition_url") or "").strip() or None
        return [FindingRecord(query_type=query_type, queried_value=query, finding_type="instant_answer", platform="DuckDuckGo", value=str(record.get("heading") or query), canonical_url=canonical, display_url=canonical, status=FindingStatus.LIKELY, confidence=0.55, title=record.get("heading"), description=record.get("abstract") or record.get("definition") or record.get("answer"), metadata={"abstract_source": record.get("abstract_source"), "related_topics": record.get("related_topics", []), "source_scope": record.get("source_scope"), "retrieved_at": record.get("retrieved_at")}, source_tool=self.tool_id, raw_output_reference=raw_reference)]
