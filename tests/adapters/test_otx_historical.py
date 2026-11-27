import json
from pathlib import Path

import httpx
import pytest

from app.adapters.otx_historical import OTXHistoricalURLAdapter, parse_url_list
from app.models.schemas import FindingStatus, QueryType


def test_parse_url_list_rejects_foreign_private_and_duplicate_urls() -> None:
    payload = {
        "url_list": [
            {"url": "https://example.com/admin-old/", "date": "2026-08-01T10:00:00"},
            {"url": "https://sub.example.com/api/v1", "httpcode": 200},
            {"url": "https://other.example/nope"},
            {"url": "http://127.0.0.1/private"},
            {"url": "https://example.com/admin-old/"},
        ]
    }
    records = parse_url_list(payload, "example.com")
    assert [record["url"] for record in records] == [
        "https://example.com/admin-old",
        "https://sub.example.com/api/v1",
    ]


@pytest.mark.asyncio
async def test_otx_executes_bounded_domain_query_and_retains_provenance(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "url_list": [
                    {
                        "url": "https://example.com/staff/alice",
                        "hostname": "example.com",
                        "httpcode": 200,
                        "date": "2026-07-12T08:30:00",
                    },
                    {"url": "https://unrelated.example/nope"},
                ]
            },
            headers={"content-type": "application/json"},
        )

    adapter = OTXHistoricalURLAdapter(httpx.MockTransport(handler))
    assert adapter.default_selected is False
    result = await adapter.execute(
        adapter.build_command("example.com", QueryType.DOMAIN, tmp_path, 45),
        tmp_path,
        45,
        "example.com",
    )

    assert result.returncode == 0
    assert len(requests) == 1
    assert requests[0].url.params["limit"] == "100"
    payload = json.loads((tmp_path / "otx-historical.json").read_text())
    assert payload["source_scope"] == "passive_public_url_observations_no_target_fetch"
    assert len(payload["results"]) == 1

    findings = adapter.normalise_results(
        adapter.parse_results(result, tmp_path),
        "example.com",
        QueryType.DOMAIN,
        "raw/otx",
    )
    assert len(findings) == 1
    assert findings[0].status == FindingStatus.FOUND
    assert findings[0].value == "https://example.com/staff/alice"
    assert findings[0].metadata["otx_observed_at"] == "2026-07-12T08:30:00"
    assert findings[0].raw_output_reference == "raw/otx"


@pytest.mark.asyncio
async def test_otx_rate_limit_is_structured(tmp_path: Path) -> None:
    adapter = OTXHistoricalURLAdapter(httpx.MockTransport(lambda _: httpx.Response(429)))
    result = await adapter.execute(
        adapter.build_command("example.com", QueryType.DOMAIN, tmp_path, 45),
        tmp_path,
        45,
        "example.com",
    )
    assert result.rate_limited is True
    assert result.error_code == "rate_limited"
