import asyncio
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.models.schemas import LocationEvidenceType, LocationObservationInput
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

Resolver = Callable[[str, int], Awaitable[list[str]]]
MAX_LOCATION_PAGE_BYTES = 2 * 1024 * 1024


class LocationCollectionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PlatformPolicy:
    platform: str
    evidence_type: LocationEvidenceType
    default_precision_radius_m: int


POLICIES: tuple[tuple[str, PlatformPolicy], ...] = (
    ("google.com", PlatformPolicy("Google Maps", LocationEvidenceType.REVIEW, 100)),
    ("google.ie", PlatformPolicy("Google Maps", LocationEvidenceType.REVIEW, 100)),
    ("tripadvisor.com", PlatformPolicy("Tripadvisor", LocationEvidenceType.REVIEW, 100)),
    ("tripadvisor.ie", PlatformPolicy("Tripadvisor", LocationEvidenceType.REVIEW, 100)),
    ("yelp.com", PlatformPolicy("Yelp", LocationEvidenceType.REVIEW, 100)),
    ("strava.com", PlatformPolicy("Strava", LocationEvidenceType.FITNESS_ACTIVITY, 1000)),
    ("alltrails.com", PlatformPolicy("AllTrails", LocationEvidenceType.FITNESS_ACTIVITY, 1000)),
    ("komoot.com", PlatformPolicy("Komoot", LocationEvidenceType.FITNESS_ACTIVITY, 1000)),
    (
        "connect.garmin.com",
        PlatformPolicy("Garmin Connect", LocationEvidenceType.FITNESS_ACTIVITY, 1000),
    ),
)


@dataclass(frozen=True, slots=True)
class CollectedLocation:
    payload: LocationObservationInput
    schema_type: str | None


class _StructuredLocationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.in_json_ld = False
        self.json_ld_parts: list[str] = []
        self.json_documents: list[object] = []
        self.visible_text: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value for key, value in attrs if value is not None}
        lowered = tag.casefold()
        if lowered in {"style", "noscript"}:
            self.ignored_depth += 1
        elif lowered == "title":
            self.in_title = True
        elif lowered == "meta":
            key = values.get("property") or values.get("name") or values.get("itemprop")
            content = values.get("content")
            if key and content:
                self.meta[key.casefold()] = content.strip()
        elif lowered == "script":
            script_type = values.get("type", "").split(";", 1)[0].strip().casefold()
            if script_type == "application/ld+json":
                self.in_json_ld = True
                self.json_ld_parts = []
            else:
                self.ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "title":
            self.in_title = False
        elif lowered == "script" and self.in_json_ld:
            raw = "".join(self.json_ld_parts).strip()
            self.in_json_ld = False
            self.json_ld_parts = []
            if raw:
                try:
                    self.json_documents.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
        elif lowered in {"script", "style", "noscript"}:
            self.ignored_depth = max(0, self.ignored_depth - 1)

    def handle_data(self, data: str) -> None:
        if self.in_json_ld:
            self.json_ld_parts.append(data)
            return
        if self.ignored_depth:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        self.visible_text.append(cleaned)
        if self.in_title:
            self.title_parts.append(cleaned)


async def resolve_host(host: str, port: int) -> list[str]:
    records = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    return sorted({str(record[4][0]) for record in records})


def policy_for_url(url: str) -> PlatformPolicy | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if not parsed.hostname:
        return None
    lowered = parsed.hostname.casefold().rstrip(".")
    for suffix, policy in POLICIES:
        if lowered != suffix and not lowered.endswith(f".{suffix}"):
            continue
        if policy.platform == "Google Maps" and not (
            lowered.startswith("maps.") or parsed.path.casefold().startswith("/maps")
        ):
            return None
        return policy
    return None


def _validate_url(url: str) -> tuple[str, int, PlatformPolicy]:
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError as exc:
        raise LocationCollectionError("invalid public location-evidence URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LocationCollectionError("only public HTTP(S) location-evidence URLs are supported")
    if parsed.username or parsed.password:
        raise LocationCollectionError("location-evidence URLs must not contain credentials")
    policy = policy_for_url(url)
    if policy is None:
        raise LocationCollectionError("unsupported review or fitness platform")
    return parsed.hostname, port or (443 if parsed.scheme == "https" else 80), policy


def _validate_public_addresses(addresses: list[str]) -> None:
    if not addresses:
        raise LocationCollectionError("location-evidence host did not resolve")
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise LocationCollectionError(
                "private or non-routable location-evidence host is blocked"
            )


def _types(value: object) -> set[str]:
    if isinstance(value, str):
        return {value.casefold()}
    if isinstance(value, list):
        return {str(item).casefold() for item in value}
    return set()


def _walk_json(value: object) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        try:
            parsed = datetime.strptime(cleaned[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _number(value: object, *, minimum: float, maximum: float) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if minimum <= parsed <= maximum else None


def _country(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name")
        return str(name).strip() if name else None
    return None


def _place_details(value: object) -> dict[str, object] | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return {"place_name": cleaned} if cleaned else None
    if not isinstance(value, dict):
        return None
    address = value.get("address")
    address_data = address if isinstance(address, dict) else {}
    geo = value.get("geo")
    geo_data = geo if isinstance(geo, dict) else {}
    place_name = value.get("name") or address_data.get("name")
    street = address_data.get("streetAddress")
    locality = address_data.get("addressLocality")
    region = address_data.get("addressRegion")
    country = _country(address_data.get("addressCountry"))
    if not place_name:
        place_name = street or locality or region or country
    if not place_name:
        return None
    return {
        "place_name": str(place_name).strip(),
        "locality": str(locality).strip() if locality else None,
        "region": str(region).strip() if region else None,
        "country": country,
        "latitude": _number(geo_data.get("latitude"), minimum=-90, maximum=90),
        "longitude": _number(geo_data.get("longitude"), minimum=-180, maximum=180),
    }


def _location_value(item: dict[str, Any], item_types: set[str]) -> object:
    for key in ("location", "contentLocation", "spatialCoverage"):
        if item.get(key):
            return item[key]
    reviewed = item.get("itemReviewed")
    if reviewed and item_types.intersection({"review", "rating"}):
        return reviewed
    return None


def _occurred_at(item: dict[str, Any], item_types: set[str]) -> datetime | None:
    if item_types.intersection({"review", "rating"}):
        return _parse_datetime(
            item.get("dateVisited") or item.get("visitDate") or item.get("temporalCoverage")
        )
    return _parse_datetime(
        item.get("startDate")
        or item.get("startTime")
        or item.get("contentDate")
        or item.get("dateCreated")
    )


def _account_identifier(item: dict[str, Any]) -> str | None:
    author = item.get("author")
    if isinstance(author, str):
        return author.strip() or None
    if isinstance(author, dict):
        value = author.get("name") or author.get("identifier")
        return str(value).strip() if value else None
    athlete = item.get("agent") or item.get("participant")
    if isinstance(athlete, dict) and athlete.get("name"):
        return str(athlete["name"]).strip()
    return None


def _privacy_masked(parser: _StructuredLocationParser) -> bool:
    text = " ".join(parser.visible_text).casefold()
    markers = (
        "privacy zone",
        "hidden start",
        "hidden end",
        "start and end hidden",
        "approximate location",
        "map visibility",
    )
    return any(marker in text for marker in markers)


def parse_location_page(content: bytes, source_url: str) -> list[CollectedLocation]:
    policy = policy_for_url(source_url)
    if policy is None:
        raise LocationCollectionError("unsupported review or fitness platform")
    parser = _StructuredLocationParser()
    try:
        parser.feed(content.decode("utf-8", errors="replace"))
        parser.close()
    except ValueError as exc:
        raise LocationCollectionError("location-evidence HTML could not be parsed") from exc

    privacy_masked = _privacy_masked(parser)
    collected: list[CollectedLocation] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for document in parser.json_documents:
        for item in _walk_json(document):
            item_types = _types(item.get("@type"))
            location = _place_details(_location_value(item, item_types))
            if not location:
                continue
            occurred_at = _occurred_at(item, item_types)
            published_at = _parse_datetime(item.get("datePublished"))
            place_name = str(location["place_name"])
            key = (
                place_name.casefold(),
                occurred_at.isoformat() if occurred_at else None,
                str(item.get("@id") or "") or None,
            )
            if key in seen:
                continue
            seen.add(key)
            radius = policy.default_precision_radius_m
            if location.get("latitude") is not None and location.get("longitude") is not None:
                radius = max(radius, 1000) if privacy_masked else radius
            payload = LocationObservationInput(
                place_name=place_name,
                locality=location.get("locality")
                if isinstance(location.get("locality"), str)
                else None,
                region=location.get("region") if isinstance(location.get("region"), str) else None,
                country=location.get("country")
                if isinstance(location.get("country"), str)
                else None,
                latitude=location.get("latitude")
                if isinstance(location.get("latitude"), float)
                else None,
                longitude=location.get("longitude")
                if isinstance(location.get("longitude"), float)
                else None,
                precision_radius_m=radius,
                evidence_type=policy.evidence_type,
                occurred_at=occurred_at,
                published_at=published_at,
                source_platform=policy.platform,
                source_url=source_url,
                account_identifier=_account_identifier(item),
                privacy_masked=privacy_masked,
                raw_evidence={
                    "collector": "structured_public_page",
                    "schema_type": sorted(item_types),
                    "schema_id": str(item.get("@id"))[:500] if item.get("@id") else None,
                },
            )
            collected.append(CollectedLocation(payload, next(iter(sorted(item_types)), None)))

    if collected:
        return collected

    latitude = _number(
        parser.meta.get("place:location:latitude")
        or parser.meta.get("geo.position", "").partition(";")[0],
        minimum=-90,
        maximum=90,
    )
    longitude = _number(
        parser.meta.get("place:location:longitude")
        or parser.meta.get("geo.position", "").partition(";")[2],
        minimum=-180,
        maximum=180,
    )
    title = parser.meta.get("og:title") or " ".join(parser.title_parts).strip()
    if title and latitude is not None and longitude is not None:
        payload = LocationObservationInput(
            place_name=title[:300],
            latitude=latitude,
            longitude=longitude,
            precision_radius_m=max(policy.default_precision_radius_m, 1000)
            if privacy_masked
            else policy.default_precision_radius_m,
            evidence_type=policy.evidence_type,
            source_platform=policy.platform,
            source_url=source_url,
            privacy_masked=privacy_masked,
            raw_evidence={"collector": "structured_meta", "source_field": "geo.position"},
        )
        return [CollectedLocation(payload, None)]

    raise LocationCollectionError("no structured location evidence was found on the public page")


class PublicLocationCollector:
    def __init__(
        self,
        *,
        max_bytes: int = MAX_LOCATION_PAGE_BYTES,
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
        self._last_request_at = 0.0
        self._robots: dict[str, RobotFileParser | None] = {}
        self._lock = asyncio.Lock()

    async def collect(self, url: str) -> list[CollectedLocation]:
        host, port, _ = _validate_url(url)
        if self.client is not None:
            _validate_public_addresses(await self.resolver(host, port))
        owns_client = self.client is None
        client = self.client or safe_client(
            timeout=httpx.Timeout(15.0),
            user_agent="OSINT-Workbench/1 (+local authorised research)",
            resolver=self.resolver,
        )
        try:
            if self.respect_robots:
                await self._enforce_robots(url, client)
            async with self._lock:
                content = await self._fetch_page(client, url)
            return parse_location_page(content, url)
        except EgressPolicyError as exc:
            if "public, routable address" in str(exc):
                raise LocationCollectionError(
                    "private or non-routable location-evidence host is blocked"
                ) from exc
            if "exceeds" in str(exc):
                raise LocationCollectionError("location-evidence page exceeds maximum size") from exc
            raise LocationCollectionError(f"location-evidence retrieval blocked: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LocationCollectionError(f"location-evidence retrieval failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

    async def _fetch_page(self, client: httpx.AsyncClient, url: str) -> bytes:
        now = asyncio.get_running_loop().time()
        delay = self.minimum_interval_seconds - (now - self._last_request_at)
        if delay > 0:
            await asyncio.sleep(delay)
        async with client.stream(
            "GET",
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "OSINT-Workbench/1 (+local authorised research)",
            },
        ) as response:
            self._last_request_at = asyncio.get_running_loop().time()
            if response.is_redirect:
                raise LocationCollectionError("location-evidence redirects are blocked")
            if response.status_code in {401, 403}:
                raise LocationCollectionError(
                    "location-evidence page requires authentication or denies access"
                )
            response.raise_for_status()
            mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
            if mime_type not in {"text/html", "application/xhtml+xml"}:
                raise LocationCollectionError("location-evidence response is not HTML")
            try:
                return await read_bounded_response(response, max_bytes=self.max_bytes)
            except EgressPolicyError as exc:
                raise LocationCollectionError(
                    "location-evidence page exceeds maximum size"
                ) from exc

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
                raise LocationCollectionError("robots.txt redirects are blocked")
            if response.status_code == 404:
                self._robots[origin] = None
            elif response.status_code in {401, 403}:
                raise LocationCollectionError("robots.txt denies automated access")
            elif response.status_code >= 500:
                raise LocationCollectionError("robots.txt is temporarily unavailable")
            else:
                response.raise_for_status()
                policy = RobotFileParser()
                policy.set_url(robots_url)
                policy.parse(content.decode(response.encoding or "utf-8", errors="replace").splitlines())
                self._robots[origin] = policy
        cached_policy = self._robots[origin]
        if cached_policy is not None and not cached_policy.can_fetch("OSINT-Workbench", url):
            raise LocationCollectionError("robots.txt disallows public location collection")
