from __future__ import annotations

import asyncio
import hashlib
import html
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.adapters.bing_name import (
    MAX_QUERY_COUNT,
    MAX_RESULTS_PER_QUERY,
    _public_result_url,
    _quoted,
    build_bing_name_queries,
    name_result_is_relevant,
)
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.search_relevance import (
    username_result_is_relevant as shared_username_result_is_relevant,
)
from app.services.searxng_settings import DEFAULT_ENGINE_IDS, get_active_searxng_engines

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_USERNAME_QUERY_COUNT = 8
USERNAME_QUERY_HOSTS = {
    "instagram": ("instagram.com",),
    "social": ("reddit.com", "github.com", "instagram.com", "x.com", "tiktok.com", "facebook.com", "discord.com", "threads.net", "bsky.app", "pinterest.com"),
    "community": ("reddit.com", "tumblr.com", "disqus.com", "discord.com"),
    "gaming": ("twitch.tv", "steamcommunity.com", "xboxgamertag.com"),
    "creator": ("youtube.com", "soundcloud.com", "bandcamp.com", "last.fm"),
    "developer": ("github.com", "gitlab.com", "news.ycombinator.com", "stackoverflow.com"),
}
SEARXNG_USAGE_SCOPE = "local_self_hosted_metasearch"


def build_searxng_username_queries(username: str) -> tuple[dict[str, str], ...]:
    value = username.strip().lstrip("@")
    exact = _quoted(value)
    queries = (
        {
            "id": "exact",
            "label": "Exact username across the public web",
            "query": exact,
        },
        {
            "id": "social",
            "label": "Public social and community profiles",
            "query": (
                f"{exact} (site:reddit.com OR site:github.com OR site:instagram.com "
                "OR site:x.com OR site:tiktok.com OR site:facebook.com OR site:discord.com)"
            ),
        },
        {
            "id": "instagram",
            "label": "Instagram profile",
            "query": f"{exact} site:instagram.com",
        },
        {
            "id": "documents",
            "label": "Documents and published records",
            "query": f"{exact} (filetype:pdf OR filetype:doc OR filetype:docx OR filetype:txt)",
        },
        {"id": "community", "label": "Community and forum accounts",
         "query": f"{exact} (site:reddit.com OR site:tumblr.com OR site:disqus.com OR site:discord.com)"},
        {"id": "gaming", "label": "Gaming and streaming profiles",
         "query": f"{exact} (site:twitch.tv OR site:steamcommunity.com/id/ OR site:xboxgamertag.com)"},
        {"id": "creator", "label": "Video, music and creator profiles",
         "query": f"{exact} (site:youtube.com OR site:soundcloud.com OR site:bandcamp.com OR site:last.fm/user/)"},
        {"id": "developer", "label": "Developer and technical community profiles",
         "query": f"{exact} (site:github.com OR site:gitlab.com OR site:news.ycombinator.com/user OR site:stackoverflow.com/users/)"},
    )
    return tuple(
        {**item, "query": item["query"][:900]} for item in queries[:MAX_USERNAME_QUERY_COUNT]
    )


def username_result_is_relevant(username: str, record: dict[str, Any]) -> bool:
    needle = username.strip().lstrip("@").casefold()
    if not needle:
        return False
    parsed = urlsplit(str(record.get("url") or ""))
    segments = [unquote(segment).casefold().lstrip("@") for segment in parsed.path.split("/") if segment]
    if parsed.hostname == "news.ycombinator.com" and parsed.path == "/user" and needle in parse_qs(parsed.query).get("id", []):
        return True
    if any(segment.rsplit(".", 1)[0] == needle and segment.endswith((".pdf", ".doc", ".docx", ".txt")) for segment in segments):
        return True
    # Everything else goes through the rule shared by all search collectors: an account page
    # must be this exact handle, and a page that only mentions the word is not kept.
    return shared_username_result_is_relevant(
        needle, str(record.get("url") or ""), html.unescape(str(record.get("title") or "")),
        str(record.get("content") or record.get("snippet") or ""),
    )


def username_result_matches_query_spec(spec_id: str, record: dict[str, Any]) -> bool:
    host = (urlsplit(str(record.get("url") or "")).hostname or "").casefold().removeprefix("www.")
    if spec_id in USERNAME_QUERY_HOSTS:
        return any(host == item or host.endswith(f".{item}") for item in USERNAME_QUERY_HOSTS[spec_id])
    if spec_id == "documents":
        path = urlsplit(str(record.get("url") or "")).path.casefold()
        return path.endswith((".pdf", ".doc", ".docx", ".txt", ".xls", ".xlsx"))
    return True


def _clean_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned[:limit] if cleaned else None


def normalise_searxng_result(record: dict[str, Any]) -> dict[str, Any] | None:
    url = _public_result_url(str(record.get("url") or ""))
    if not url:
        return None
    parsed = urlsplit(url)
    if not parsed.hostname or parsed.username or parsed.password:
        return None
    engines = record.get("engines", [])
    if not isinstance(engines, list):
        engines = []
    positions = record.get("positions", [])
    if not isinstance(positions, list):
        positions = []
    return {
        "url": url,
        "title": _clean_text(record.get("title"), 500),
        "snippet": _clean_text(record.get("content"), 2000),
        "published_at": _clean_text(
            record.get("publishedDate") or record.get("published_date"), 100
        ),
        "engines": sorted({str(item)[:80] for item in engines if item}),
        "positions": [int(item) for item in positions if isinstance(item, int | float)][:20],
        "score": record.get("score") if isinstance(record.get("score"), int | float) else None,
        "category": _clean_text(record.get("category"), 80),
    }


class SearXNGNameSearchAdapter(Adapter):
    # Keep the legacy tool_id/executable for persisted investigations and update compatibility.
    tool_id = "searxng_name_search"
    display_name = "SearXNG Web Search"
    executable = "searxng-name-search"
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
        self.base_url = (base_url or get_settings().searxng_url).rstrip("/")
        self.cancel_requested = False

    async def healthcheck(self) -> HealthResult:
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=8.0,
                follow_redirects=False,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "OSINT-Workbench/0.1",
                    "X-Forwarded-For": "127.0.0.1",
                    "X-Real-IP": "127.0.0.1",
                },
            ) as client:
                response = await client.get(f"{self.base_url}/config")
                response.raise_for_status()
                data = response.json()
            instance_name = str(data.get("instance_name") or "SearXNG")[:100]
            return HealthResult(True, "ready", instance_name)
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            return HealthResult(False, f"local SearXNG health check failed: {exc}")

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        if query_type not in self.supported_query_types:
            raise ValueError("SearXNG Web Search accepts name or username investigations only")
        command = [
            self.executable,
            query,
            "--query-type",
            query_type.value,
            "--output",
            str(output_dir / "searxng-name-search.json"),
            "--timeout",
            str(timeout),
        ]
        if proxy:
            command.extend(["--proxy-note", "configured-on-workbench-not-searxng"])
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
                error_message="SearXNG Web Search command did not match the bounded adapter contract",
            )
        try:
            query_type = QueryType(command[3])
        except ValueError:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="SearXNG Web Search received an invalid query type",
            )
        if query_type not in self.supported_query_types:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="SearXNG Web Search received an unsupported query type",
            )

        relevance_filter: Callable[[str, dict[str, Any]], bool]
        if query_type == QueryType.USERNAME:
            query_specs = build_searxng_username_queries(query)
            relevance_filter = username_result_is_relevant
        else:
            query_specs = build_bing_name_queries(query)[:MAX_QUERY_COUNT]
            relevance_filter = name_result_is_relevant
        query_records: list[dict[str, Any]] = []
        merged: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, str]] = []
        engine_failures: list[dict[str, str]] = []
        rejected_irrelevant_count = 0
        rate_limited = False
        timed_out = False
        started_at = datetime.now(UTC)
        try:
            with SessionLocal() as db:
                active_engines = get_active_searxng_engines(db)
        except SQLAlchemyError:
            active_engines = DEFAULT_ENGINE_IDS

        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(
                    transport=self.transport,
                    timeout=min(20.0, max(8.0, timeout / max(1, len(query_specs)))),
                    follow_redirects=False,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "OSINT-Workbench/0.1",
                        "X-Forwarded-For": "127.0.0.1",
                        "X-Real-IP": "127.0.0.1",
                    },
                ) as client:
                    for index, spec in enumerate(query_specs):
                        if self.cancel_requested:
                            return ExecutionResult(
                                None,
                                json.dumps({"queries_completed": index, "cancelled": True}),
                                "",
                                cancelled=True,
                                error_code="cancelled",
                                error_message="SearXNG Name Search was cancelled",
                            )
                        request_query = spec["query"].strip()
                        engine_selection = ",".join(active_engines)
                        try:
                            response = await client.get(
                                f"{self.base_url}/search",
                                params={
                                    "q": request_query, "engines": engine_selection,
                                    "format": "json", "language": "en", "safesearch": "0",
                                },
                            )
                        except httpx.HTTPError as exc:
                            errors.append({"query_id": spec["id"], "error": f"Search request failed: {type(exc).__name__}"})
                            continue
                        if response.status_code == 429:
                            rate_limited = True
                            errors.append(
                                {
                                    "query_id": spec["id"],
                                    "error": "local SearXNG returned HTTP 429",
                                }
                            )
                            break
                        if response.status_code >= 500:
                            errors.append({"query_id": spec["id"], "error": f"local SearXNG returned HTTP {response.status_code}"})
                            continue
                        response.raise_for_status()
                        if len(response.content) > MAX_RESPONSE_BYTES:
                            errors.append(
                                {
                                    "query_id": spec["id"],
                                    "error": "SearXNG JSON response exceeded the 2 MiB limit",
                                }
                            )
                            continue
                        try:
                            data = response.json()
                        except ValueError:
                            errors.append({"query_id": spec["id"], "error": "Search returned malformed JSON"})
                            continue
                        raw_results = data.get("results", []) if isinstance(data, dict) else []
                        if not isinstance(raw_results, list):
                            raw_results = []
                        normalised = [
                            item
                            for item in (
                                normalise_searxng_result(record)
                                for record in raw_results[: MAX_RESULTS_PER_QUERY * 2]
                                if isinstance(record, dict)
                            )
                            if item is not None
                        ]
                        relevant = [
                            record
                            for record in normalised
                            if relevance_filter(query, record)
                            and (
                                query_type != QueryType.USERNAME
                                or username_result_matches_query_spec(spec["id"], record)
                            )
                        ]
                        rejected_irrelevant_count += len(normalised) - len(relevant)
                        unresponsive = (
                            data.get("unresponsive_engines", []) if isinstance(data, dict) else []
                        )
                        if isinstance(unresponsive, list):
                            for item in unresponsive[:50]:
                                if isinstance(item, list | tuple) and len(item) >= 2:
                                    engine_failures.append(
                                        {
                                            "engine": str(item[0])[:80],
                                            "error": str(item[1])[:300],
                                            "query_id": spec["id"],
                                            "query": spec["query"],
                                        }
                                    )
                        raw_path = output_dir / f"searxng-{spec['id']}.json"
                        raw_path.write_text(
                            json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        raw_path.chmod(0o600)
                        query_records.append(
                            {
                                **spec,
                                "returned_count": len(normalised),
                                "retained_count": len(relevant),
                                "raw_json": raw_path.name,
                                "requested_engines": list(active_engines),
                                "request_query": request_query,
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
                                f"SearXNG {query_type.value} query {index + 1}/{len(query_specs)} retained "
                                f"{len(relevant)}/{len(normalised)} result(s): {spec['label']}",
                            )
                        if index + 1 < len(query_specs):
                            await asyncio.sleep(0.5)
        except TimeoutError:
            timed_out = True
        except httpx.HTTPStatusError as exc:
            errors.append(
                {
                    "query_id": query_specs[len(query_records)]["id"]
                    if len(query_records) < len(query_specs)
                    else "unknown",
                    "error": f"local SearXNG returned HTTP {exc.response.status_code}",
                }
            )
        except (httpx.HTTPError, ValueError, OSError) as exc:
            errors.append({"query_id": "collection", "error": str(exc)[:500]})

        payload = {
            "schema_version": 1,
            "tool": self.tool_id,
            "engine": "searxng",
            "endpoint": self.base_url,
            "usage_scope": SEARXNG_USAGE_SCOPE,
            "requested_engines": list(active_engines),
            "query": query,
            "query_type": query_type.value,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "queries": query_records,
            "errors": errors,
            "unresponsive_engines": engine_failures,
            "rejected_irrelevant_count": rejected_irrelevant_count,
            "results": list(merged.values()),
        }
        output_path = output_dir / "searxng-name-search.json"
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
                "requested_engines": list(active_engines),
                "unresponsive_engines": len(engine_failures),
                "rejected_irrelevant": rejected_irrelevant_count,
            },
            sort_keys=True,
        )
        stderr_parts = [item["error"] for item in errors]
        challenge_failures = [
            item
            for item in engine_failures
            if any(marker in item["error"].casefold() for marker in ("captcha", "challenge"))
        ]
        if engine_failures:
            stderr_parts.append(
                f"{len(engine_failures)} upstream SearXNG engine failure(s) were recorded"
            )
        challenge_message = None
        if challenge_failures:
            challenged_engines = ", ".join(sorted({item["engine"] for item in challenge_failures}))
            challenge_message = (
                f"Manual action required: {challenged_engines} returned a CAPTCHA/challenge. "
                "Open the sock browser and run the search manually to complete the challenge."
            )
        if timed_out:
            return ExecutionResult(
                None,
                stdout,
                "\n".join([*stderr_parts, "SearXNG Name Search exceeded its bounded timeout"]),
                output_files=[*output_dir.glob("*")],
                timed_out=True,
                error_code="timeout",
                error_message=f"tool exceeded {timeout}s",
            )
        if not query_records and rate_limited:
            return ExecutionResult(
                None,
                stdout,
                "\n".join(stderr_parts) or "local SearXNG rate limited every name query",
                output_files=[*output_dir.glob("*")],
                rate_limited=True,
                error_code="rate_limited",
                error_message="local SearXNG rate limited every name query",
            )
        if not query_records and errors:
            return ExecutionResult(
                None,
                stdout,
                "\n".join(stderr_parts),
                output_files=[*output_dir.glob("*")],
                error_code="upstream_error",
                error_message=errors[-1]["error"],
            )
        return ExecutionResult(
            0,
            stdout,
            "\n".join(stderr_parts),
            output_files=[*output_dir.glob("*")],
            rate_limited=rate_limited,
            error_code="manual_action_required" if challenge_message else None,
            error_message=challenge_message,
        )

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "searxng-name-search.json"
        if not path.is_file():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload.get("results", []) if isinstance(payload, dict) else []
        return [record for record in records if isinstance(record, dict)]

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        findings: list[FindingRecord] = []
        for record in records:
            url = _public_result_url(str(record.get("url") or ""))
            if not url:
                continue
            host = (urlsplit(url).hostname or "").removeprefix("www.") or None
            title = _clean_text(record.get("title"), 500)
            snippet = _clean_text(record.get("snippet"), 2000)
            source_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="search_result",
                    platform=host,
                    value=title or url,
                    canonical_url=url,
                    display_url=url,
                    status=FindingStatus.LIKELY,
                    confidence=None,
                    title=title,
                    description=snippet,
                    metadata={
                        "engine": "searxng",
                        "usage_scope": SEARXNG_USAGE_SCOPE,
                        "published_at": record.get("published_at"),
                        "query_matches": record.get("query_matches", []),
                        "search_engines": record.get("engines", []),
                        "search_positions": record.get("positions", []),
                        "search_score": record.get("score"),
                        "profile": {},
                    },
                    source_tool=self.tool_id,
                    source_record_id=source_id,
                    raw_output_reference=raw_reference,
                )
            )
        return findings
