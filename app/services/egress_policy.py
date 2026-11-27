from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

import httpx

from app.core.config import get_settings

Resolver = Callable[[str, int], Awaitable[list[str]]]
DEFAULT_USER_AGENT = "Corvus/0.1 (+local authorised research)"


class EgressPolicyError(ValueError):
    """Raised when an outbound request would violate Corvus egress policy."""


class EgressPayloadKind(StrEnum):
    METADATA = "metadata"
    URL_REFERENCE = "url_reference"
    MEDIA = "media"


@dataclass(frozen=True, slots=True)
class EgressDecision:
    destination: str
    payload_kind: EgressPayloadKind
    media_approved: bool


def _normalise_proxy(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _configured_proxy(url: httpx.URL) -> str | None:
    settings = get_settings()
    if url.scheme == "https":
        return _normalise_proxy(settings.https_proxy) or _normalise_proxy(settings.http_proxy)
    if url.scheme == "http":
        return _normalise_proxy(settings.http_proxy)
    return None


def safe_client(
    *,
    timeout: float | httpx.Timeout = 10.0,
    user_agent: str = DEFAULT_USER_AGENT,
    proxy: str | None = None,
    resolver: Resolver | None = None,
) -> httpx.AsyncClient:
    """Create the standard Corvus client for untrusted third-party HTTP(S) targets.

    Redirects and environment-derived proxies are disabled. DNS is resolved and pinned by the
    transport, and an explicitly supplied proxy wins over the configured HTTP(S) proxy.
    """

    transport = PinnedDNSTransport(resolver=resolver, proxy=_normalise_proxy(proxy))
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
        headers={"User-Agent": user_agent},
    )


async def read_bounded_response(response: httpx.Response, *, max_bytes: int) -> bytes:
    """Read a streamed response without ever buffering more than the configured limit."""

    declared = response.headers.get("content-length")
    if declared:
        try:
            if int(declared) > max_bytes:
                raise EgressPolicyError(f"response exceeds the {max_bytes}-byte limit")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise EgressPolicyError(f"response exceeds the {max_bytes}-byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def validate_external_egress(
    destination: str,
    *,
    payload_kind: EgressPayloadKind,
    media_approved: bool = False,
    allowed_hosts: frozenset[str] | None = None,
) -> EgressDecision:
    """Validate one explicit external egress decision.

    External media is denied by default. A caller must opt into media egress for a specific
    request and the destination must still pass the normal public-HTTPS checks. Internal
    service traffic should not use this helper; this policy is for third-party destinations.
    """

    parsed = urlsplit(destination.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise EgressPolicyError("external egress requires an HTTPS destination")
    if parsed.username or parsed.password:
        raise EgressPolicyError("credentials are not allowed in external egress destinations")
    host = parsed.hostname.casefold().rstrip(".")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise EgressPolicyError("external egress destination is not allow-listed")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise EgressPolicyError("external egress cannot target a local hostname")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise EgressPolicyError("external egress requires a public hostname") from None
    else:
        if not address.is_global:
            raise EgressPolicyError("external egress cannot target a private or non-routable address")
    if payload_kind == EgressPayloadKind.MEDIA and not media_approved:
        raise EgressPolicyError(
            "media egress requires an explicit per-request examiner approval"
        )
    return EgressDecision(
        destination=destination,
        payload_kind=payload_kind,
        media_approved=media_approved,
    )


async def _default_resolve(hostname: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        candidate = info[4][0]
        if candidate not in addresses:
            addresses.append(candidate)
    return addresses


def _require_global_address(address: str) -> None:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise EgressPolicyError(f"resolved address is not a valid IP: {address}") from exc
    if not parsed.is_global:
        raise EgressPolicyError(
            f"resolved address {address} is not a public, routable address"
        )


class PinnedDNSTransport(httpx.AsyncBaseTransport):
    """Resolve each request's hostname once, validate it, and connect to that exact address.

    Validating a hostname and then handing the same hostname to the HTTP client for
    connection leaves a window between the two DNS lookups: an attacker-controlled domain
    (and Corvus's threat model is explicit that the investigated subject controls bios,
    avatar URLs and "external site" links Corvus fetches) can answer the validation lookup
    with a public address and the connection lookup, moments later, with a private or
    loopback one. Pinning closes that window by resolving once, rejecting the request if any
    resolved candidate is not a public/global address, and connecting to the validated
    address directly - while still sending the original Host header and TLS SNI so
    certificate validation and virtual hosting keep working normally.
    """

    def __init__(
        self,
        *,
        inner: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        proxy: str | None = None,
    ) -> None:
        self._inner = inner
        self._proxy = _normalise_proxy(proxy)
        self._resolve = resolver if resolver is not None else _default_resolve

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme not in {"http", "https"}:
            raise EgressPolicyError("outbound requests require HTTP(S)")
        if url.username or url.password:
            raise EgressPolicyError("credentials are not allowed in outbound request URLs")
        hostname = url.host
        if not hostname:
            raise EgressPolicyError("outbound request URL has no hostname")
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            _require_global_address(hostname)
            transport = self._inner or httpx.AsyncHTTPTransport(
                proxy=self._proxy or _configured_proxy(url)
            )
            if self._inner is None:
                self._inner = transport
            return await transport.handle_async_request(request)

        port = url.port or (443 if url.scheme == "https" else 80)
        try:
            addresses = await self._resolve(hostname, port)
        except OSError as exc:
            raise EgressPolicyError(f"could not resolve {hostname}") from exc
        if not addresses:
            raise EgressPolicyError(f"could not resolve {hostname}")
        for address in addresses:
            _require_global_address(address)

        pinned = addresses[0]
        request.extensions["sni_hostname"] = hostname
        request.url = url.copy_with(host=pinned)
        transport = self._inner or httpx.AsyncHTTPTransport(
            proxy=self._proxy or _configured_proxy(url)
        )
        if self._inner is None:
            self._inner = transport
        try:
            return await transport.handle_async_request(request)
        finally:
            request.url = url

    async def aclose(self) -> None:
        if self._inner is not None:
            await self._inner.aclose()
