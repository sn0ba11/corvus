from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.adapters.bing_name import (
    MAX_QUERY_COUNT,
    MAX_RESULTS_PER_QUERY,
    _public_result_url,
    build_bing_name_queries,
    name_result_is_relevant,
)
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.metasearch2_settings import DEFAULT_ENGINE_IDS, get_active_metasearch2_engines
from app.services.search_relevance import username_result_is_relevant

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
METASEARCH2_USAGE_SCOPE = "local_self_hosted_metasearch"


def build_metasearch2_username_queries(username: str) -> tuple[dict[str, str], ...]:
    value = username.strip().lstrip("@")
    quoted = f'"{value}"'
    queries = (
        {"id": "raw", "label": "Username across the public web", "query": value},
        {"id": "exact", "label": "Exact quoted username", "query": quoted},
        {
            "id": "social",
            "label": "Public social and community profiles",
            "query": f"{value} profile social forum",
        },
        {
            "id": "accounts",
            "label": "Account and member pages",
            "query": f"{value} user member account",
        },
        {
            "id": "documents",
            "label": "Documents and published records",
            "query": f"{value} pdf document record",
        },
    )
    return tuple({**item, "query": item["query"][:900]} for item in queries[:MAX_QUERY_COUNT])


def metasearch2_username_result_is_relevant(username: str, record: dict[str, Any]) -> bool:
    needle = username.strip().lstrip("@").casefold()
    if not needle:
        return False
    return username_result_is_relevant(
        needle, str(record.get("url") or ""), str(record.get("title") or ""),
        str(record.get("snippet") or ""),
    )


def _clean_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned[:limit] if cleaned else None


def _normalise_result(record: dict[str, Any]) -> dict[str, Any] | None:
    result = record.get("result")
    if not isinstance(result, dict):
        return None
    url = _public_result_url(str(result.get("url") or ""))
    if not url:
        return None
    parsed = urlsplit(url)
    if not parsed.hostname or parsed.username or parsed.password:
        return None
    engines = record.get("engines")
    if not isinstance(engines, list):
        engines = []
    score = record.get("score")
    return {
        "url": url,
        "title": _clean_text(result.get("title"), 500),
        "snippet": _clean_text(result.get("description"), 2000),
        "engines": sorted({str(item)[:80] for item in engines if item}),
        "score": score if isinstance(score, int | float) else None,
    }


def _flatten_results(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    flattened: list[dict[str, Any]] = []
    for response in payload[:20]:
        if not isinstance(response, dict):
            continue
        records = response.get("search_results")
        if not isinstance(records, list):
            continue
        flattened.extend(item for item in records if isinstance(item, dict))
    return flattened


class Metasearch2Adapter(Adapter):
    tool_id = "metasearch2"
    display_name = "Metasearch2 Web Search"
    executable = "metasearch2-search"
    capabilities = frozenset(
        {
            "name_search",
            "username_search",
            "self_hosted_metasearch",
            "json_output",
            "title_snippet_results",
        }
    )
    supported_query_types = frozenset({QueryType.NAME, QueryType.USERNAME})
    default_timeout = 120
    concurrency_limit = 1

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__()
        self.transport = transport
        self.base_url = (base_url or get_settings().metasearch2_url).rstrip("/")
        self.cancel_requested = False

    async def healthcheck(self) -> HealthResult:
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=8.0,
                follow_redirects=False,
            ) as client:
                response = await client.get(self.base_url)
                if response.status_code not in {200, 302, 307, 308}:
                    response.raise_for_status()
            return HealthResult(True, "ready", "metasearch2")
        except httpx.HTTPError as exc:
            return HealthResult(False, f"local metasearch2 health check failed: {exc}")

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        if query_type not in self.supported_query_types:
            raise ValueError("Metasearch2 accepts name or username investigations only")
        command = [
            self.executable,
            query,
            "--query-type",
            query_type.value,
            "--output",
            str(output_dir / "metasearch2.json"),
            "--timeout",
            str(timeout),
        ]
        if proxy:
            command.extend(["--proxy-note", "configured-on-workbench-not-metasearch2"])
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
        if (
            len(command) < 8
            or command[0] != self.executable
            or command[1] != query
            or command[2] != "--query-type"
            or command[4] != "--output"
            or command[6] != "--timeout"
        ):
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="Metasearch2 command did not match the bounded adapter contract",
            )
        try:
            query_type = QueryType(command[3])
        except ValueError:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="Metasearch2 received an invalid query type",
            )
        if query_type not in self.supported_query_types:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="Metasearch2 received an unsupported query type",
            )

        relevance_filter: Callable[[str, dict[str, Any]], bool]
        if query_type == QueryType.USERNAME:
            query_specs = build_metasearch2_username_queries(query)
            relevance_filter = metasearch2_username_result_is_relevant
        else:
            query_specs = build_bing_name_queries(query)[:MAX_QUERY_COUNT]
            relevance_filter = name_result_is_relevant

        query_records: list[dict[str, Any]] = []
        merged: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, str]] = []
        try:
            with SessionLocal() as db:
                active_engines = get_active_metasearch2_engines(db)
        except SQLAlchemyError:
            active_engines = DEFAULT_ENGINE_IDS
        active_engine_names = {engine.casefold() for engine in active_engines}
        rejected_irrelevant_count = 0
        started_at = datetime.now(UTC)
        timed_out = False

        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(
                    transport=self.transport,
                    timeout=min(30.0, max(10.0, timeout / max(1, len(query_specs)))),
                    follow_redirects=False,
                    headers={"Accept": "application/json", "User-Agent": "Corvus/0.1"},
                ) as client:
                    for index, spec in enumerate(query_specs):
                        if self.cancel_requested:
                            return ExecutionResult(
                                None,
                                json.dumps({"queries_completed": index, "cancelled": True}),
                                "",
                                cancelled=True,
                                error_code="cancelled",
                                error_message="Metasearch2 was cancelled",
                            )
                        try:
                            response = await client.get(
                                f"{self.base_url}/search",
                                params={
                                    "q": spec["query"],
                                    "engines": ",".join(active_engines),
                                },
                            )
                            response.raise_for_status()
                            if len(response.content) > MAX_RESPONSE_BYTES:
                                errors.append(
                                    {
                                        "query_id": spec["id"],
                                        "error": "Metasearch2 JSON response exceeded the 2 MiB limit",
                                    }
                                )
                                continue
                            data = response.json()
                        except (httpx.HTTPError, ValueError) as exc:
                            errors.append({"query_id": spec["id"], "error": str(exc)[:500]})
                            continue

                        raw_path = output_dir / f"metasearch2-{spec['id']}.json"
                        raw_path.write_text(
                            json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        raw_path.chmod(0o600)

                        normalised: list[dict[str, Any]] = []
                        for raw_record in _flatten_results(data)[: MAX_RESULTS_PER_QUERY * 4]:
                            item = _normalise_result(raw_record)
                            if item is None:
                                continue
                            selected_engines = [
                                engine
                                for engine in item.get("engines", [])
                                if str(engine).casefold() in active_engine_names
                            ]
                            if not selected_engines:
                                continue
                            item["engines"] = selected_engines
                            normalised.append(item)
                        relevant = [
                            record for record in normalised if relevance_filter(query, record)
                        ][:MAX_RESULTS_PER_QUERY]
                        rejected_irrelevant_count += len(normalised) - len(relevant)
                        query_records.append(
                            {
                                **spec,
                                "returned_count": len(normalised),
                                "retained_count": len(relevant),
                                "raw_json": raw_path.name,
                                "requested_engines": list(active_engines),
                            }
                        )
                        for record in relevant:
                            url = str(record["url"])
                            query_match = {
                                "query_id": spec["id"],
                                "query_label": spec["label"],
                                "query": spec["query"],
                                "raw_json": raw_path.name,
                            }
                            existing = merged.get(url)
                            if existing is None:
                                merged[url] = {**record, "query_matches": [query_match]}
                            else:
                                existing["query_matches"].append(query_match)
                                existing["engines"] = sorted(
                                    set(existing.get("engines", []))
                                    | set(record.get("engines", []))
                                )
                                if not existing.get("title") and record.get("title"):
                                    existing["title"] = record["title"]
                                if not existing.get("snippet") and record.get("snippet"):
                                    existing["snippet"] = record["snippet"]
                        if log:
                            await log(
                                "stdout",
                                f"Metasearch2 {query_type.value} query {index + 1}/{len(query_specs)} retained "
                                f"{len(relevant)}/{len(normalised)} result(s): {spec['label']}",
                            )
                        if index + 1 < len(query_specs):
                            await asyncio.sleep(0.4)
        except TimeoutError:
            timed_out = True

        payload = {
            "schema_version": 1,
            "tool": self.tool_id,
            "engine": "metasearch2",
            "endpoint": self.base_url,
            "usage_scope": METASEARCH2_USAGE_SCOPE,
            "query": query,
            "query_type": query_type.value,
            "requested_engines": list(active_engines),
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "queries": query_records,
            "errors": errors,
            "rejected_irrelevant_count": rejected_irrelevant_count,
            "results": list(merged.values()),
        }
        output_path = output_dir / "metasearch2.json"
        output_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        output_path.chmod(0o600)
        stdout = json.dumps(
            {
                "queries_completed": len(query_records),
                "results": len(merged),
                "errors": len(errors),
                "rejected_irrelevant": rejected_irrelevant_count,
            },
            sort_keys=True,
        )
        stderr = "\n".join(item["error"] for item in errors)
        if timed_out:
            return ExecutionResult(
                None,
                stdout,
                "\n".join(part for part in (stderr, "Metasearch2 exceeded its bounded timeout") if part),
                output_files=[*output_dir.glob("*")],
                timed_out=True,
                error_code="timeout",
                error_message=f"tool exceeded {timeout}s",
            )
        if not query_records and errors:
            return ExecutionResult(
                None,
                stdout,
                stderr,
                output_files=[*output_dir.glob("*")],
                error_code="upstream_error",
                error_message=errors[0]["error"],
            )
        return ExecutionResult(
            0,
            stdout,
            stderr,
            output_files=[*output_dir.glob("*")],
        )

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        output = output_dir / "metasearch2.json"
        if not output.exists():
            return []
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        records = payload.get("results", []) if isinstance(payload, dict) else []
        return [item for item in records if isinstance(item, dict)] if isinstance(records, list) else []

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        findings: list[FindingRecord] = []
        for record in records:
            url = str(record.get("url") or "")
            if not url:
                continue
            engines = [str(item) for item in record.get("engines", []) if item]
            score = record.get("score")
            metadata = {
                "usage_scope": METASEARCH2_USAGE_SCOPE,
                "search_engines": engines,
                "score": score,
                "query_matches": record.get("query_matches", []),
                "snippet": record.get("snippet"),
            }
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="search_result",
                    platform="Web search",
                    value=url,
                    display_url=url,
                    status=FindingStatus.LIKELY,
                    confidence=0.72 if engines else 0.65,
                    title=str(record.get("title") or url)[:500],
                    metadata=metadata,
                    source_tool=self.tool_id,
                    source_record_id=url,
                    raw_output_reference=raw_reference,
                    canonical_url=url,
                )
            )
        return findings
