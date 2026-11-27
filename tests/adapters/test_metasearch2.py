import json
from pathlib import Path

import httpx
import pytest

from app.adapters.metasearch2 import Metasearch2Adapter
from app.models.schemas import FindingStatus, QueryType


@pytest.mark.asyncio
async def test_metasearch2_username_search_filters_noise_and_preserves_provenance(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, text="metasearch")
        assert request.url.path == "/search"
        assert request.url.params["engines"] == "google,bing,brave,marginalia"
        return httpx.Response(
            200,
            json=[
                {
                    "search_results": [
                        {
                            "result": {
                                "url": "https://example.org/users/example_person",
                                "title": "example_person public profile",
                                "description": "Public account for example_person.",
                            },
                            "engines": ["Brave", "Marginalia"],
                            "score": 1.25,
                        },
                        {
                            "result": {
                                "url": "https://example.org/unrelated",
                                "title": "Unrelated page",
                                "description": "No target identifier is present.",
                            },
                            "engines": ["Bing"],
                            "score": 1.0,
                        },
                    ],
                    "featured_snippet": None,
                    "answer": None,
                    "infobox": None,
                }
            ],
        )

    adapter = Metasearch2Adapter(
        transport=httpx.MockTransport(handler), base_url="http://metasearch.test"
    )
    health = await adapter.healthcheck()
    command = adapter.build_command("example_person", QueryType.USERNAME, tmp_path, 90)
    logs: list[tuple[str, str]] = []

    async def log(channel: str, line: str) -> None:
        logs.append((channel, line))

    result = await adapter.execute(command, tmp_path, 90, "example_person", log)
    records = adapter.parse_results(result, tmp_path)
    findings = adapter.normalise_results(records, "example_person", QueryType.USERNAME, "raw")

    assert health.healthy is True
    assert result.returncode == 0
    assert len(records) == 1
    assert len(findings) == 1
    assert findings[0].status == FindingStatus.LIKELY
    assert findings[0].metadata["search_engines"] == ["Brave", "Marginalia"]
    assert len(logs) == 5
    payload = json.loads((tmp_path / "metasearch2.json").read_text(encoding="utf-8"))
    assert payload["rejected_irrelevant_count"] == 5
    assert len(payload["queries"]) == 5


def test_metasearch2_rejects_unsupported_query_type(tmp_path: Path) -> None:
    adapter = Metasearch2Adapter(base_url="http://metasearch.test")
    with pytest.raises(ValueError, match="name or username"):
        adapter.build_command("example.org", QueryType.DOMAIN, tmp_path, 60)
