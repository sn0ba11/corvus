import json
from pathlib import Path

import httpx
import pytest

from app.adapters.domain_rdap import DomainRDAPAdapter, _authoritative_rdap_base
from app.models.schemas import FindingStatus, QueryType


def test_rdap_bootstrap_selects_https_service_for_tld() -> None:
    payload = {
        "services": [
            [["com", "net"], ["https://rdap.example-registry.test/"]],
            [["org"], ["http://unsafe.example/"]],
        ]
    }
    assert _authoritative_rdap_base(payload, "com") == "https://rdap.example-registry.test/"


@pytest.mark.asyncio
async def test_domain_rdap_uses_iana_bootstrap_and_retains_registration_provenance(tmp_path: Path) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "data.iana.org":
            return httpx.Response(
                200,
                json={"services": [[["com"], ["https://rdap.example-registry.test/"]]]},
            )
        assert request.url.host == "rdap.example-registry.test"
        assert request.url.path == "/domain/example.com"
        return httpx.Response(
            200,
            json={
                "handle": "EXAMPLE-1",
                "ldhName": "EXAMPLE.COM",
                "status": ["active"],
                "events": [
                    {"eventAction": "registration", "eventDate": "1995-08-14T04:00:00Z"},
                    {"eventAction": "last changed", "eventDate": "2026-01-01T00:00:00Z"},
                ],
                "entities": [{"handle": "REG-1", "roles": ["registrar"]}],
                "nameservers": [{"ldhName": "A.IANA-SERVERS.NET"}, {"ldhName": "B.IANA-SERVERS.NET"}],
            },
            headers={"content-type": "application/rdap+json"},
        )

    adapter = DomainRDAPAdapter(httpx.MockTransport(handler))
    result = await adapter.execute(
        adapter.build_command("example.com", QueryType.DOMAIN, tmp_path, 30),
        tmp_path,
        30,
        "example.com",
    )
    assert result.returncode == 0
    assert len(requests) == 2
    payload = json.loads((tmp_path / "domain-rdap.json").read_text(encoding="utf-8"))
    assert payload["handle"] == "EXAMPLE-1"
    assert payload["nameservers"] == ["A.IANA-SERVERS.NET", "B.IANA-SERVERS.NET"]

    findings = adapter.normalise_results(adapter.parse_results(result, tmp_path), "example.com", QueryType.DOMAIN, "raw/ref")
    assert findings[0].finding_type == "domain_registration"
    assert findings[0].status == FindingStatus.FOUND
    assert findings[0].metadata["events"][0]["action"] == "registration"
    assert [finding.value for finding in findings[1:]] == ["A.IANA-SERVERS.NET", "B.IANA-SERVERS.NET"]
    assert all(finding.raw_output_reference == "raw/ref" for finding in findings)
