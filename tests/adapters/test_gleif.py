from pathlib import Path

import httpx
import pytest

from app.adapters.gleif import GLEIFAdapter
from app.models.schemas import FindingStatus, QueryType


@pytest.mark.asyncio
async def test_gleif_is_explicit_and_normalises_official_lei_record(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.gleif.org"
        assert request.url.params["filter[entity.names]"] == "Example Limited"
        return httpx.Response(200, json={"data": [{
            "id": "5493001KJTIIGC8Y1R12",
            "attributes": {
                "lei": "5493001KJTIIGC8Y1R12",
                "entity": {
                    "legalName": {"name": "Example Limited"},
                    "status": "ACTIVE",
                    "jurisdiction": "IE",
                    "legalAddress": {"city": "Dublin", "country": "IE", "addressLines": ["1 Main Street"]},
                },
                "registration": {"status": "ISSUED", "initialRegistrationDate": "2020-01-01T00:00:00Z"},
            },
        }]})

    adapter = GLEIFAdapter(httpx.MockTransport(handler))
    assert adapter.default_selected is False
    command = adapter.build_command("Example Limited", QueryType.NAME, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "Example Limited")
    assert result.returncode == 0
    findings = adapter.normalise_results(adapter.parse_results(result, tmp_path), "Example Limited", QueryType.NAME, "raw")
    assert findings[0].status == FindingStatus.LIKELY
    assert findings[0].value == "Example Limited"
    assert findings[0].source_record_id == "5493001KJTIIGC8Y1R12"
