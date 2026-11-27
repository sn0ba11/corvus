import httpx
import pytest

from app.services.egress_policy import (
    EgressPayloadKind,
    EgressPolicyError,
    PinnedDNSTransport,
    _normalise_proxy,
    validate_external_egress,
)


def test_empty_proxy_values_are_treated_as_no_proxy() -> None:
    assert _normalise_proxy(None) is None
    assert _normalise_proxy("") is None
    assert _normalise_proxy("   ") is None
    assert _normalise_proxy("http://127.0.0.1:8080") == "http://127.0.0.1:8080"


def test_media_egress_requires_explicit_per_request_approval() -> None:
    with pytest.raises(EgressPolicyError, match="explicit per-request examiner approval"):
        validate_external_egress(
            "https://lens.google.com/uploadbyurl",
            payload_kind=EgressPayloadKind.MEDIA,
        )
    decision = validate_external_egress(
        "https://lens.google.com/uploadbyurl",
        payload_kind=EgressPayloadKind.MEDIA,
        media_approved=True,
        allowed_hosts=frozenset({"lens.google.com"}),
    )
    assert decision.media_approved is True
    assert decision.payload_kind == EgressPayloadKind.MEDIA


@pytest.mark.asyncio
async def test_pinned_transport_connects_to_resolved_address_and_preserves_identity() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["host"] = request.url.host
        captured["sni_hostname"] = request.extensions.get("sni_hostname")
        captured["host_header"] = request.headers.get("host")
        return httpx.Response(200)

    async def resolver(hostname: str, port: int) -> list[str]:
        assert hostname == "example.org"
        assert port == 443
        return ["93.184.216.34"]

    transport = PinnedDNSTransport(inner=httpx.MockTransport(handler), resolver=resolver)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get("https://example.org/path")

    assert response.status_code == 200
    assert captured["host"] == "93.184.216.34"
    assert captured["sni_hostname"] == "example.org"
    assert captured["host_header"] == "example.org"


@pytest.mark.asyncio
async def test_pinned_transport_rejects_private_resolved_address() -> None:
    async def resolver(hostname: str, port: int) -> list[str]:
        return ["10.0.0.5"]

    transport = PinnedDNSTransport(
        inner=httpx.MockTransport(lambda request: httpx.Response(200)), resolver=resolver
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(EgressPolicyError, match="not a public"):
            await client.get("https://example.org/path")


@pytest.mark.asyncio
async def test_pinned_transport_rejects_if_any_resolved_address_is_private() -> None:
    """A rebinding-style mixed answer is refused outright rather than picking the public one."""

    async def resolver(hostname: str, port: int) -> list[str]:
        return ["93.184.216.34", "127.0.0.1"]

    transport = PinnedDNSTransport(
        inner=httpx.MockTransport(lambda request: httpx.Response(200)), resolver=resolver
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(EgressPolicyError, match="not a public"):
            await client.get("https://example.org/path")


@pytest.mark.asyncio
async def test_pinned_transport_rejects_literal_private_ip_without_resolving() -> None:
    async def resolver(hostname: str, port: int) -> list[str]:
        raise AssertionError("resolver should not be called for a literal IP host")

    transport = PinnedDNSTransport(
        inner=httpx.MockTransport(lambda request: httpx.Response(200)), resolver=resolver
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(EgressPolicyError, match="not a public"):
            await client.get("http://127.0.0.1/admin")


@pytest.mark.asyncio
async def test_pinned_transport_allows_literal_public_ip_without_resolving() -> None:
    async def resolver(hostname: str, port: int) -> list[str]:
        raise AssertionError("resolver should not be called for a literal IP host")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        return httpx.Response(200)

    transport = PinnedDNSTransport(inner=httpx.MockTransport(handler), resolver=resolver)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get("https://93.184.216.34/path")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_pinned_transport_wraps_resolution_failure() -> None:
    async def resolver(hostname: str, port: int) -> list[str]:
        raise OSError("nodename nor servname provided, or not known")

    transport = PinnedDNSTransport(
        inner=httpx.MockTransport(lambda request: httpx.Response(200)), resolver=resolver
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(EgressPolicyError, match="could not resolve"):
            await client.get("https://nonexistent.example/path")


@pytest.mark.asyncio
async def test_pinned_transport_rejects_empty_resolution() -> None:
    async def resolver(hostname: str, port: int) -> list[str]:
        return []

    transport = PinnedDNSTransport(
        inner=httpx.MockTransport(lambda request: httpx.Response(200)), resolver=resolver
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(EgressPolicyError, match="could not resolve"):
            await client.get("https://nonexistent.example/path")


@pytest.mark.asyncio
async def test_pinned_transport_rejects_non_http_scheme() -> None:
    transport = PinnedDNSTransport(
        inner=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    request = httpx.Request("GET", "ftp://example.org/file")
    with pytest.raises(EgressPolicyError, match=r"HTTP\(S\)"):
        await transport.handle_async_request(request)


@pytest.mark.asyncio
async def test_pinned_transport_rejects_embedded_credentials() -> None:
    transport = PinnedDNSTransport(
        inner=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    request = httpx.Request("GET", "https://user:secret@example.org/path")
    with pytest.raises(EgressPolicyError, match="credentials"):
        await transport.handle_async_request(request)


@pytest.mark.asyncio
async def test_pinned_transport_handles_ipv6_resolved_address() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "2606:4700:4700::1111"
        return httpx.Response(200)

    async def resolver(hostname: str, port: int) -> list[str]:
        return ["2606:4700:4700::1111"]

    transport = PinnedDNSTransport(inner=httpx.MockTransport(handler), resolver=resolver)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get("https://example.org/path")

    assert response.status_code == 200
