from pathlib import Path

import httpx
import pytest

from app.models.schemas import LocationEvidenceType
from app.services.location_collection import (
    LocationCollectionError,
    PublicLocationCollector,
    parse_location_page,
    policy_for_url,
)


def test_parses_dated_review_venue_from_json_ld() -> None:
    content = b"""
    <html><head><script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Review",
      "dateVisited": "2025-02-10",
      "datePublished": "2025-02-14",
      "author": {"@type": "Person", "name": "John Smith"},
      "reviewBody": "Coffee before a flight",
      "itemReviewed": {
        "@type": "Restaurant",
        "name": "Airport Cafe",
        "address": {
          "@type": "PostalAddress",
          "addressLocality": "Cork",
          "addressRegion": "County Cork",
          "addressCountry": "Ireland"
        },
        "geo": {"@type": "GeoCoordinates", "latitude": 51.849, "longitude": -8.491}
      }
    }
    </script></head></html>
    """
    result = parse_location_page(content, "https://www.tripadvisor.com/Profile/john/review/1")
    assert len(result) == 1
    payload = result[0].payload
    assert payload.evidence_type == LocationEvidenceType.REVIEW
    assert payload.source_platform == "Tripadvisor"
    assert payload.place_name == "Airport Cafe"
    assert payload.locality == "Cork"
    assert payload.region == "County Cork"
    assert payload.country == "Ireland"
    assert payload.latitude == 51.849
    assert payload.longitude == -8.491
    assert payload.occurred_at is not None
    assert payload.occurred_at.date().isoformat() == "2025-02-10"
    assert payload.published_at is not None
    assert payload.published_at.date().isoformat() == "2025-02-14"
    assert payload.account_identifier == "John Smith"


def test_parses_fitness_activity_and_marks_visible_privacy_mask() -> None:
    content = b"""
    <html><head><script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "ExerciseAction",
      "name": "Morning Run",
      "startTime": "2026-06-01T07:30:00Z",
      "agent": {"@type": "Person", "name": "runner42"},
      "location": {
        "@type": "Place",
        "name": "Phoenix Park",
        "address": {"addressLocality": "Dublin", "addressCountry": "Ireland"},
        "geo": {"latitude": 53.355, "longitude": -6.329}
      }
    }
    </script></head><body>Start and end hidden by map visibility settings.</body></html>
    """
    result = parse_location_page(content, "https://www.strava.com/activities/123")
    payload = result[0].payload
    assert payload.evidence_type == LocationEvidenceType.FITNESS_ACTIVITY
    assert payload.source_platform == "Strava"
    assert payload.place_name == "Phoenix Park"
    assert payload.account_identifier == "runner42"
    assert payload.privacy_masked is True
    assert payload.precision_radius_m == 1000
    assert payload.occurred_at is not None
    assert payload.occurred_at.isoformat() == "2026-06-01T07:30:00+00:00"


def test_uses_geo_meta_only_when_coordinates_are_explicit() -> None:
    content = b"""
    <html><head>
      <meta property="og:title" content="Visible trail area">
      <meta name="geo.position" content="53.35;-6.30">
    </head></html>
    """
    result = parse_location_page(content, "https://www.alltrails.com/trail/example")
    assert result[0].payload.place_name == "Visible trail area"
    assert result[0].payload.latitude == 53.35
    assert result[0].payload.longitude == -6.30


def test_rejects_unsupported_hosts_and_unstructured_pages() -> None:
    assert policy_for_url("https://example.org/profile") is None
    with pytest.raises(LocationCollectionError, match="unsupported"):
        parse_location_page(b"<html></html>", "https://example.org/profile")
    with pytest.raises(LocationCollectionError, match="no structured"):
        parse_location_page(
            b"<html><title>Unstructured activity</title></html>",
            "https://www.strava.com/activities/123",
        )


@pytest.mark.asyncio
async def test_collector_blocks_private_resolution_and_redirects(tmp_path: Path) -> None:
    async def private_resolver(host: str, port: int) -> list[str]:
        assert host == "www.strava.com"
        assert port == 443
        return ["127.0.0.1"]

    collector = PublicLocationCollector(resolver=private_resolver, respect_robots=False)
    with pytest.raises(LocationCollectionError, match="private or non-routable"):
        await collector.collect("https://www.strava.com/activities/123")

    async def public_resolver(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = PublicLocationCollector(
            resolver=public_resolver,
            client=client,
            respect_robots=False,
        )
        with pytest.raises(LocationCollectionError, match="redirects are blocked"):
            await collector.collect("https://www.strava.com/activities/123")


@pytest.mark.asyncio
async def test_collector_enforces_streamed_size_limit() -> None:
    async def public_resolver(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            content=b"<html>" + (b"x" * 100) + b"</html>",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = PublicLocationCollector(
            resolver=public_resolver,
            client=client,
            respect_robots=False,
            max_bytes=32,
        )
        with pytest.raises(LocationCollectionError, match="maximum size"):
            await collector.collect("https://www.strava.com/activities/123")


@pytest.mark.asyncio
async def test_collector_respects_robots_policy() -> None:
    requests: list[str] = []

    async def public_resolver(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/plain"},
                text="User-agent: OSINT-Workbench\nDisallow: /activities/",
            )
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = PublicLocationCollector(
            resolver=public_resolver,
            client=client,
            minimum_interval_seconds=0,
        )
        with pytest.raises(LocationCollectionError, match="robots.txt disallows"):
            await collector.collect("https://www.strava.com/activities/123")
    assert requests == ["https://www.strava.com/robots.txt"]
