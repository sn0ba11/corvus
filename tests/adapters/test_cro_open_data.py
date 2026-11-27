import json
from pathlib import Path

import httpx
import pytest

from app.adapters.cro_open_data import RESOURCE_ID, CROOpenDataAdapter
from app.models.schemas import FindingStatus, QueryType


@pytest.mark.asyncio
async def test_cro_open_data_retains_company_snapshot_and_provenance(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "opendata.cro.ie"
        assert request.url.params["resource_id"] == RESOURCE_ID
        assert request.url.params["q"] == "Google Payment Ireland Limited"
        assert request.url.params["limit"] == "20"
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "total": 1,
                    "records": [
                        {
                            "company_num": 598776,
                            "company_name": "GOOGLE PAYMENT IRELAND LIMITED",
                            "company_status": "Normal",
                            "company_type": "LTD - Private Company Limited by Shares",
                            "company_reg_date": "2017-02-17T00:00:00",
                            "company_address_1": "70 SIR JOHN ROGERSON'S QUAY",
                            "company_address_2": "DUBLIN 2",
                            "eircode": "D02R296",
                            "last_ar_date": "2025-09-30T00:00:00",
                            "nard": "2026-09-30T00:00:00",
                        }
                    ],
                },
            },
        )

    adapter = CROOpenDataAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("Google Payment Ireland Limited", QueryType.NAME, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "Google Payment Ireland Limited")
    findings = adapter.normalise_results(
        adapter.parse_results(result, tmp_path),
        "Google Payment Ireland Limited",
        QueryType.NAME,
        "cro-open-data.json",
    )
    assert result.returncode == 0
    assert len(findings) == 1
    assert findings[0].status == FindingStatus.FOUND
    assert findings[0].metadata["company_number"] == "598776"
    assert findings[0].metadata["licence"] == "CC-BY-4.0"
    payload = json.loads((tmp_path / "cro-open-data.json").read_text())
    assert payload["source_resource_id"] == RESOURCE_ID
