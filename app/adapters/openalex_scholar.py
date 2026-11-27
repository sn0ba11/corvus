from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.models.schemas import FindingRecord, FindingStatus, ProfileEvidence, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

ENDPOINT = "https://api.openalex.org/authors"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESULTS = 8
USER_AGENT = "Corvus/0.1 OpenAlex scholarly identity"
SPACE_RE = re.compile(r"\s+")


def _clean(value: object, limit: int = 1000) -> str | None:
    if value in (None, ""):
        return None
    text = SPACE_RE.sub(" ", str(value)).strip()[:limit]
    return text or None


def _normalise_name(value: object) -> str:
    return SPACE_RE.sub(" ", str(value or "")).strip().casefold()


def _openalex_id(value: object) -> str | None:
    text = _clean(value, 200)
    if not text:
        return None
    return text.rstrip("/").rsplit("/", 1)[-1]


def _institution_names(item: dict[str, Any]) -> list[str]:
    output: list[str] = []
    values: list[object] = []
    last_known = item.get("last_known_institutions")
    if isinstance(last_known, list):
        values.extend(last_known[:10])
    legacy = item.get("last_known_institution")
    if isinstance(legacy, dict):
        values.append(legacy)
    affiliations = item.get("affiliations")
    if isinstance(affiliations, list):
        for affiliation in affiliations[:20]:
            if isinstance(affiliation, dict):
                values.append(affiliation.get("institution"))
    for value in values:
        if not isinstance(value, dict):
            continue
        name = _clean(value.get("display_name"), 300)
        if name and name.casefold() not in {existing.casefold() for existing in output}:
            output.append(name)
    return output[:10]


def _topics(item: dict[str, Any]) -> list[str]:
    output: list[str] = []
    for topic in item.get("topics", [])[:10] if isinstance(item.get("topics"), list) else []:
        if not isinstance(topic, dict):
            continue
        name = _clean(topic.get("display_name"), 200)
        if name:
            output.append(name)
    return output


def parse_openalex_authors(payload: object, query: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("OpenAlex returned an unexpected author-search response")
    query_name = _normalise_name(query)
    records: list[dict[str, Any]] = []
    for item in payload["results"][:MAX_RESULTS]:
        if not isinstance(item, dict):
            continue
        author_id = _openalex_id(item.get("id"))
        display_name = _clean(item.get("display_name"), 300)
        if not author_id or not display_name:
            continue
        alternatives: list[str] = []
        for field in ("display_name_alternatives", "raw_author_names"):
            values = item.get(field)
            if not isinstance(values, list):
                continue
            for value in values[:20]:
                if not isinstance(value, str) or not value.strip():
                    continue
                clean_value = value.strip()[:300]
                if clean_value.casefold() not in {existing.casefold() for existing in alternatives}:
                    alternatives.append(clean_value)
        full_name = _clean(item.get("full_name"), 300)
        if full_name and full_name.casefold() not in {existing.casefold() for existing in alternatives}:
            alternatives.append(full_name)
        exact_name = query_name in {
            _normalise_name(display_name),
            *(_normalise_name(value) for value in alternatives),
        }
        institutions = _institution_names(item)
        records.append(
            {
                "openalex_id": author_id,
                "display_name": display_name,
                "display_name_alternatives": alternatives,
                "orcid": _clean(item.get("orcid"), 100),
                "works_count": item.get("works_count"),
                "cited_by_count": item.get("cited_by_count"),
                "institutions": institutions,
                "topics": _topics(item),
                "works_api_url": f"https://api.openalex.org/works?filter=author.id:{author_id}&per-page=10",
                "openalex_url": f"https://openalex.org/{author_id}",
                "exact_name": exact_name,
            }
        )
    return records


class OpenAlexScholarAdapter(Adapter):
    tool_id = "openalex_scholar"
    display_name = "OpenAlex Scholarly Identity"
    executable = "openalex-scholar"
    capabilities = frozenset(
        {
            "scholarly_identity",
            "orcid_pivot",
            "public_affiliations",
            "research_topics",
        }
    )
    supported_query_types = frozenset({QueryType.NAME})
    default_selected = False
    default_timeout = 45
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "OpenAlex API")

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        if query_type != QueryType.NAME:
            raise ValueError("OpenAlex Scholarly Identity accepts full-name investigations only")
        clean_query = _clean(query, 320)
        if not clean_query or len(clean_query.split()) < 2:
            raise ValueError("OpenAlex Scholarly Identity requires a full name")
        command = [
            self.executable,
            clean_query,
            "--output",
            str(output_dir / "openalex-scholar.json"),
            "--timeout",
            str(timeout),
        ]
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
        clean_query = _clean(query, 320)
        if not clean_query:
            return ExecutionResult(None, "", "", error_code="invalid_query", error_message="OpenAlex query is empty")
        if len(command) < 6 or command[0] != self.executable or command[1] != clean_query:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="OpenAlex command did not match the bounded adapter contract",
            )
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        request_timeout = min(20.0, max(5.0, float(timeout)))
        client_context = (
            httpx.AsyncClient(
                transport=self.transport,
                timeout=request_timeout,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            if self.transport is not None
            else safe_client(timeout=request_timeout, user_agent=USER_AGENT, proxy=proxy)
        )
        try:
            async with asyncio.timeout(timeout):
                async with client_context as client:
                    request = client.build_request(
                        "GET",
                        ENDPOINT,
                        params={"search": clean_query, "per-page": str(MAX_RESULTS)},
                    )
                    response = await client.send(request, stream=True)
                    try:
                        if response.status_code == 429:
                            return ExecutionResult(
                                None,
                                "",
                                "",
                                rate_limited=True,
                                error_code="rate_limited",
                                error_message="OpenAlex rate limited the request",
                            )
                        if response.is_redirect:
                            raise ValueError("OpenAlex redirected unexpectedly")
                        response.raise_for_status()
                        raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
                    finally:
                        await response.aclose()
            records = parse_openalex_authors(json.loads(raw), clean_query)
            payload = {
                "schema_version": "1.0",
                "source": "OpenAlex author search",
                "query": clean_query,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "results": records,
                "limits": {"results": MAX_RESULTS, "response_bytes": MAX_RESPONSE_BYTES},
                "caution": (
                    "OpenAlex author disambiguation and affiliation metadata can contain errors; "
                    "records are candidate scholarly identities until independently corroborated."
                ),
            }
            path = output_dir / "openalex-scholar.json"
            path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            path.chmod(0o600)
            if log:
                await log("stdout", f"OpenAlex retained {len(records)} bounded author candidate(s)")
            return ExecutionResult(
                0,
                json.dumps({"status": "found" if records else "unknown", "results": len(records)}),
                "",
                output_files=[path],
            )
        except TimeoutError:
            return ExecutionResult(
                None,
                "",
                "",
                timed_out=True,
                error_code="timeout",
                error_message=f"OpenAlex scholarly search exceeded {timeout}s",
            )
        except (httpx.HTTPError, EgressPolicyError, ValueError, json.JSONDecodeError) as exc:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="upstream_error",
                error_message=f"OpenAlex scholarly search failed: {exc}",
            )

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "openalex-scholar.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            return []
        return [item for item in payload["results"][:MAX_RESULTS] if isinstance(item, dict)]

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        findings: list[FindingRecord] = []
        for record in records[:MAX_RESULTS]:
            author_id = str(record.get("openalex_id") or "").strip()
            display_name = str(record.get("display_name") or "").strip()
            if not author_id or not display_name:
                continue
            institutions = [
                str(value)
                for value in record.get("institutions", [])[:10]
                if isinstance(value, str) and value.strip()
            ] if isinstance(record.get("institutions"), list) else []
            orcid = _clean(record.get("orcid"), 100)
            exact_name = bool(record.get("exact_name"))
            confidence = 0.72 if exact_name and orcid else (0.62 if exact_name else 0.42)
            description_parts = []
            if institutions:
                description_parts.append("Affiliations: " + ", ".join(institutions[:3]))
            if orcid:
                description_parts.append(f"ORCID: {orcid.rstrip('/').rsplit('/', 1)[-1]}")
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="scholarly_profile",
                    platform="OpenAlex",
                    value=author_id,
                    canonical_url=str(record.get("openalex_url") or f"https://openalex.org/{author_id}"),
                    display_url=str(record.get("openalex_url") or f"https://openalex.org/{author_id}"),
                    status=FindingStatus.LIKELY,
                    confidence=confidence,
                    title=display_name,
                    description=" · ".join(description_parts) or "Candidate scholarly identity from OpenAlex.",
                    metadata={
                        "openalex_id": author_id,
                        "orcid": orcid,
                        "display_name_alternatives": record.get("display_name_alternatives", []),
                        "institutions": institutions,
                        "topics": record.get("topics", []),
                        "works_count": record.get("works_count"),
                        "cited_by_count": record.get("cited_by_count"),
                        "exact_name": exact_name,
                        "works_api_url": record.get("works_api_url"),
                        "source_scope": "public_openalex_author_search",
                        "identity_caution": (
                            "OpenAlex author disambiguation and affiliation metadata can be wrong; "
                            "corroborate this candidate with ORCID, works, affiliation or other evidence."
                        ),
                    },
                    profile=ProfileEvidence(
                        display_name=display_name,
                        location=institutions[0] if institutions else None,
                    ),
                    source_tool=self.tool_id,
                    source_record_id=f"openalex:{author_id}",
                    raw_output_reference=raw_reference,
                )
            )
        return findings
