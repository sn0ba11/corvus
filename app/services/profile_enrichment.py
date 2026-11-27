import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

Resolver = Callable[[str, int], Awaitable[list[str]]]


class ProfileEnrichmentError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    source_url: str
    observed_at: datetime
    title: str | None
    description: str | None
    canonical_url: str | None
    avatar_url: str | None

    def metadata(self) -> dict[str, str | None]:
        return {
            "source_url": self.source_url,
            "observed_at": self.observed_at.astimezone(UTC).isoformat(),
            "title": self.title,
            "description": self.description,
            "canonical_url": self.canonical_url,
            "avatar_url": self.avatar_url,
        }


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.canonical_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value for key, value in attrs if value is not None}
        if tag.casefold() == "title":
            self.in_title = True
            return
        if tag.casefold() == "meta":
            key = values.get("property") or values.get("name")
            content = values.get("content")
            if key and content:
                self.meta[key.casefold()] = content.strip()
            return
        if tag.casefold() == "link" and values.get("rel", "").casefold() == "canonical":
            self.canonical_url = values.get("href")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)

    def result(self, source_url: str) -> EnrichmentResult:
        title = self.meta.get("og:title") or " ".join(self.title_parts).strip() or None
        description = self.meta.get("og:description") or self.meta.get("description")
        canonical = self.meta.get("og:url") or self.canonical_url
        avatar = self.meta.get("og:image") or self.meta.get("twitter:image")
        return EnrichmentResult(
            source_url=source_url,
            observed_at=datetime.now(UTC),
            title=title[:500] if title else None,
            description=description[:2000] if description else None,
            canonical_url=_absolute_http_url(source_url, canonical),
            avatar_url=_absolute_http_url(source_url, avatar),
        )


def _absolute_http_url(source_url: str, value: str | None) -> str | None:
    if not value:
        return None
    absolute = urljoin(source_url, value)
    parsed = urlsplit(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return absolute


async def resolve_host(host: str, port: int) -> list[str]:
    records = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    return sorted({str(record[4][0]) for record in records})


def _validate_url(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ProfileEnrichmentError("only HTTP(S) profile URLs are supported")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ProfileEnrichmentError("invalid profile URL")
    return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def _validate_public_addresses(addresses: list[str]) -> None:
    if not addresses:
        raise ProfileEnrichmentError("profile host did not resolve")
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise ProfileEnrichmentError("private or non-routable profile host is blocked")


class ProfileEnricher:
    def __init__(
        self,
        *,
        max_bytes: int = 1024 * 1024,
        minimum_interval_seconds: float = 1.0,
        respect_robots: bool = True,
        resolver: Resolver = resolve_host,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.max_bytes = max_bytes
        self.minimum_interval_seconds = minimum_interval_seconds
        self.respect_robots = respect_robots
        self.resolver = resolver
        self.client = client
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._robots: dict[str, RobotFileParser | None] = {}

    async def fetch(self, url: str) -> EnrichmentResult:
        host, port = _validate_url(url)
        if self.client is not None:
            _validate_public_addresses(await self.resolver(host, port))
        owns_client = self.client is None
        client = self.client or safe_client(
            timeout=httpx.Timeout(10.0),
            user_agent="OSINT-Workbench/1 (+local authorised research)",
            resolver=self.resolver,
        )
        try:
            if self.respect_robots:
                await self._enforce_robots(url, client)
            async with self._lock:
                response, content = await self._request(
                    client,
                    url,
                    accept="text/html,application/xhtml+xml",
                    max_bytes=self.max_bytes,
                )
            if response.is_redirect:
                raise ProfileEnrichmentError("profile redirects are blocked")
            if response.status_code in {401, 403}:
                raise ProfileEnrichmentError("profile requires authentication or denies access")
            response.raise_for_status()
            mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if mime_type not in {"text/html", "application/xhtml+xml"}:
                raise ProfileEnrichmentError("profile response is not HTML")
        except EgressPolicyError as exc:
            if "public, routable address" in str(exc):
                raise ProfileEnrichmentError("private or non-routable profile host is blocked") from exc
            if "exceeds" in str(exc):
                raise ProfileEnrichmentError("profile page exceeds maximum size") from exc
            raise ProfileEnrichmentError(f"profile retrieval blocked: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProfileEnrichmentError(f"profile retrieval failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

        parser = _MetadataParser()
        try:
            parser.feed(content.decode(response.encoding or "utf-8", errors="replace"))
            parser.close()
        except ValueError as exc:
            raise ProfileEnrichmentError("profile HTML could not be parsed") from exc
        return parser.result(url)

    async def _request(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        accept: str,
        max_bytes: int,
    ) -> tuple[httpx.Response, bytes]:
        now = asyncio.get_running_loop().time()
        delay = self.minimum_interval_seconds - (now - self._last_request_at)
        if delay > 0:
            await asyncio.sleep(delay)
        request = client.build_request(
            "GET",
            url,
            headers={
                "Accept": accept,
                "User-Agent": "OSINT-Workbench/1 (+local authorised research)",
            },
        )
        response = await client.send(request, stream=True)
        try:
            content = await read_bounded_response(response, max_bytes=max_bytes)
        finally:
            await response.aclose()
        self._last_request_at = asyncio.get_running_loop().time()
        return response, content

    async def _enforce_robots(self, url: str, client: httpx.AsyncClient) -> None:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            robots_url = f"{origin}/robots.txt"
            async with self._lock:
                response, content = await self._request(
                    client,
                    robots_url,
                    accept="text/plain",
                    max_bytes=256 * 1024,
                )
            if response.is_redirect:
                raise ProfileEnrichmentError("robots.txt redirects are blocked")
            if response.status_code == 404:
                self._robots[origin] = None
            elif response.status_code in {401, 403}:
                raise ProfileEnrichmentError("robots.txt denies automated access")
            elif response.status_code >= 500:
                raise ProfileEnrichmentError("robots.txt is temporarily unavailable")
            else:
                response.raise_for_status()
                policy = RobotFileParser()
                policy.set_url(robots_url)
                policy.parse(content.decode(response.encoding or "utf-8", errors="replace").splitlines())
                self._robots[origin] = policy
        cached_policy: RobotFileParser | None = self._robots[origin]
        if cached_policy is not None and not cached_policy.can_fetch("OSINT-Workbench", url):
            raise ProfileEnrichmentError("robots.txt disallows profile enrichment")
