import json
from pathlib import Path

import httpx
import pytest

from app.adapters.duckduckgo_instant import DuckDuckGoInstantAdapter
from app.models.schemas import FindingStatus, QueryType


@pytest.mark.asyncio
async def test_duckduckgo_instant_is_explicit_and_retains_bounded_answer(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.duckduckgo.com"
        assert request.url.params["q"] == "Example Company"
        return httpx.Response(200, headers={"Content-Type": "application/json"}, json={
            "Heading": "Example Company",
            "AbstractText": "Example Company is an example organisation.",
            "AbstractURL": "https://example.org/reference",
            "AbstractSource": "Example Source",
            "RelatedTopics": [{"Text": "Example topic", "FirstURL": "https://example.org/topic"}],
        })

    adapter = DuckDuckGoInstantAdapter(httpx.MockTransport(handler))
    assert adapter.default_selected is False
    command = adapter.build_command("Example Company", QueryType.NAME, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "Example Company")
    assert result.returncode == 0
    records = adapter.parse_results(result, tmp_path)
    findings = adapter.normalise_results(records, "Example Company", QueryType.NAME, "raw")
    assert findings[0].status == FindingStatus.LIKELY
    assert findings[0].canonical_url == "https://example.org/reference"
    retained = json.loads((tmp_path / "duckduckgo-instant.json").read_text())
    assert len(retained["related_topics"]) == 1
