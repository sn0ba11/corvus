import json
from pathlib import Path

import httpx
import pytest

from app.adapters.commoncrawl import CommonCrawlAdapter, _latest_collection, parse_index_jsonl
from app.models.schemas import FindingStatus, QueryType


def test_latest_collection_accepts_only_official_https_index() -> None:
    selected = _latest_collection(
        [
            {"id": "CC-MAIN-2026-20", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-20-index"},
            {"id": "CC-MAIN-2026-30", "cdx-api": "http://index.commoncrawl.org/unsafe"},
            {"id": "CC-MAIN-2026-40", "cdx-api": "https://evil.example/index"},
            {"id": "CC-MAIN-2026-25", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-25-index"},
        ]
    )
    assert selected["id"] == "CC-MAIN-2026-25"


def test_parse_index_jsonl_is_bounded_and_rejects_foreign_or_private_urls() -> None:
    text = "\n".join(
        [
            json.dumps({"url": "https://example.com/a", "timestamp": "20260701010203", "status": "200"}),
            json.dumps({"url": "https://sub.example.com/b", "timestamp": "20260702010203", "status": "200"}),
            json.dumps({"url": "https://other.example/bad", "timestamp": "20260703010203"}),
            json.dumps({"url": "http://127.0.0.1/private", "timestamp": "20260704010203"}),
            "not-json",
        ]
    )
    records = parse_index_jsonl(text, "example.com")
    assert [item["url"] for item in records] == [
        "https://example.com/a",
        "https://sub.example.com/b",
    ]


@pytest.mark.asyncio
async def test_commoncrawl_executes_official_index_query_and_retains_provenance(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/collinfo.json":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "CC-MAIN-2026-30",
                        "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-30-index",
                    }
                ],
            )
        return httpx.Response(
            200,
            text="\n".join(
                [
                    json.dumps(
                        {
                            "url": "https://example.com/profile",
                            "timestamp": "20260715112233",
                            "status": "200",
                            "mime": "text/html",
                            "digest": "sha1:abc",
                            "filename": "crawl-data/file.warc.gz",
                            "offset": "123",
                            "length": "456",
                        }
                    ),
                    json.dumps({"url": "https://unrelated.example/nope", "timestamp": "20260715112234"}),
                ]
            ),
            headers={"content-type": "application/x-ndjson"},
        )

    adapter = CommonCrawlAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("example.com", QueryType.DOMAIN, tmp_path, 60)
    result = await adapter.execute(command, tmp_path, 60, "example.com")

    assert result.returncode == 0
    assert len(requests) == 2
    params = requests[1].url.params
    assert params["url"] == "*.example.com/*"
    assert params["pageSize"] == "100"
    assert params["collapse"] == "urlkey"
    payload = json.loads((tmp_path / "commoncrawl-index.json").read_text())
    assert payload["collection_id"] == "CC-MAIN-2026-30"
    assert len(payload["results"]) == 1

    records = adapter.parse_results(result, tmp_path)
    findings = adapter.normalise_results(records, "example.com", QueryType.DOMAIN, "raw/ref")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.status == FindingStatus.FOUND
    assert finding.value == "https://example.com/profile"
    assert finding.metadata["crawl_observed_at"] == "2026-07-15T11:22:33Z"
    assert finding.metadata["source_scope"] == "index_metadata_only_no_page_download"
    assert finding.raw_output_reference == "raw/ref"


@pytest.mark.asyncio
async def test_commoncrawl_rate_limit_is_visible_without_false_findings(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/collinfo.json":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "CC-MAIN-2026-30",
                        "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-30-index",
                    }
                ],
            )
        return httpx.Response(429)

    adapter = CommonCrawlAdapter(httpx.MockTransport(handler))
    result = await adapter.execute(
        adapter.build_command("example.com", QueryType.DOMAIN, tmp_path, 60),
        tmp_path,
        60,
        "example.com",
    )
    assert result.rate_limited is True
    assert result.error_code == "rate_limited"
    assert adapter.parse_results(result, tmp_path) == []
