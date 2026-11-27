from __future__ import annotations

import httpx

from app.adapters.searxng_name import normalise_searxng_result
from app.services.search_imports import SearchResultItem

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESULTS = 50


class SearxngCaptureError(ValueError):
    """Raised when a SearXNG dork-query capture cannot be completed."""


async def search_via_searxng(
    query: str,
    *,
    base_url: str,
    active_engines: tuple[str, ...],
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 20.0,
) -> tuple[list[SearchResultItem], int]:
    """Run one analyst-generated dork query against the self-hosted SearXNG instance.

    Reuses the exact result normalisation already proven in production by
    SearXNGNameSearchAdapter (app/adapters/searxng_name.py), applied here to arbitrary
    query-pack text - including site:/filetype: operators - rather than a fixed preset.
    This is an internal-service call (SearXNG publishes no host port and is not a
    third-party destination), matching that adapter's existing client configuration.
    """
    request_query = query.strip()
    engine_selection = ",".join(active_engines)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "User-Agent": "OSINT-Workbench/0.1",
                "X-Forwarded-For": "127.0.0.1",
                "X-Real-IP": "127.0.0.1",
            },
        ) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/search",
                params={
                    "q": request_query,
                    "engines": engine_selection,
                    "format": "json",
                    "language": "en",
                    "safesearch": "0",
                },
            )
            if response.status_code == 429:
                raise SearxngCaptureError("local SearXNG rate limited this query")
            response.raise_for_status()
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise SearxngCaptureError("SearXNG response exceeded the size limit")
            data = response.json()
    except httpx.HTTPStatusError as exc:
        raise SearxngCaptureError(
            f"local SearXNG returned HTTP {exc.response.status_code}"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SearxngCaptureError(f"local SearXNG query failed: {exc}") from exc

    raw_results = data.get("results", []) if isinstance(data, dict) else []
    if not isinstance(raw_results, list):
        raw_results = []
    items: list[SearchResultItem] = []
    rejected = 0
    seen: set[str] = set()
    for record in raw_results:
        if len(items) >= MAX_RESULTS:
            break
        if not isinstance(record, dict):
            continue
        normalised = normalise_searxng_result(record)
        if normalised is None:
            rejected += 1
            continue
        url = str(normalised["url"])
        if url in seen:
            continue
        seen.add(url)
        items.append(
            SearchResultItem(
                url=url,
                title=normalised.get("title"),
                snippet=normalised.get("snippet"),
            )
        )
    return items, rejected
