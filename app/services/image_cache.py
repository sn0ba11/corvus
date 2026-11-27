import asyncio
import hashlib
import ipaddress
import json
import os
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

ALLOWED_MIME_TYPES = {
    "image/gif": b"GIF8",
    "image/jpeg": b"\xff\xd8\xff",
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/webp": b"RIFF",
}
Resolver = Callable[[str, int], Awaitable[list[str]]]


class ImageCacheError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CachedImage:
    path: Path
    source_url: str
    retrieved_at: datetime
    mime_type: str
    size_bytes: int
    sha256: str

    def metadata(self) -> dict[str, str | int]:
        return {
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at.astimezone(UTC).isoformat(),
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


async def resolve_host(host: str, port: int) -> list[str]:
    records = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    return sorted({str(record[4][0]) for record in records})


def _validate_public_addresses(addresses: list[str]) -> None:
    if not addresses:
        raise ImageCacheError("image host did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ImageCacheError("private or non-routable image host is blocked")


def _validate_url(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ImageCacheError("only HTTP(S) image URLs are supported")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ImageCacheError("invalid image URL")
    return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def _validate_image(mime_type: str, content: bytes) -> None:
    expected = ALLOWED_MIME_TYPES.get(mime_type)
    if expected is None:
        raise ImageCacheError("unsupported image content type")
    if not content.startswith(expected):
        raise ImageCacheError("image signature does not match content type")
    if mime_type == "image/webp" and (len(content) < 12 or content[8:12] != b"WEBP"):
        raise ImageCacheError("invalid WebP image signature")


class ImageCache:
    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = 5 * 1024 * 1024,
        resolver: Resolver = resolve_host,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.resolver = resolver
        self.client = client

    async def fetch(self, url: str) -> CachedImage:
        host, port = _validate_url(url)
        if self.client is not None:
            _validate_public_addresses(await self.resolver(host, port))
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        metadata_path = self.root / f"{key}.json"
        if metadata_path.is_file():
            cached = self._load_cached(metadata_path)
            if cached.path.is_file():
                return cached

        owns_client = self.client is None
        client = self.client or safe_client(
            timeout=httpx.Timeout(10.0),
            user_agent="OSINT-Workbench/1",
            resolver=self.resolver,
        )
        try:
            async with client.stream(
                "GET",
                url,
                headers={
                    "Accept": ", ".join(ALLOWED_MIME_TYPES),
                    "User-Agent": "OSINT-Workbench/1",
                },
            ) as response:
                if response.is_redirect:
                    raise ImageCacheError("image redirects are blocked")
                response.raise_for_status()
                mime_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                )
                try:
                    content = await read_bounded_response(response, max_bytes=self.max_bytes)
                except EgressPolicyError as exc:
                    raise ImageCacheError("image exceeds maximum size") from exc
        except EgressPolicyError as exc:
            if "public, routable address" in str(exc):
                raise ImageCacheError("private or non-routable image host is blocked") from exc
            raise ImageCacheError(f"image retrieval blocked: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ImageCacheError(f"image retrieval failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

        _validate_image(mime_type, content)
        digest = hashlib.sha256(content).hexdigest()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        image_path = self.root / f"{key}.img"
        temporary = self.root / f".{key}.{os.getpid()}.tmp"
        temporary.write_bytes(content)
        temporary.chmod(0o600)
        temporary.replace(image_path)
        retrieved_at = datetime.now(UTC)
        metadata = {
            "source_url": url,
            "retrieved_at": retrieved_at.isoformat(),
            "mime_type": mime_type,
            "size_bytes": len(content),
            "sha256": digest,
            "filename": image_path.name,
        }
        metadata_path.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
        metadata_path.chmod(0o600)
        return CachedImage(image_path, url, retrieved_at, mime_type, len(content), digest)

    def _load_cached(self, metadata_path: Path) -> CachedImage:
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            filename = str(metadata["filename"])
            candidate = Path(filename)
            if candidate.is_absolute() or candidate.name != filename:
                raise ImageCacheError("cached image filename escapes the cache root")
            root = self.root.resolve()
            image_path = (self.root / candidate).resolve()
            if image_path.parent != root:
                raise ImageCacheError("cached image filename escapes the cache root")
            return CachedImage(
                path=image_path,
                source_url=str(metadata["source_url"]),
                retrieved_at=datetime.fromisoformat(str(metadata["retrieved_at"])),
                mime_type=str(metadata["mime_type"]),
                size_bytes=int(metadata["size_bytes"]),
                sha256=str(metadata["sha256"]),
            )
        except ImageCacheError:
            raise
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ImageCacheError("cached image metadata is invalid") from exc
