from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from defusedxml import DefusedXmlException  # type: ignore[import-untyped]
from defusedxml.ElementTree import fromstring as safe_xml_fromstring  # type: ignore[import-untyped]

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.core.security import canonicalise_url
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client
from app.services.name_variants import ascii_transliteration, collect_name_variants

BING_RSS_ENDPOINT = "https://www.bing.com/search"
MAX_QUERY_COUNT = 5
MAX_RESULTS_PER_QUERY = 10
MAX_RESPONSE_BYTES = 512 * 1024
RSS_USAGE_SCOPE = "personal_non_commercial_rss_rendering"


def _clean_text(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    without_markup = re.sub(r"<[^>]+>", " ", html.unescape(value))
    cleaned = re.sub(r"\s+", " ", without_markup).strip()
    return cleaned[:limit] if cleaned else None


def _quoted(value: str) -> str:
    return f'"{value.replace(chr(34), " ").strip()}"'


def _normalised_match_text(value: str) -> str:
    transliterated = ascii_transliteration(value).casefold()
    return re.sub(r"[^a-z0-9]+", " ", transliterated).strip()


def name_result_is_relevant(name: str, record: dict[str, Any]) -> bool:
    searchable = _normalised_match_text(
        " ".join(str(record.get(key) or "") for key in ("title", "snippet", "url"))
    )
    if not searchable:
        return False
    padded = f" {searchable} "
    source_tokens = _normalised_match_text(name).split()
    if len(source_tokens) >= 2:
        surname = source_tokens[-1]
        given_tokens = [token for token in source_tokens[:-1] if len(token) > 1]
        searchable_tokens = set(searchable.split())
        if surname in searchable_tokens and any(
            token in searchable_tokens for token in given_tokens
        ):
            return True
    for variant in collect_name_variants(name, limit=16):
        normalised = _normalised_match_text(variant.value)
        if len(normalised.split()) >= 2 and f" {normalised} " in padded:
            return True
    return False


def build_bing_name_queries(name: str) -> tuple[dict[str, str], ...]:
    variants = collect_name_variants(name, limit=8)
    original = variants[0].value
    variant_values = [item.value for item in variants[:6]]
    variant_expression = " OR ".join(_quoted(value) for value in variant_values)
    queries = (
        {
            "id": "exact",
            "label": "Exact name across the public web",
            "query": _quoted(original),
        },
        {
            "id": "professional",
            "label": "Professional and researcher profiles",
            "query": (
                f"{_quoted(original)} "
                "(site:linkedin.com/in OR site:github.com OR site:researchgate.net/profile "
                "OR site:orcid.org)"
            ),
        },
        {
            "id": "social",
            "label": "Public social profiles",
            "query": (
                f"{_quoted(original)} "
                "(site:facebook.com OR site:instagram.com OR site:x.com OR site:tiktok.com)"
            ),
        },
        {
            "id": "documents",
            "label": "Documents and published records",
            "query": f"{_quoted(original)} (filetype:pdf OR filetype:doc OR filetype:docx)",
        },
        {
            "id": "variants",
            "label": "Deterministic name variants",
            "query": f"({variant_expression})",
        },
    )
    return tuple({**item, "query": item["query"][:900]} for item in queries[:MAX_QUERY_COUNT])


def _public_result_url(value: str) -> str | None:
    canonical = canonicalise_url(value)
    if not canonical:
        return None
    parsed = urlsplit(canonical)
    if parsed.username or parsed.password or not parsed.hostname:
        return None
    host = parsed.hostname.casefold().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            return None
    return canonical


def parse_bing_rss(xml_text: str) -> list[dict[str, str | None]]:
    try:
        root = safe_xml_fromstring(xml_text, forbid_dtd=True)
    except DefusedXmlException as exc:
        raise ValueError("Bing RSS response contained a prohibited XML declaration") from exc
    except ET.ParseError as exc:
        raise ValueError("Bing RSS response was not valid XML") from exc
    records: list[dict[str, str | None]] = []
    for item in root.findall("./channel/item")[:MAX_RESULTS_PER_QUERY]:
        url = _clean_text(item.findtext("link"), 2048)
        if not url:
            continue
        canonical = _public_result_url(url)
        if not canonical:
            continue
        host = (urlsplit(canonical).hostname or "").casefold()
        if host == "bing.com" or host.endswith(".bing.com"):
            continue
        records.append(
            {
                "url": canonical,
                "title": _clean_text(item.findtext("title"), 500),
                "snippet": _clean_text(item.findtext("description"), 2000),
                "published_at": _clean_text(item.findtext("pubDate"), 100),
            }
        )
    return records


class BingNameSearchAdapter(Adapter):
    tool_id = "bing_name_search"
    display_name = "Bing Name Search"
    executable = "bing-name-search"
    capabilities = frozenset({"name_search", "rss_output", "title_snippet_results"})
    supported_query_types = frozenset({QueryType.NAME})
    default_timeout = 90
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport
        self.cancel_requested = False

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "Bing RSS")

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        if query_type != QueryType.NAME:
            raise ValueError("Bing Name Search accepts full-name investigations only")
        command = [
            self.executable,
            query,
            "--output",
            str(output_dir / "bing-name-search.json"),
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
                error_message="Bing Name Search command did not match the bounded adapter contract",
            )

        query_specs = build_bing_name_queries(query)
        query_records: list[dict[str, Any]] = []
        merged: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, str]] = []
        rejected_irrelevant_count = 0
        rate_limited = False
        timed_out = False
        started_at = datetime.now(UTC)
        headers = {
            "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/142 Safari/537.36 OSINT-Workbench/0.1"
            ),
        }

        try:
            async with asyncio.timeout(timeout):
                request_timeout = min(20.0, max(5.0, timeout / max(1, len(query_specs))))
                client_context = (
                    httpx.AsyncClient(
                        transport=self.transport,
                        timeout=request_timeout,
                        follow_redirects=False,
                        headers=headers,
                    )
                    if self.transport is not None
                    else safe_client(
                        timeout=request_timeout,
                        user_agent=headers["User-Agent"],
                    )
                )
                async with client_context as client:
                    for index, spec in enumerate(query_specs):
                        if self.cancel_requested:
                            return ExecutionResult(
                                None,
                                json.dumps({"queries_completed": index, "cancelled": True}),
                                "",
                                cancelled=True,
                                error_code="cancelled",
                                error_message="Bing Name Search was cancelled",
                            )
                        request = client.build_request(
                            "GET",
                            BING_RSS_ENDPOINT,
                            params={
                                "format": "rss",
                                "count": str(MAX_RESULTS_PER_QUERY),
                                "q": spec["query"],
                            },
                        )
                        response = await client.send(request, stream=True)
                        try:
                            content = await read_bounded_response(
                                response, max_bytes=MAX_RESPONSE_BYTES
                            )
                        except EgressPolicyError:
                            errors.append(
                                {
                                    "query_id": spec["id"],
                                    "error": "Bing RSS response exceeded the 512 KiB limit",
                                }
                            )
                            await response.aclose()
                            continue
                        await response.aclose()
                        if response.status_code == 429:
                            rate_limited = True
                            errors.append(
                                {
                                    "query_id": spec["id"],
                                    "error": "Bing returned HTTP 429",
                                }
                            )
                            continue
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").casefold()
                        if "xml" not in content_type:
                            errors.append(
                                {
                                    "query_id": spec["id"],
                                    "error": "Bing returned a non-XML response",
                                }
                            )
                            continue
                        xml_path = output_dir / f"bing-{spec['id']}.xml"
                        xml_path.write_bytes(content)
                        xml_path.chmod(0o600)
                        records = parse_bing_rss(
                            content.decode(response.encoding or "utf-8", errors="replace")
                        )
                        relevant_records = [
                            record for record in records if name_result_is_relevant(query, record)
                        ]
                        rejected_irrelevant_count += len(records) - len(relevant_records)
                        query_records.append(
                            {
                                **spec,
                                "returned_count": len(records),
                                "retained_count": len(relevant_records),
                                "raw_xml": xml_path.name,
                            }
                        )
                        for record in relevant_records:
                            url = str(record["url"])
                            existing = merged.get(url)
                            query_match = {
                                "query_id": spec["id"],
                                "query_label": spec["label"],
                                "query": spec["query"],
                                "raw_xml": xml_path.name,
                            }
                            if existing is None:
                                merged[url] = {**record, "query_matches": [query_match]}
                            else:
                                existing["query_matches"].append(query_match)
                                if not existing.get("title") and record.get("title"):
                                    existing["title"] = record["title"]
                                if not existing.get("snippet") and record.get("snippet"):
                                    existing["snippet"] = record["snippet"]
                        if log:
                            await log(
                                "stdout",
                                f"Bing name query {index + 1}/{len(query_specs)} returned "
                                f"{len(relevant_records)}/{len(records)} relevant result(s): "
                                f"{spec['label']}",
                            )
                        if index + 1 < len(query_specs):
                            await asyncio.sleep(0.75)
        except TimeoutError:
            timed_out = True
        except httpx.HTTPStatusError as exc:
            errors.append(
                {
                    "query_id": query_specs[len(query_records)]["id"]
                    if len(query_records) < len(query_specs)
                    else "unknown",
                    "error": f"Bing returned HTTP {exc.response.status_code}",
                }
            )
        except (httpx.HTTPError, ValueError, OSError) as exc:
            errors.append({"query_id": "collection", "error": str(exc)[:500]})

        payload = {
            "schema_version": 1,
            "tool": self.tool_id,
            "engine": "bing",
            "endpoint": BING_RSS_ENDPOINT,
            "usage_scope": RSS_USAGE_SCOPE,
            "query": query,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "queries": query_records,
            "errors": errors,
            "rejected_irrelevant_count": rejected_irrelevant_count,
            "results": list(merged.values()),
        }
        output_path = output_dir / "bing-name-search.json"
        output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
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
        if timed_out:
            return ExecutionResult(
                None,
                stdout,
                "Bing Name Search exceeded its bounded timeout",
                output_files=[*output_dir.glob("*")],
                timed_out=True,
                error_code="timeout",
                error_message=f"tool exceeded {timeout}s",
            )
        if not query_records and rate_limited:
            return ExecutionResult(
                None,
                stdout,
                "Bing rate limited every name query",
                output_files=[*output_dir.glob("*")],
                rate_limited=True,
                error_code="rate_limited",
                error_message="Bing rate limited every name query",
            )
        if not query_records and errors:
            return ExecutionResult(
                None,
                stdout,
                errors[-1]["error"],
                output_files=[*output_dir.glob("*")],
                error_code="upstream_error",
                error_message=errors[-1]["error"],
            )
        return ExecutionResult(
            0,
            stdout,
            "\n".join(item["error"] for item in errors),
            output_files=[*output_dir.glob("*")],
            rate_limited=rate_limited,
        )

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "bing-name-search.json"
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
            url = canonicalise_url(str(record.get("url") or ""))
            if not url:
                continue
            host = (urlsplit(url).hostname or "").removeprefix("www.") or None
            title = _clean_text(str(record.get("title") or ""), 500)
            snippet = _clean_text(str(record.get("snippet") or ""), 2000)
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
                        "engine": "bing",
                        "usage_scope": RSS_USAGE_SCOPE,
                        "published_at": record.get("published_at"),
                        "query_matches": record.get("query_matches", []),
                        "profile": {},
                    },
                    source_tool=self.tool_id,
                    source_record_id=source_id,
                    raw_output_reference=raw_reference,
                )
            )
        return findings
