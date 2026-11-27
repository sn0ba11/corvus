import json
from pathlib import Path

import httpx
import pytest

from app.services.image_cache import ImageCache, ImageCacheError


async def public_resolver(host: str, port: int) -> list[str]:
    assert host == "images.example"
    assert port == 443
    return ["93.184.216.34"]


@pytest.mark.asyncio
async def test_fetches_valid_image_and_reuses_cache(tmp_path: Path) -> None:
    requests = 0
    content = b"\x89PNG\r\n\x1a\nfixture"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            headers={"Content-Type": "image/png", "Content-Length": str(len(content))},
            content=content,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = ImageCache(tmp_path, resolver=public_resolver, client=client)
        first = await cache.fetch("https://images.example/avatar.png")
        second = await cache.fetch("https://images.example/avatar.png")

    assert requests == 1
    assert first.path.read_bytes() == content
    assert second.sha256 == first.sha256
    assert first.mime_type == "image/png"
    assert first.size_bytes == len(content)
    assert first.path.stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
async def test_blocks_private_and_loopback_destinations(tmp_path: Path) -> None:
    async def private_resolver(host: str, port: int) -> list[str]:
        return ["127.0.0.1", "10.0.0.8"]

    cache = ImageCache(tmp_path, resolver=private_resolver)
    with pytest.raises(ImageCacheError, match="private or non-routable"):
        await cache.fetch("http://internal.example/avatar.png")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:password@images.example/avatar.png",
        "data:image/png;base64,AAAA",
    ],
)
async def test_rejects_unsafe_url_forms(tmp_path: Path, url: str) -> None:
    cache = ImageCache(tmp_path, resolver=public_resolver)
    with pytest.raises(ImageCacheError):
        await cache.fetch(url)


@pytest.mark.asyncio
async def test_rejects_redirects_and_wrong_content(tmp_path: Path) -> None:
    responses = [
        httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"}),
        httpx.Response(200, headers={"Content-Type": "image/png"}, content=b"<html>bad"),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = ImageCache(tmp_path, resolver=public_resolver, client=client)
        with pytest.raises(ImageCacheError, match="redirects are blocked"):
            await cache.fetch("https://images.example/redirect")
        with pytest.raises(ImageCacheError, match="signature"):
            await cache.fetch("https://images.example/not-image")


def test_cached_metadata_cannot_escape_cache_root(tmp_path: Path) -> None:
    metadata_path = tmp_path / "cache.json"
    metadata_path.write_text(
        json.dumps(
            {
                "filename": "../outside.img",
                "source_url": "https://images.example/avatar.png",
                "retrieved_at": "2026-08-14T00:00:00+00:00",
                "mime_type": "image/png",
                "size_bytes": 8,
                "sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ImageCacheError, match="escapes the cache root"):
        ImageCache(tmp_path)._load_cached(metadata_path)


@pytest.mark.asyncio
async def test_enforces_streamed_size_limit(tmp_path: Path) -> None:
    content = b"\xff\xd8\xff" + b"x" * 100

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=content)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = ImageCache(tmp_path, max_bytes=16, resolver=public_resolver, client=client)
        with pytest.raises(ImageCacheError, match="maximum size"):
            await cache.fetch("https://images.example/large.jpg")
