from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from app.services.egress_policy import read_bounded_response, safe_client

MAX_RESPONSE_BYTES = 768 * 1024
ASN_RE = re.compile(r"^(?:AS)?([1-9][0-9]{0,9})$", re.IGNORECASE)


class InfrastructureEnrichmentError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InfrastructureResult:
    provider: str
    resource: str
    finding_type: str
    title: str
    description: str | None
    canonical_url: str
    metadata: dict[str, Any]


def normalise_resource(value: str) -> tuple[str, str]:
    raw = value.strip()
    match = ASN_RE.fullmatch(raw)
    if match:
        number = int(match.group(1))
        if number <= 0 or number > 4294967295:
            raise InfrastructureEnrichmentError("ASN is outside the supported range")
        return f"AS{number}", "asn"
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise InfrastructureEnrichmentError("resource must be a public IP address or ASN") from exc
    if not address.is_global:
        raise InfrastructureEnrichmentError("resource must be a public, globally routable IP address")
    return address.compressed, "ip"


def _bounded_records(value: object) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    if not isinstance(value, list):
        return output
    for record in value[:25]:
        if not isinstance(record, list):
            continue
        for entry in record[:25]:
            if not isinstance(entry, dict):
                continue
            key = " ".join(str(entry.get("key") or "").split())[:80]
            item = " ".join(str(entry.get("value") or "").split())[:500]
            if key and item:
                output.append({"key": key, "value": item})
            if len(output) >= 100:
                return output
    return output


async def enrich_ripe(resource: str) -> InfrastructureResult:
    canonical, kind = normalise_resource(resource)
    url = "https://stat.ripe.net/data/whois/data.json"
    async with safe_client(timeout=15.0, user_agent="Corvus/0.1 RIPEstat enrichment") as client:
        request = client.build_request("GET", url, params={"resource": canonical})
        response = await client.send(request, stream=True)
        try:
            response.raise_for_status()
            raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
        finally:
            await response.aclose()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InfrastructureEnrichmentError("RIPEstat returned malformed JSON") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise InfrastructureEnrichmentError("RIPEstat returned an unexpected response")
    records = _bounded_records(data.get("records"))
    authorities = [str(item)[:50] for item in data.get("authorities", [])[:10]] if isinstance(data.get("authorities"), list) else []
    return InfrastructureResult(
        provider="RIPEstat",
        resource=canonical,
        finding_type="asn_registration" if kind == "asn" else "ip_registration",
        title=f"RIPEstat registration · {canonical}",
        description=f"{len(records)} bounded WHOIS fields from {', '.join(authorities) or 'registry data'}",
        canonical_url=f"https://stat.ripe.net/{quote(canonical, safe='')}",
        metadata={"resource": canonical, "resource_kind": kind, "authorities": authorities, "records": records, "query_time": data.get("query_time"), "source_scope": "ripe_stat_whois_api"},
    )


async def enrich_arin(resource: str) -> InfrastructureResult:
    canonical, kind = normalise_resource(resource)
    suffix = f"autnum/{canonical[2:]}" if kind == "asn" else f"ip/{canonical}"
    url = f"https://rdap.arin.net/registry/{suffix}"
    async with safe_client(timeout=15.0, user_agent="Corvus/0.1 ARIN RDAP enrichment") as client:
        response = await client.send(client.build_request("GET", url, headers={"Accept": "application/rdap+json,application/json"}), stream=True)
        try:
            if response.is_redirect:
                raise InfrastructureEnrichmentError("ARIN referred this resource to another registry; use RIPEstat for cross-RIR lookup")
            response.raise_for_status()
            raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
        finally:
            await response.aclose()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InfrastructureEnrichmentError("ARIN RDAP returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise InfrastructureEnrichmentError("ARIN RDAP returned an unexpected response")
    name = str(payload.get("name") or payload.get("handle") or canonical)[:300]
    events = [item for item in payload.get("events", [])[:20] if isinstance(item, dict)] if isinstance(payload.get("events"), list) else []
    entities = []
    for item in payload.get("entities", [])[:20] if isinstance(payload.get("entities"), list) else []:
        if isinstance(item, dict):
            entities.append({"handle": str(item.get("handle") or "")[:100], "roles": [str(role)[:50] for role in item.get("roles", [])[:10]] if isinstance(item.get("roles"), list) else []})
    return InfrastructureResult(
        provider="ARIN RDAP",
        resource=canonical,
        finding_type="asn_registration" if kind == "asn" else "ip_registration",
        title=f"ARIN RDAP · {name}",
        description=str(payload.get("remarks") or "")[:500] or None,
        canonical_url=url,
        metadata={"resource": canonical, "resource_kind": kind, "handle": payload.get("handle"), "name": payload.get("name"), "start_address": payload.get("startAddress"), "end_address": payload.get("endAddress"), "country": payload.get("country"), "events": events, "entities": entities, "source_scope": "arin_rdap"},
    )


async def enrich_abuseipdb(resource: str, api_key: str) -> InfrastructureResult:
    canonical, kind = normalise_resource(resource)
    if kind != "ip":
        raise InfrastructureEnrichmentError("AbuseIPDB accepts public IP addresses only")
    url = "https://api.abuseipdb.com/api/v2/check"
    async with safe_client(timeout=15.0, user_agent="Corvus/0.1 AbuseIPDB enrichment") as client:
        response = await client.send(
            client.build_request(
                "GET",
                url,
                params={"ipAddress": canonical, "maxAgeInDays": "90"},
                headers={"Accept": "application/json", "Key": api_key},
            ),
            stream=True,
        )
        try:
            if response.status_code == 429:
                raise InfrastructureEnrichmentError("AbuseIPDB rate limit was exhausted")
            if response.status_code in {401, 403}:
                raise InfrastructureEnrichmentError("AbuseIPDB did not accept the configured API key")
            response.raise_for_status()
            raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
        finally:
            await response.aclose()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InfrastructureEnrichmentError("AbuseIPDB returned malformed JSON") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise InfrastructureEnrichmentError("AbuseIPDB returned an unexpected response")
    score = data.get("abuseConfidenceScore")
    reports = data.get("totalReports")
    score_text = f"abuse confidence {score}%" if isinstance(score, int) else "abuse confidence unavailable"
    report_text = f"{reports} report(s) in the retained window" if isinstance(reports, int) else "report count unavailable"
    return InfrastructureResult(
        provider="AbuseIPDB",
        resource=canonical,
        finding_type="ip_abuse_reputation",
        title=f"AbuseIPDB · {canonical}",
        description=f"{score_text}; {report_text}",
        canonical_url=f"https://www.abuseipdb.com/check/{quote(canonical, safe='')}",
        metadata={
            "resource": canonical,
            "resource_kind": "ip",
            "abuse_confidence_score": score,
            "total_reports": reports,
            "last_reported_at": data.get("lastReportedAt"),
            "country_code": data.get("countryCode"),
            "usage_type": data.get("usageType"),
            "isp": data.get("isp"),
            "domain": data.get("domain"),
            "is_tor": data.get("isTor"),
            "is_whitelisted": data.get("isWhitelisted"),
            "max_age_days": 90,
            "source_scope": "abuseipdb_check_api_no_verbose_report_comments",
        },
    )


async def enrich_infrastructure(
    provider: str,
    resource: str,
    *,
    abuseipdb_api_key: str | None = None,
) -> InfrastructureResult:
    if provider == "ripe":
        return await enrich_ripe(resource)
    if provider == "arin":
        return await enrich_arin(resource)
    if provider == "abuseipdb":
        if not abuseipdb_api_key:
            raise InfrastructureEnrichmentError("configure an AbuseIPDB API key in Settings first")
        return await enrich_abuseipdb(resource, abuseipdb_api_key)
    raise InfrastructureEnrichmentError("unsupported infrastructure provider")
