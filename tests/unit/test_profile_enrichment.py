import httpx
import pytest

from app.services.profile_enrichment import ProfileEnricher, ProfileEnrichmentError


async def public_resolver(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


async def private_resolver(_host: str, _port: int) -> list[str]:
    return ["127.0.0.1"]


def allow_missing_robots(profile_response: httpx.Response):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        profile_response.request = request
        return profile_response

    return handler


@pytest.mark.asyncio
async def test_extracts_public_profile_metadata() -> None:
    html = """
    <html><head>
      <title>Fallback title</title>
      <meta property="og:title" content="Example Person">
      <meta property="og:description" content="Public biography">
      <meta property="og:image" content="/avatar.jpg">
      <link rel="canonical" href="https://example.com/example">
    </head></html>
    """
    transport = httpx.MockTransport(
        allow_missing_robots(
            httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=html)
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await ProfileEnricher(
            resolver=public_resolver, client=client, minimum_interval_seconds=0
        ).fetch("https://example.com/example")
    assert result.title == "Example Person"
    assert result.description == "Public biography"
    assert result.avatar_url == "https://example.com/avatar.jpg"
    assert result.canonical_url == "https://example.com/example"
    assert result.metadata()["observed_at"]


@pytest.mark.asyncio
async def test_blocks_private_profile_hosts_before_request() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProfileEnrichmentError, match="private or non-routable"):
            await ProfileEnricher(
                resolver=private_resolver, client=client, minimum_interval_seconds=0
            ).fetch("http://localhost/profile")
    assert not called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(302, "redirects are blocked"), (401, "requires authentication"), (403, "denies access")],
)
async def test_rejects_redirects_and_access_restrictions(status_code: int, expected: str) -> None:
    transport = httpx.MockTransport(allow_missing_robots(httpx.Response(status_code)))
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        with pytest.raises(ProfileEnrichmentError, match=expected):
            await ProfileEnricher(
                resolver=public_resolver, client=client, minimum_interval_seconds=0
            ).fetch("https://example.com/profile")


@pytest.mark.asyncio
async def test_rejects_non_html_and_oversized_pages() -> None:
    responses = [
        httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}"),
        httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * 101),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        response = responses.pop(0)
        response.request = request
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        enricher = ProfileEnricher(
            resolver=public_resolver,
            client=client,
            max_bytes=100,
            minimum_interval_seconds=0,
        )
        with pytest.raises(ProfileEnrichmentError, match="not HTML"):
            await enricher.fetch("https://example.com/profile")
        with pytest.raises(ProfileEnrichmentError, match="exceeds maximum size"):
            await enricher.fetch("https://example.com/profile")


@pytest.mark.asyncio
async def test_discards_unsafe_metadata_urls() -> None:
    html = '<meta property="og:image" content="javascript:alert(1)">'
    transport = httpx.MockTransport(
        allow_missing_robots(httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await ProfileEnricher(
            resolver=public_resolver, client=client, minimum_interval_seconds=0
        ).fetch("https://example.com/profile")
    assert result.avatar_url is None


@pytest.mark.asyncio
async def test_respects_robots_disallow_and_caches_policy() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                text="User-agent: OSINT-Workbench\nDisallow: /private\nAllow: /public\n",
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<title>Public</title>",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        enricher = ProfileEnricher(
            resolver=public_resolver, client=client, minimum_interval_seconds=0
        )
        await enricher.fetch("https://example.com/public/profile")
        with pytest.raises(ProfileEnrichmentError, match="robots.txt disallows"):
            await enricher.fetch("https://example.com/private/profile")
    assert requests.count("/robots.txt") == 1
    assert "/private/profile" not in requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("robots_status", "expected"),
    [(403, "denies automated access"), (503, "temporarily unavailable")],
)
async def test_rejects_unavailable_or_denied_robots(robots_status: int, expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(robots_status, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProfileEnrichmentError, match=expected):
            await ProfileEnricher(
                resolver=public_resolver, client=client, minimum_interval_seconds=0
            ).fetch("https://example.com/profile")


@pytest.mark.asyncio
async def test_can_explicitly_ignore_robots_policy() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /", request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<title>Allowed by analyst override</title>",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ProfileEnricher(
            resolver=public_resolver,
            client=client,
            minimum_interval_seconds=0,
            respect_robots=False,
        ).fetch("https://example.com/profile")
    assert result.title == "Allowed by analyst override"
    assert requests == ["/profile"]


@pytest.mark.asyncio
async def test_malformed_robots_without_rules_does_not_block() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="this is not a robots policy", request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<title>Profile</title>",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ProfileEnricher(
            resolver=public_resolver, client=client, minimum_interval_seconds=0
        ).fetch("https://example.com/profile")
    assert result.title == "Profile"
