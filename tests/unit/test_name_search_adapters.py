import json
from pathlib import Path

from app.adapters.base import ExecutionResult
from app.adapters.bing_name import BingNameSearchAdapter, name_result_is_relevant, parse_bing_rss
from app.adapters.searxng_name import SearXNGNameSearchAdapter, normalise_searxng_result
from app.models.schemas import QueryType


def test_bing_rss_parsing_rejects_private_and_bing_urls() -> None:
    xml = """<rss><channel>
    <item><title>Nova Example profile</title><link>https://example.com/nova-example</link><description>Public profile</description></item>
    <item><title>Internal</title><link>http://127.0.0.1/private</link></item>
    <item><title>Bing</title><link>https://www.bing.com/search?q=test</link></item>
    </channel></rss>"""
    records = parse_bing_rss(xml)
    assert records == [
        {
            "url": "https://example.com/nova-example",
            "title": "Nova Example profile",
            "snippet": "Public profile",
            "published_at": None,
        }
    ]
    assert name_result_is_relevant("Nova Example", records[0]) is True
    assert name_result_is_relevant("Nova Example", {"title": "Unrelated", "snippet": "noise", "url": "https://example.org"}) is False


def test_searxng_result_normalisation_bounds_fields() -> None:
    record = normalise_searxng_result(
        {
            "url": "https://example.com/profile",
            "title": "  Nova   Example  ",
            "content": "Public profile",
            "engines": ["duckduckgo", "bing", "bing"],
            "positions": [2, 1, "bad"],
            "score": 1.5,
            "category": "general",
        }
    )
    assert record is not None
    assert record["title"] == "Nova Example"
    assert record["engines"] == ["bing", "duckduckgo"]
    assert record["positions"] == [2, 1]
    assert normalise_searxng_result({"url": "http://localhost/profile"}) is None


def test_name_adapters_parse_and_normalise_retained_json(tmp_path: Path) -> None:
    payload = {
        "results": [
            {
                "url": "https://example.com/nova-example",
                "title": "Nova Example",
                "snippet": "Public researcher profile",
                "query_matches": [{"id": "exact", "label": "Exact", "query": '"Nova Example"'}],
                "engines": ["bing"],
            }
        ]
    }
    for adapter, filename in (
        (BingNameSearchAdapter(), "bing-name-search.json"),
        (SearXNGNameSearchAdapter(base_url="http://searxng:8080"), "searxng-name-search.json"),
    ):
        (tmp_path / filename).write_text(json.dumps(payload), encoding="utf-8")
        result = ExecutionResult(0, "{}", "")
        parsed = adapter.parse_results(result, tmp_path)
        findings = adapter.normalise_results(parsed, "Nova Example", QueryType.NAME, str(tmp_path / "process-output.txt"))
        assert len(findings) == 1
        assert findings[0].canonical_url == "https://example.com/nova-example"
        assert findings[0].title == "Nova Example"
        (tmp_path / filename).unlink()
