from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.db.session import SessionLocal
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client
from app.services.external_api_settings import get_companies_house_api_key

API_BASE = "https://api.company-information.service.gov.uk"
MAX_SEARCH_RESULTS = 10
MAX_OFFICERS_WITH_APPOINTMENTS = 5
MAX_APPOINTMENTS = 15
MAX_RESPONSE_BYTES = 1024 * 1024
USER_AGENT = "Corvus/0.1 Companies House public data collector"


def _clean(value: Any, limit: int = 1000) -> str | None:
    if value in (None, ""):
        return None
    text = " ".join(str(value).split())[:limit]
    return text or None


def _address(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    parts = [
        _clean(value.get(key), 200)
        for key in ("premises", "address_line_1", "address_line_2", "locality", "region", "postal_code", "country")
    ]
    return ", ".join(part for part in parts if part) or None


class CompaniesHouseAdapter(Adapter):
    tool_id = "companies_house"
    display_name = "UK Companies House"
    executable = "companies-house-public"
    capabilities = frozenset({"company_search", "officer_search", "officer_appointments", "official_registry_api"})
    supported_query_types = frozenset({QueryType.NAME})
    default_selected = False
    default_timeout = 45
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        with SessionLocal() as db:
            configured = get_companies_house_api_key(db) is not None
        return HealthResult(configured, "ready" if configured else "API key not configured", "Companies House REST API")

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        if query_type != QueryType.NAME:
            raise ValueError("Companies House accepts name investigations only")
        target = " ".join(query.split())
        if len(target) < 2 or len(target) > 200:
            raise ValueError("invalid Companies House search")
        command = [self.executable, "--query", target, "--output", str(output_dir / "companies-house.json")]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    @staticmethod
    async def _json(client: httpx.AsyncClient, url: str, headers: dict[str, str], params: dict[str, str] | None = None) -> dict[str, Any]:
        response = await client.send(client.build_request("GET", url, params=params, headers=headers), stream=True)
        try:
            if response.status_code == 429:
                raise RuntimeError("rate_limited")
            response.raise_for_status()
            raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
        finally:
            await response.aclose()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Companies House returned an unexpected response")
        return payload

    async def execute(
        self,
        command: list[str],
        output_dir: Path,
        timeout: int,
        query: str,
        log: LogCallback | None = None,
    ) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = " ".join(query.split())
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        expected = [self.executable, "--query", target, "--output", str(output_dir / "companies-house.json")]
        if proxy:
            expected.extend(["--proxy", proxy])
        if command != expected:
            return ExecutionResult(None, "", "", error_code="invalid_command", error_message="Companies House command did not match the bounded adapter contract")
        with SessionLocal() as db:
            api_key = get_companies_house_api_key(db)
        if api_key is None:
            return ExecutionResult(None, "", "", error_code="configuration_required", error_message="Configure a Companies House API key in Settings")
        auth = base64.b64encode(f"{api_key}:".encode()).decode()
        headers = {"Accept": "application/json", "Authorization": f"Basic {auth}", "User-Agent": USER_AGENT}
        request_timeout = min(20.0, max(5.0, float(timeout) / 2))
        client_context = (
            httpx.AsyncClient(transport=self.transport, timeout=request_timeout, follow_redirects=False, trust_env=False)
            if self.transport is not None
            else safe_client(timeout=request_timeout, user_agent=USER_AGENT, proxy=proxy)
        )
        try:
            async with asyncio.timeout(timeout):
                async with client_context as client:
                    companies_payload = await self._json(
                        client,
                        f"{API_BASE}/search/companies",
                        headers,
                        {"q": target, "items_per_page": str(MAX_SEARCH_RESULTS)},
                    )
                    officers_payload = await self._json(
                        client,
                        f"{API_BASE}/search/officers",
                        headers,
                        {"q": target, "items_per_page": str(MAX_SEARCH_RESULTS)},
                    )
                    officers_raw = officers_payload.get("items")
                    officers: list[Any] = officers_raw if isinstance(officers_raw, list) else []
                    appointment_batches: list[dict[str, Any]] = []
                    for officer in officers[:MAX_OFFICERS_WITH_APPOINTMENTS]:
                        if not isinstance(officer, dict):
                            continue
                        links_raw = officer.get("links")
                        links: dict[str, Any] = links_raw if isinstance(links_raw, dict) else {}
                        self_path = str(links.get("self") or "")
                        if not self_path.startswith("/officers/") or not self_path.endswith("/appointments"):
                            continue
                        appointment_payload = await self._json(
                            client,
                            f"{API_BASE}{quote(self_path, safe='/')}",
                            headers,
                            {"items_per_page": str(MAX_APPOINTMENTS)},
                        )
                        appointment_batches.append({
                            "officer_title": officer.get("title"),
                            "officer_path": self_path,
                            "payload": appointment_payload,
                        })
            companies_raw = companies_payload.get("items")
            companies: list[Any] = companies_raw if isinstance(companies_raw, list) else []
            artifact = {
                "source": "Companies House REST API",
                "query": target,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "companies": [item for item in companies[:MAX_SEARCH_RESULTS] if isinstance(item, dict)],
                "officers": [item for item in officers[:MAX_SEARCH_RESULTS] if isinstance(item, dict)],
                "appointment_batches": appointment_batches,
            }
            path = output_dir / "companies-house.json"
            path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
            path.chmod(0o600)
            count = len(artifact["companies"]) + len(artifact["officers"])
            if log:
                await log("stdout", f"Companies House retained {count} search result(s) and {len(appointment_batches)} bounded appointment set(s)")
            return ExecutionResult(0, json.dumps({"status": "found" if count else "not_found", "results": count}), "", output_files=[path])
        except TimeoutError:
            return ExecutionResult(None, "", "", timed_out=True, error_code="timeout", error_message=f"Companies House search exceeded {timeout}s")
        except RuntimeError as exc:
            if str(exc) == "rate_limited":
                return ExecutionResult(None, "", "", rate_limited=True, error_code="rate_limited", error_message="Companies House API rate limit was exhausted")
            raise
        except (httpx.HTTPError, EgressPolicyError, ValueError, json.JSONDecodeError) as exc:
            return ExecutionResult(None, "", "", error_code="upstream_error", error_message=f"Companies House search failed: {exc}")

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "companies-house.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [payload] if isinstance(payload, dict) else []

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        if not records:
            return []
        payload = records[0]
        findings: list[FindingRecord] = []
        companies_raw = payload.get("companies")
        companies: list[Any] = companies_raw if isinstance(companies_raw, list) else []
        for company in companies[:MAX_SEARCH_RESULTS]:
            if not isinstance(company, dict):
                continue
            number = _clean(company.get("company_number"), 30)
            title = _clean(company.get("title"), 300)
            if not number or not title:
                continue
            address = _address(company.get("address"))
            url = f"https://find-and-update.company-information.service.gov.uk/company/{quote(number, safe='')}"
            findings.append(FindingRecord(
                query_type=query_type,
                queried_value=query,
                finding_type="legal_entity",
                platform="Companies House",
                value=title,
                canonical_url=url,
                display_url=url,
                status=FindingStatus.LIKELY,
                confidence=0.78,
                title=title,
                description=address,
                metadata={
                    "company_number": number,
                    "company_status": company.get("company_status"),
                    "company_type": company.get("company_type"),
                    "date_of_creation": company.get("date_of_creation"),
                    "date_of_cessation": company.get("date_of_cessation"),
                    "address": address,
                    "description": company.get("description"),
                    "source_scope": "companies_house_public_search",
                },
                source_tool=self.tool_id,
                source_record_id=f"company:{number}",
                raw_output_reference=raw_reference,
            ))
        officers_raw = payload.get("officers")
        officers: list[Any] = officers_raw if isinstance(officers_raw, list) else []
        for officer in officers[:MAX_SEARCH_RESULTS]:
            if not isinstance(officer, dict):
                continue
            title = _clean(officer.get("title"), 300)
            links_raw = officer.get("links")
            links: dict[str, Any] = links_raw if isinstance(links_raw, dict) else {}
            self_path = str(links.get("self") or "")
            if not title or not self_path.startswith("/officers/"):
                continue
            officer_id = self_path.split("/", 3)[2] if self_path.count("/") >= 3 else self_path
            public_url = f"https://find-and-update.company-information.service.gov.uk{self_path}"
            findings.append(FindingRecord(
                query_type=query_type,
                queried_value=query,
                finding_type="companies_house_officer",
                platform="Companies House",
                value=title,
                canonical_url=public_url,
                display_url=public_url,
                status=FindingStatus.LIKELY,
                confidence=0.7,
                title=title,
                description=_address(officer.get("address")),
                metadata={
                    "appointment_count": officer.get("appointment_count"),
                    "date_of_birth": officer.get("date_of_birth"),
                    "address": _address(officer.get("address")),
                    "source_scope": "companies_house_officer_search",
                },
                source_tool=self.tool_id,
                source_record_id=f"officer:{officer_id}",
                raw_output_reference=raw_reference,
            ))
        batches_raw = payload.get("appointment_batches")
        batches: list[Any] = batches_raw if isinstance(batches_raw, list) else []
        for batch in batches[:MAX_OFFICERS_WITH_APPOINTMENTS]:
            if not isinstance(batch, dict):
                continue
            officer_title = _clean(batch.get("officer_title"), 300)
            officer_path = str(batch.get("officer_path") or "")
            appointment_payload_raw = batch.get("payload")
            appointment_payload: dict[str, Any] = appointment_payload_raw if isinstance(appointment_payload_raw, dict) else {}
            items_raw = appointment_payload.get("items")
            items: list[Any] = items_raw if isinstance(items_raw, list) else []
            for appointment in items[:MAX_APPOINTMENTS]:
                if not isinstance(appointment, dict):
                    continue
                appointed_raw = appointment.get("appointed_to")
                appointed: dict[str, Any] = appointed_raw if isinstance(appointed_raw, dict) else {}
                company_number = _clean(appointed.get("company_number"), 30)
                company_name = _clean(appointed.get("company_name"), 300)
                if not company_number or not company_name:
                    continue
                role = _clean(appointment.get("officer_role"), 100)
                url = f"https://find-and-update.company-information.service.gov.uk/company/{quote(company_number, safe='')}"
                findings.append(FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="company_officer_appointment",
                    platform="Companies House",
                    value=company_name,
                    canonical_url=url,
                    display_url=url,
                    status=FindingStatus.LIKELY,
                    confidence=0.82,
                    title=f"{officer_title or query} · {role or 'officer'} · {company_name}",
                    metadata={
                        "officer_name": officer_title,
                        "officer_path": officer_path,
                        "company_number": company_number,
                        "company_name": company_name,
                        "officer_role": role,
                        "appointed_on": appointment.get("appointed_on"),
                        "resigned_on": appointment.get("resigned_on"),
                        "occupation": appointment.get("occupation"),
                        "nationality": appointment.get("nationality"),
                        "country_of_residence": appointment.get("country_of_residence"),
                        "source_scope": "companies_house_public_officer_appointments",
                    },
                    source_tool=self.tool_id,
                    source_record_id=f"appointment:{officer_path}:{company_number}:{role or ''}",
                    raw_output_reference=raw_reference,
                ))
        return findings
