import json
from pathlib import Path

import httpx
import pytest

from app.adapters.domain_relationships import DomainRelationshipAdapter, _decode_txt_data
from app.models.schemas import FindingStatus, QueryType


def _txt_response(value: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "Status": 0,
            "Answer": [{"name": "example.test.", "type": 16, "TTL": 300, "data": json.dumps(value)}],
        },
    )


def test_decode_txt_data_reassembles_dns_txt_chunks() -> None:
    assert _decode_txt_data('"v=spf1 include:_spf.example.test " "~all"') == (
        "v=spf1 include:_spf.example.test ~all"
    )


@pytest.mark.asyncio
async def test_domain_relationships_collects_bounded_mail_and_ct_relationships(tmp_path: Path) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "dns.google":
            name = request.url.params.get("name")
            if name == "example.com":
                return _txt_response("v=spf1 include:_spf.mailvendor.test ~all")
            if name == "_spf.mailvendor.test":
                return _txt_response("v=spf1 -all")
            if name == "_dmarc.example.com":
                return _txt_response(
                    "v=DMARC1; p=reject; rua=mailto:reports@dmarc.vendor.test; ruf=mailto:forensic@dmarc.vendor.test"
                )
            if name == "_mta-sts.example.com":
                return _txt_response("v=STSv1; id=20260917")
            if name == "default._bimi.example.com":
                return _txt_response(
                    "v=BIMI1; l=https://cdn.brand.test/logo.svg; a=https://vmc.brand.test/vmc.pem"
                )
            return httpx.Response(200, json={"Status": 0})
        if request.url.host == "mta-sts.example.com":
            return httpx.Response(
                200,
                text="version: STSv1\nmode: enforce\nmx: mail.example.com\nmax_age: 86400\n",
                headers={"content-type": "text/plain"},
            )
        if request.url.host == "crt.sh":
            assert request.url.params.get("output") == "json"
            assert request.url.params.get("q") == "%.example.com"
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 42,
                        "serial_number": "abcd",
                        "issuer_name": "CN=Example CA",
                        "common_name": "example.com",
                        "name_value": "example.com\nwww.example.com\n*.api.example.com\noutside.test",
                        "entry_timestamp": "2026-09-17T01:02:03Z",
                        "not_before": "2026-09-01T00:00:00Z",
                        "not_after": "2026-12-01T00:00:00Z",
                    }
                ],
            )
        raise AssertionError(f"unexpected request: {request.url}")

    adapter = DomainRelationshipAdapter(httpx.MockTransport(handler))
    result = await adapter.execute(
        adapter.build_command("example.com", QueryType.DOMAIN, tmp_path, 45),
        tmp_path,
        45,
        "example.com",
    )

    assert result.returncode == 0
    payload = json.loads((tmp_path / "domain-relationships.json").read_text(encoding="utf-8"))
    assert payload["spf"][0]["includes"] == ["_spf.mailvendor.test"]
    assert payload["dmarc"]["policy"] == "reject"
    assert payload["mta_sts"]["policy"]["mx"] == ["mail.example.com"]
    assert payload["bimi"]["logo_url"] == "https://cdn.brand.test/logo.svg"
    assert payload["certificates"][0]["names"] == ["example.com", "www.example.com", "api.example.com"]

    relationship_keys = {
        (item["kind"], item["source"], item["target"]) for item in payload["relationships"]
    }
    assert ("spf_include", "example.com", "_spf.mailvendor.test") in relationship_keys
    assert (
        "dmarc_reporting_address",
        "example.com",
        "reports@dmarc.vendor.test",
    ) in relationship_keys
    assert ("mta_sts_policy_host", "example.com", "mta-sts.example.com") in relationship_keys
    assert ("mta_sts_mx", "example.com", "mail.example.com") in relationship_keys
    assert ("bimi_logo_host", "example.com", "cdn.brand.test") in relationship_keys
    assert ("certificate_san", "example.com", "www.example.com") in relationship_keys
    assert ("certificate_san", "example.com", "api.example.com") in relationship_keys
    assert all("outside.test" not in item for item in requested)

    findings = adapter.normalise_results(
        adapter.parse_results(result, tmp_path),
        "example.com",
        QueryType.DOMAIN,
        "raw/domain-relationships.json",
    )
    assert findings[0].finding_type == "domain_control_posture"
    assert findings[0].status == FindingStatus.FOUND
    assert any(item.finding_type == "certificate_transparency_observation" for item in findings)
    relationship_findings = [item for item in findings if "relationship" in item.metadata]
    assert relationship_findings
    assert any(
        item.metadata["relationship"]["kind"] == "dmarc_reporting_address"
        and item.metadata["relationship"]["target_type"] == "email"
        for item in relationship_findings
    )
    assert all(item.raw_output_reference == "raw/domain-relationships.json" for item in findings)
