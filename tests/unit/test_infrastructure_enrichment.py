import httpx
import pytest

from app.services import infrastructure_enrichment as infra
from app.services.infrastructure_enrichment import InfrastructureEnrichmentError, normalise_resource


def test_normalise_resource_accepts_public_ip_and_asn_only() -> None:
    assert normalise_resource("AS64496") == ("AS64496", "asn")
    assert normalise_resource("8.8.8.8") == ("8.8.8.8", "ip")
    with pytest.raises(InfrastructureEnrichmentError):
        normalise_resource("192.168.1.1")
    with pytest.raises(InfrastructureEnrichmentError):
        normalise_resource("example.org")


@pytest.mark.asyncio
async def test_ripe_enrichment_retains_bounded_registry_provenance(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "stat.ripe.net"
        assert request.url.params["resource"] == "AS64496"
        return httpx.Response(200, json={"data": {"authorities": ["arin"], "query_time": "2026-08-17T09:00:00Z", "records": [[{"key": "aut-num", "value": "AS64496"}, {"key": "as-name", "value": "EXAMPLE"}]]}})

    monkeypatch.setattr(infra, "safe_client", lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False))
    result = await infra.enrich_ripe("AS64496")
    assert result.provider == "RIPEstat"
    assert result.metadata["records"][1] == {"key": "as-name", "value": "EXAMPLE"}
    assert result.metadata["source_scope"] == "ripe_stat_whois_api"


@pytest.mark.asyncio
async def test_arin_enrichment_retains_rdap_fields(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "rdap.arin.net"
        return httpx.Response(200, json={"handle": "NET-8-8-8-0-1", "name": "GOGL", "startAddress": "8.8.8.0", "endAddress": "8.8.8.255", "country": "US", "events": [{"eventAction": "registration", "eventDate": "1992-12-01T00:00:00Z"}], "entities": [{"handle": "GOGL", "roles": ["registrant"]}]})

    monkeypatch.setattr(infra, "safe_client", lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False))
    result = await infra.enrich_arin("8.8.8.8")
    assert result.provider == "ARIN RDAP"
    assert result.metadata["country"] == "US"
    assert result.metadata["entities"][0]["roles"] == ["registrant"]


@pytest.mark.asyncio
async def test_abuseipdb_enrichment_retains_reputation_without_verbose_reports(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.abuseipdb.com"
        assert request.url.path == "/api/v2/check"
        assert request.url.params["ipAddress"] == "8.8.8.8"
        assert request.url.params["maxAgeInDays"] == "90"
        assert "verbose" not in request.url.params
        assert request.headers["key"] == "api_key_1234567890"
        return httpx.Response(200, json={"data": {"ipAddress": "8.8.8.8", "abuseConfidenceScore": 7, "totalReports": 3, "lastReportedAt": "2026-09-10T10:00:00Z", "countryCode": "US", "usageType": "Data Center/Web Hosting/Transit", "isp": "Example ISP", "domain": "example.net", "isTor": False, "isWhitelisted": False}})

    monkeypatch.setattr(infra, "safe_client", lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False))
    result = await infra.enrich_infrastructure("abuseipdb", "8.8.8.8", abuseipdb_api_key="api_key_1234567890")
    assert result.provider == "AbuseIPDB"
    assert result.metadata["abuse_confidence_score"] == 7
    assert result.metadata["source_scope"] == "abuseipdb_check_api_no_verbose_report_comments"
    with pytest.raises(InfrastructureEnrichmentError):
        await infra.enrich_infrastructure("abuseipdb", "AS15169", abuseipdb_api_key="api_key_1234567890")
