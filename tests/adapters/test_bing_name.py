import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.adapters.bing_name import (
    RSS_USAGE_SCOPE,
    BingNameSearchAdapter,
    build_bing_name_queries,
    name_result_is_relevant,
    parse_bing_rss,
)
from app.models.schemas import FindingStatus, QueryType


def rss(items: list[tuple[str, str, str]]) -> str:
    body = "".join(
        f"<item><title>{title}</title><link>{url}</link>"
        f"<description>{snippet}</description><pubDate>Fri, 31 Jul 2026 13:32:00 GMT</pubDate>"
        "</item>"
        for title, url, snippet in items
    )
    return (
        f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>{body}</channel></rss>'
    )


def test_bing_name_queries_are_bounded_and_include_variants() -> None:
    queries = build_bing_name_queries("William Randolph Hearst")

    assert [item["id"] for item in queries] == [
        "exact",
        "professional",
        "social",
        "documents",
        "variants",
    ]
    assert all(len(item["query"]) <= 900 for item in queries)
    assert '"William Randolph Hearst"' in queries[0]["query"]
    assert '"William R Hearst"' in queries[-1]["query"]
    assert "site:linkedin.com/in" in queries[1]["query"]
    assert "filetype:pdf" in queries[3]["query"]


def test_name_result_relevance_requires_name_evidence() -> None:
    assert name_result_is_relevant(
        "Ada Lovelace",
        {
            "title": "Ada Lovelace | Biography",
            "snippet": "English mathematician and writer",
            "url": "https://example.org/ada-lovelace",
        },
    )
    assert name_result_is_relevant(
        "Ada Lovelace",
        {
            "title": "Lovelace, Ada",
            "snippet": "Catalogue record",
            "url": "https://example.org/catalogue/1",
        },
    )
    assert not name_result_is_relevant(
        "Ada Lovelace",
        {
            "title": "Kinder Happy Hippo Hazelnut Biscuits",
            "snippet": "Five wafer bars with milk and hazelnut filling",
            "url": "https://shop.example/products/happy-hippo",
        },
    )


def test_bing_rss_parser_rejects_entities_and_internal_links() -> None:
    with pytest.raises(ValueError, match="prohibited XML"):
        parse_bing_rss('<!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel /></rss>')
    with pytest.raises(ValueError, match="prohibited XML"):
        parse_bing_rss('<!--' + ' ' * 5000 + '--><!DOCTYPE rss><rss><channel /></rss>')

    records = parse_bing_rss(
        rss(
            [
                ("Profile", "https://example.org/person", "Public &amp; useful"),
                ("Bing", "https://www.bing.com/search?q=person", "Internal"),
                ("Local", "http://127.0.0.1/private", "Private network"),
            ]
        )
    )

    assert records == [
        {
            "url": "https://example.org/person",
            "title": "Profile",
            "snippet": "Public & useful",
            "published_at": "Fri, 31 Jul 2026 13:32:00 GMT",
        }
    ]


@pytest.mark.asyncio
async def test_bing_name_search_collects_deduplicates_and_normalises(tmp_path: Path) -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.bing.com"
        params = parse_qs(request.url.query.decode())
        query = params["q"][0]
        seen_queries.append(query)
        query_id = build_bing_name_queries("Example Person")[len(seen_queries) - 1]["id"]
        return httpx.Response(
            200,
            headers={"content-type": "text/xml; charset=utf-8"},
            text=rss(
                [
                    (
                        "Example Person public profile",
                        "https://profiles.example.org/example-person",
                        "Example Person public biography and profile.",
                    ),
                    (
                        f"Example Person {query_id}",
                        f"https://records.example.net/{query_id}",
                        f"Result returned by the {query_id} query.",
                    ),
                    (
                        "Unrelated shopping result",
                        f"https://shop.example.net/{query_id}",
                        "A product page with no subject-name evidence.",
                    ),
                ]
            ),
        )

    adapter = BingNameSearchAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("Example Person", QueryType.NAME, tmp_path, 90)
    result = await adapter.execute(command, tmp_path, 90, "Example Person")
    records = adapter.parse_results(result, tmp_path)
    findings = adapter.normalise_results(records, "Example Person", QueryType.NAME, "raw")

    assert result.returncode == 0
    assert not result.error_code
    assert len(seen_queries) == 5
    assert len(records) == 6
    assert len(findings) == 6
    assert all(finding.finding_type == "search_result" for finding in findings)
    assert all(finding.status == FindingStatus.LIKELY for finding in findings)
    common = next(
        finding
        for finding in findings
        if finding.canonical_url == "https://profiles.example.org/example-person"
    )
    assert common.title == "Example Person public profile"
    assert common.description == "Example Person public biography and profile."
    assert len(common.metadata["query_matches"]) == 5
    assert all(
        (tmp_path / match["raw_xml"]).is_file() for match in common.metadata["query_matches"]
    )
    assert common.metadata["engine"] == "bing"
    assert common.metadata["usage_scope"] == RSS_USAGE_SCOPE
    assert (tmp_path / "bing-name-search.json").is_file()
    assert len(list(tmp_path.glob("bing-*.xml"))) == 5
    payload = json.loads((tmp_path / "bing-name-search.json").read_text(encoding="utf-8"))
    assert payload["usage_scope"] == RSS_USAGE_SCOPE
    assert len(payload["queries"]) == 5
    assert all(item["returned_count"] == 3 for item in payload["queries"])
    assert all(item["retained_count"] == 2 for item in payload["queries"])
    assert payload["rejected_irrelevant_count"] == 5
    assert len(payload["results"]) == 6


@pytest.mark.asyncio
async def test_bing_name_search_reports_complete_rate_limit(tmp_path: Path) -> None:
    adapter = BingNameSearchAdapter(
        httpx.MockTransport(lambda _: httpx.Response(429, headers={"content-type": "text/xml"}))
    )
    command = adapter.build_command("Example Person", QueryType.NAME, tmp_path, 60)

    result = await adapter.execute(command, tmp_path, 60, "Example Person")

    assert result.rate_limited is True
    assert result.error_code == "rate_limited"
    assert adapter.parse_results(result, tmp_path) == []


def test_bing_name_search_rejects_non_name_query_type(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="full-name"):
        BingNameSearchAdapter().build_command(
            "example_person", QueryType.USERNAME, tmp_path, timeout=60
        )
