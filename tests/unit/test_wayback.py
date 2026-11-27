from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.services import wayback
from app.services.wayback import (
    IdentityObservation,
    WaybackCapture,
    archived_asset_url,
    build_cdx_url,
    build_identity_evolution,
    discover_captures,
    normalise_save_url,
    normalise_target,
    parse_archived_page,
    parse_cdx_payload,
    save_page_now,
    select_timeline_captures,
)


def test_normalise_wayback_targets() -> None:
    assert normalise_target("example.org") == "example.org"
    assert normalise_target("example.org", wildcard_domain=True) == "example.org/*"
    assert normalise_target("https://example.org/profile/", prefix_target=True) == "https://example.org/profile*"
    with pytest.raises(ValueError, match="requires an HTTP"):
        normalise_target("example.org", prefix_target=True)
    assert normalise_target("https://example.org/profile") == "https://example.org/profile"
    with pytest.raises(ValueError):
        normalise_target("localhost")
    with pytest.raises(ValueError):
        normalise_target("file:///etc/passwd")
    with pytest.raises(ValueError):
        normalise_target("https://user:secret@example.org/")


def test_normalise_save_page_urls() -> None:
    assert normalise_save_url("HTTPS://Example.org/profile#section") == (
        "https://example.org/profile"
    )
    assert normalise_save_url("http://example.org:80") == "http://example.org/"
    with pytest.raises(ValueError, match="public HTTP"):
        normalise_save_url("example.org")
    with pytest.raises(ValueError, match="credentials"):
        normalise_save_url("https://user:secret@example.org/")
    with pytest.raises(ValueError, match="private or non-routable"):
        normalise_save_url("http://127.0.0.1/admin")
    with pytest.raises(ValueError, match="private or local"):
        normalise_save_url("http://service.internal/")


@pytest.mark.asyncio
async def test_save_page_now_records_archive_response_without_following_redirects() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://web.archive.org/save"
        assert parse_qs(request.content.decode())["url"] == ["https://example.org/profile"]
        return httpx.Response(
            302,
            headers={
                "Location": "/web/20260720123456/https://example.org/profile",
                "X-Archive-Save-Job-Id": "job-123",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await save_page_now("https://example.org/profile", client=client)

    assert result.status_code == 302
    assert result.archive_url == (
        "https://web.archive.org/web/20260720123456/https://example.org/profile"
    )
    assert result.job_id == "job-123"
    assert result.message == "capture available"


@pytest.mark.asyncio
async def test_discover_captures_rejects_oversized_cdx_response(monkeypatch) -> None:
    original_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (3 * 1024 * 1024))

    def fake_async_client(*, transport: object = None, **kwargs: object) -> httpx.AsyncClient:
        del transport
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(wayback.httpx, "AsyncClient", fake_async_client)

    with pytest.raises(wayback.WaybackError, match="CDX lookup failed"):
        await discover_captures("example.org")


@pytest.mark.asyncio
async def test_save_page_now_surfaces_rate_limits_and_runtime_errors() -> None:
    responses = [
        httpx.Response(429),
        httpx.Response(200, headers={"X-Archive-Wayback-Runtime-Error": "blocked capture"}),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="rate limit"):
            await save_page_now("https://example.org/one", client=client)
        with pytest.raises(RuntimeError, match="blocked capture"):
            await save_page_now("https://example.org/two", client=client)


def test_select_timeline_captures_keeps_first_and_last() -> None:
    captures = [
        WaybackCapture(
            timestamp=f"2020{month:02d}01000000",
            original_url="https://example.org/profile",
            status_code=200,
            mime_type="text/html",
            digest=str(month),
            length=100,
        )
        for month in range(1, 13)
    ]
    selected = select_timeline_captures(captures, limit=5)
    assert len(selected) == 5
    assert selected[0].timestamp == captures[0].timestamp
    assert selected[-1].timestamp == captures[-1].timestamp


@pytest.mark.asyncio
async def test_identity_evolution_dates_first_seen_biography_and_avatar_changes(
    monkeypatch,
) -> None:
    captures = [
        WaybackCapture(timestamp, "https://example.org/profile", 200, "text/html", None, 100)
        for timestamp in ("20231212000000", "20240115000000", "20251130000000")
    ]
    observations = {
        "20231212000000": IdentityObservation(
            "20231212000000",
            datetime(2023, 12, 12, tzinfo=UTC),
            "https://web.archive.org/first",
            "John Smith",
            "Original biography",
            "https://example.org/avatar.jpg",
            "https://web.archive.org/avatar-one",
            "a" * 64,
            "0" * 16,
        ),
        "20240115000000": IdentityObservation(
            "20240115000000",
            datetime(2024, 1, 15, tzinfo=UTC),
            "https://web.archive.org/middle",
            "John Smith",
            "Updated biography",
            "https://example.org/avatar.jpg",
            "https://web.archive.org/avatar-one",
            "a" * 64,
            "0" * 16,
        ),
        "20251130000000": IdentityObservation(
            "20251130000000",
            datetime(2025, 11, 30, tzinfo=UTC),
            "https://web.archive.org/latest",
            "John Smith",
            "Updated biography",
            "https://example.org/new-avatar.jpg",
            "https://web.archive.org/avatar-two",
            "b" * 64,
            "f" * 16,
        ),
    }

    async def fake_observation(capture, cache):
        return observations[capture.timestamp]

    monkeypatch.setattr(wayback, "_identity_observation", fake_observation)
    evolution = await build_identity_evolution(captures, limit=25)

    assert evolution.identity_label == "John Smith"
    assert evolution.identity_first_seen.date().isoformat() == "2023-12-12"
    assert evolution.first_seen.date().isoformat() == "2023-12-12"
    assert evolution.last_seen.date().isoformat() == "2025-11-30"
    changes = {change.change_type: change for change in evolution.changes}
    assert changes["biography_changed"].captured_at.date().isoformat() == "2024-01-15"
    assert changes["profile_picture_changed"].captured_at.date().isoformat() == "2025-11-30"
    assert changes["profile_picture_changed"].distance == 64


@pytest.mark.asyncio
async def test_identity_evolution_tracks_username_location_counts_links_and_documents(
    monkeypatch,
) -> None:
    captures = [
        WaybackCapture(timestamp, "https://github.com/example", 200, "text/html", None, 100)
        for timestamp in ("20240101000000", "20250101000000")
    ]
    first = IdentityObservation(
        "20240101000000",
        datetime(2024, 1, 1, tzinfo=UTC),
        "https://web.archive.org/first",
        "Example",
        "Bio",
        None,
        None,
        None,
        None,
        username="example",
        location="Dublin",
        followers=100,
        following=50,
        linked_sites=("https://example.org",),
        document_links=("https://github.com/example/cv.pdf",),
    )
    latest = IdentityObservation(
        "20250101000000",
        datetime(2025, 1, 1, tzinfo=UTC),
        "https://web.archive.org/latest",
        "Example",
        "Bio",
        None,
        None,
        None,
        None,
        username="example-new",
        location="Cork",
        followers=150,
        following=60,
        linked_sites=("https://example.net",),
        document_links=("https://github.com/example/portfolio.pdf",),
    )

    async def fake_observation(capture, cache):
        return first if capture.timestamp == first.timestamp else latest

    monkeypatch.setattr(wayback, "_identity_observation", fake_observation)
    evolution = await build_identity_evolution(captures)
    change_types = {change.change_type for change in evolution.changes}
    assert {
        "username_changed",
        "location_changed",
        "followers_changed",
        "following_changed",
        "linked_site_added",
        "linked_site_removed",
        "document_link_added",
        "document_link_removed",
    } <= change_types


def test_build_cdx_url_is_bounded_and_filtered() -> None:
    url = build_cdx_url("example.org/*", from_year=2010, to_year=2020, limit=999)
    params = parse_qs(urlsplit(url).query)
    assert params["url"] == ["example.org/*"]
    assert params["output"] == ["json"]
    assert params["filter"] == ["statuscode:200"]
    assert params["collapse"] == ["digest"]
    assert params["limit"] == ["200"]
    assert params["from"] == ["2010"]
    assert params["to"] == ["2020"]


def test_parse_cdx_payload_collapses_duplicate_content_and_sorts() -> None:
    payload = [
        ["timestamp", "original", "statuscode", "mimetype", "digest", "length"],
        ["20210102030405", "https://example.org/a", "200", "text/html", "ABC", "120"],
        ["20190102030405", "https://example.org/a", "200", "text/html", "ABC", "120"],
        ["20200102030405", "https://example.org/b", "200", "text/html", "DEF", "bad"],
        ["invalid"],
    ]
    captures = parse_cdx_payload(payload)
    assert [capture.timestamp for capture in captures] == ["20200102030405", "20210102030405"]
    assert captures[0].length is None
    assert captures[1].archive_url == (
        "https://web.archive.org/web/20210102030405/https://example.org/a"
    )
    assert captures[1].captured_at is not None


def test_parse_archived_page_extracts_identity_changes_without_scripts() -> None:
    snapshot = parse_archived_page(
        b"""
        <html><head><title>Example Person</title>
        <meta name="description" content="Public profile">
        <meta name="profile:location" content="Dublin, Ireland"></head>
        <body><p>Contact analyst@example.org - 1,234 followers - 321 following</p>
        <a href="/about">About</a><a href="https://example.net/me">Other site</a>
        <a href="https://example.org/cv.pdf">CV</a><img class="profile-avatar" src="/avatar.jpg">
        <script>hidden@example.org</script></body></html>
        """,
        timestamp="20200102030405",
        original_url="https://example.org/profile",
        archive_url="https://web.archive.org/web/20200102030405id_/https://example.org/profile",
    )
    assert snapshot.title == "Example Person"
    assert snapshot.description == "Public profile"
    assert snapshot.emails == ("analyst@example.org",)
    assert snapshot.links == (
        "https://example.net/me",
        "https://example.org/about",
        "https://example.org/cv.pdf",
    )
    assert snapshot.images == ("https://example.org/avatar.jpg",)
    assert snapshot.avatar_candidates == ("https://example.org/avatar.jpg",)
    assert snapshot.location == "Dublin, Ireland"
    assert snapshot.followers == 1234
    assert snapshot.following == 321
    assert snapshot.linked_sites == ("https://example.net/me",)
    assert snapshot.document_links == ("https://example.org/cv.pdf",)
    assert len(snapshot.content_sha256) == 64


def test_archived_asset_url_uses_capture_timestamp() -> None:
    assert archived_asset_url("20200102030405", "https://example.org/avatar.jpg") == (
        "https://web.archive.org/web/20200102030405id_/https://example.org/avatar.jpg"
    )
