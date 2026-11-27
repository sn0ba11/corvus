from urllib.parse import parse_qs

import httpx
import pytest

from app.services.search_searxng_capture import SearxngCaptureError, search_via_searxng


@pytest.mark.asyncio
async def test_search_via_searxng_applies_explicit_engine_selection_and_normalises_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        params = parse_qs(request.url.query.decode())
        assert params["q"][0] == '"johnsmith" site:github.com'
        assert params["engines"][0] == "brave,mojeek"
        assert params["format"][0] == "json"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://github.com/johnsmith",
                        "title": "  John   Smith  ",
                        "content": "Public GitHub profile",
                        "engines": ["brave"],
                    },
                    {"url": "http://127.0.0.1/private", "title": "Rejected"},
                ]
            },
        )

    items, rejected = await search_via_searxng(
        '"johnsmith" site:github.com',
        base_url="http://searxng.test",
        active_engines=("brave", "mojeek"),
        transport=httpx.MockTransport(handler),
    )

    assert [item.url for item in items] == ["https://github.com/johnsmith"]
    assert items[0].title == "John Smith"
    assert items[0].snippet == "Public GitHub profile"
    assert rejected == 1


@pytest.mark.asyncio
async def test_search_via_searxng_dedupes_and_caps_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        results = [
            {"url": "https://example.org/same", "title": "Dup A"},
            {"url": "https://example.org/same", "title": "Dup B"},
            *[
                {"url": f"https://example.org/result-{index}", "title": f"Result {index}"}
                for index in range(60)
            ],
        ]
        return httpx.Response(200, json={"results": results})

    items, _ = await search_via_searxng(
        "example",
        base_url="http://searxng.test",
        active_engines=("brave",),
        transport=httpx.MockTransport(handler),
    )

    assert len(items) == 50
    assert items[0].url == "https://example.org/same"
    assert items[0].title == "Dup A"


@pytest.mark.asyncio
async def test_search_via_searxng_raises_on_rate_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    with pytest.raises(SearxngCaptureError, match="rate limited"):
        await search_via_searxng(
            "example",
            base_url="http://searxng.test",
            active_engines=("brave",),
            transport=httpx.MockTransport(handler),
        )


@pytest.mark.asyncio
async def test_search_via_searxng_raises_on_oversized_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (3 * 1024 * 1024))

    with pytest.raises(SearxngCaptureError, match="size limit"):
        await search_via_searxng(
            "example",
            base_url="http://searxng.test",
            active_engines=("brave",),
            transport=httpx.MockTransport(handler),
        )


@pytest.mark.asyncio
async def test_search_via_searxng_raises_on_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(SearxngCaptureError, match="HTTP 503"):
        await search_via_searxng(
            "example",
            base_url="http://searxng.test",
            active_engines=("brave",),
            transport=httpx.MockTransport(handler),
        )
