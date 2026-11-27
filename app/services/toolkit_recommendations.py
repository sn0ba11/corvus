from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit

from app.models.db import Investigation

MAX_ARCHIVE_TODAY_RECOMMENDATIONS = 8


def _public_source_url(value: str | None) -> str | None:
    if not value or len(value) > 2048:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    host = parsed.hostname.casefold().rstrip(".")
    if host == "localhost" or "." not in host or host.endswith((".local", ".internal", ".lan")):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            return None
    return value


@dataclass(frozen=True, slots=True)
class ToolkitRecommendation:
    key: str
    label: str
    category: str
    reason: str
    destination_url: str
    destination_host: str
    sends_case_value: bool
    finding_id: str | None = None
    search_value: str | None = None
    jurisdiction: str | None = None
    provenance: str | None = None


def _static_recommendations(investigation: Investigation) -> list[ToolkitRecommendation]:
    value = investigation.query.strip()
    encoded = urlencode({"q": f'"{value}"'})
    recommendations: list[ToolkitRecommendation] = []
    if investigation.query_type in {"name", "domain"}:
        recommendations.extend(
            [
                ToolkitRecommendation(
                    "cro-core",
                    "Irish CRO / CORE",
                    "Official company register",
                    "Search Irish companies, business names, limited partnerships or register numbers.",
                    "https://core.cro.ie/",
                    "core.cro.ie",
                    False,
                    search_value=value or None,
                    jurisdiction="Ireland",
                    provenance="investigation.query",
                ),
                ToolkitRecommendation(
                    "companies-house",
                    "UK Companies House",
                    "Official company register",
                    "Search the UK public register by company name, number or officer name.",
                    "https://find-and-update.company-information.service.gov.uk/",
                    "find-and-update.company-information.service.gov.uk",
                    False,
                    search_value=value or None,
                    jurisdiction="United Kingdom",
                    provenance="investigation.query",
                ),
                ToolkitRecommendation(
                    "eu-bris",
                    "EU BRIS company search",
                    "Official company register",
                    "Search interconnected EU/EEA national business registers and retrieve source-register details.",
                    "https://e-justice.europa.eu/topics/registers-business-insolvency-land/business-registers-search-company-eu/general-information-find-company_en",
                    "e-justice.europa.eu",
                    False,
                    search_value=value or None,
                    jurisdiction="EU / EEA",
                    provenance="investigation.query",
                ),
            ]
        )
    if investigation.query_type in {"name", "domain"} and value:
        recommendations.append(
            ToolkitRecommendation(
                "sec-edgar",
                "SEC EDGAR full-text search",
                "Official filings",
                "Search public SEC filings for company, filer or officer/director mentions. The case value is sent only when you launch this recommendation.",
                f"https://www.sec.gov/edgar/search/?{encoded}",
                "www.sec.gov",
                True,
                search_value=value,
                jurisdiction="United States",
                provenance="investigation.query",
            )
        )
    if investigation.query_type == "domain" and value:
        recommendations.append(
            ToolkitRecommendation(
                "urlscan-search",
                "urlscan.io historical scans",
                "Explicit infrastructure enrichment",
                "Open the urlscan.io historical-scan search for this domain. Corvus also exposes a manual native URLScan historical collector when an analyst API key is configured; this browser handoff remains a useful quota-free review path.",
                f"https://urlscan.io/search/#{quote('domain:' + value, safe=':.*-_')}",
                "urlscan.io",
                True,
                search_value=value,
                provenance="investigation.query",
            )
        )
    return recommendations


def toolkit_recommendations(investigation: Investigation) -> list[ToolkitRecommendation]:
    recommendations = _static_recommendations(investigation)
    archive_count = 0
    for finding in investigation.findings:
        if archive_count >= MAX_ARCHIVE_TODAY_RECOMMENDATIONS:
            break
        if finding.status not in {"found", "likely"}:
            continue
        public_url = _public_source_url(finding.canonical_url)
        if public_url is None:
            continue
        recommendations.append(
            ToolkitRecommendation(
                key=f"archive-today:{finding.id}",
                label=f"Archive.today · {finding.platform or 'public page'}",
                category="Manual web archive",
                reason=(
                    "Analyst-approved secondary archive handoff for this public finding. "
                    "Launching sends the source URL to Archive.today and may begin a capture."
                ),
                destination_url=(
                    "https://archive.today/?run=1&url=" + quote(public_url, safe="")
                ),
                destination_host="archive.today",
                sends_case_value=True,
                finding_id=finding.id,
                search_value=public_url,
                provenance=f"finding:{finding.id}.canonical_url",
            )
        )
        archive_count += 1
    return recommendations


def find_toolkit_recommendation(
    investigation: Investigation, key: str
) -> ToolkitRecommendation | None:
    return next((item for item in toolkit_recommendations(investigation) if item.key == key), None)
