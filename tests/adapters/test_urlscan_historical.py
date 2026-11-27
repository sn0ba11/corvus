import json
from pathlib import Path

import httpx
import pytest

from app.adapters import urlscan_historical
from app.adapters.urlscan_historical import URLScanHistoricalAdapter, parse_search_results
from app.models.schemas import FindingStatus, QueryType

API_KEY = "A" * 32


def test_parse_search_results_rejects_foreign_private_and_duplicate_urls() -> None:
    payload = {
        "results": [
            {"_id": "scan-1", "task": {"url": "https://example.com/admin", "time": "2026-08-01T10:00:00Z"}, "page": {"url": "https://example.com/admin", "title": "Admin"}},
            {"_id": "scan-2", "task": {"url": "https://sub.example.com/api"}, "page": {"url": "https://sub.example.com/api"}},
            {"_id": "scan-3", "task": {"url": "https://other.example/nope"}},
            {"_id": "scan-4", "task": {"url": "http://127.0.0.1/private"}},
            {"_id": "scan-5", "task": {"url": "https://example.com/admin"}},
        ]
    }
    records = parse_search_results(payload, "example.com")
    assert [record["url"] for record in records] == [
        "https://example.com/admin",
        "https://sub.example.com/api",
    ]


@pytest.mark.asyncio
async def test_urlscan_executes_search_only_and_does_not_persist_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urlscan_historical, "get_urlscan_api_key", lambda _db: API_KEY)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["api-key"] == API_KEY
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "_id": "scan-123",
                        "task": {
                            "url": "https://example.com/old/login",
                            "domain": "example.com",
                            "time": "2026-07-01T09:15:00Z",
                        },
                        "page": {
                            "url": "https://example.com/old/login",
                            "domain": "example.com",
                            "ip": "203.0.113.10",
                            "status": "200",
                            "title": "Old Login",
                        },
                    }
                ]
            },
            headers={"content-type": "application/json"},
        )

    adapter = URLScanHistoricalAdapter(httpx.MockTransport(handler))
    assert adapter.default_selected is False
    result = await adapter.execute(
        adapter.build_command("example.com", QueryType.DOMAIN, tmp_path, 45),
        tmp_path,
        45,
        "example.com",
    )

    assert result.returncode == 0
    assert len(requests) == 1
    assert requests[0].url.params["q"] == "domain:example.com"
    assert requests[0].url.params["size"] == "100"
    retained = (tmp_path / "urlscan-historical.json").read_text(encoding="utf-8")
    assert API_KEY not in retained
    payload = json.loads(retained)
    assert payload["source_scope"] == "historical_scan_metadata_no_new_scan_submission"

    findings = adapter.normalise_results(
        adapter.parse_results(result, tmp_path),
        "example.com",
        QueryType.DOMAIN,
        "raw/urlscan",
    )
    assert len(findings) == 1
    assert findings[0].status == FindingStatus.FOUND
    assert findings[0].value == "https://example.com/old/login"
    assert findings[0].metadata["scan_id"] == "scan-123"
    assert findings[0].raw_output_reference == "raw/urlscan"


@pytest.mark.asyncio
async def test_urlscan_rejected_key_is_structured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urlscan_historical, "get_urlscan_api_key", lambda _db: API_KEY)
    adapter = URLScanHistoricalAdapter(httpx.MockTransport(lambda _: httpx.Response(403)))
    result = await adapter.execute(
        adapter.build_command("example.com", QueryType.DOMAIN, tmp_path, 45),
        tmp_path,
        45,
        "example.com",
    )
    assert result.error_code == "authentication_failed"
