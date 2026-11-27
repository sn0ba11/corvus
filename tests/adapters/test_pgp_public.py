import base64
import json
import struct
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.adapters.pgp_public import PGPPublicAdapter, parse_openpgp_key
from app.models.schemas import FindingStatus, QueryType


def packet(tag: int, body: bytes) -> bytes:
    assert len(body) < 192
    return bytes([0xC0 | tag, len(body)]) + body


def synthetic_armored_key() -> str:
    created = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    public_key = bytes([4]) + struct.pack(">I", created) + bytes([1, 0x01, 0x02, 0x03])
    uid = b"Example Person <user@example.org>"
    expiry_seconds = 365 * 24 * 60 * 60
    expiry_subpacket = bytes([5, 9]) + struct.pack(">I", expiry_seconds)
    certification = bytes([4, 0x13, 1, 8]) + struct.pack(">H", len(expiry_subpacket)) + expiry_subpacket
    revocation = bytes([4, 0x20, 1, 8, 0, 0])
    binary = packet(6, public_key) + packet(13, uid) + packet(2, certification) + packet(2, revocation)
    payload = base64.b64encode(binary).decode()
    return (
        "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n"
        + payload
        + "\n-----END PGP PUBLIC KEY BLOCK-----\n"
    )


def test_parse_openpgp_key_extracts_fingerprint_uid_expiry_and_revocation() -> None:
    parsed = parse_openpgp_key(synthetic_armored_key())
    assert len(parsed["fingerprint"]) == 40
    assert parsed["key_id"] == parsed["fingerprint"][-16:]
    assert parsed["uids"] == ["Example Person <user@example.org>"]
    assert parsed["created_at"].startswith("2024-01-01")
    assert parsed["expires_at"].startswith("2024-12-31")
    assert parsed["revoked"] is True


@pytest.mark.asyncio
async def test_pgp_exact_email_lookup_retains_armored_key_and_normalises(tmp_path: Path) -> None:
    armored = synthetic_armored_key()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/vks/v1/by-email/user@example.org"
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pgp-keys"},
            content=armored.encode(),
        )

    adapter = PGPPublicAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("User@Example.org", QueryType.EMAIL, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "User@Example.org")
    assert result.returncode == 0
    assert (tmp_path / "openpgp-public-key.asc").read_text() == armored
    findings = adapter.normalise_results(
        adapter.parse_results(result, tmp_path),
        "User@Example.org",
        QueryType.EMAIL,
        "openpgp-public-key.json",
    )
    assert len(findings) == 1
    assert findings[0].status == FindingStatus.FOUND
    assert findings[0].platform == "OpenPGP"
    assert findings[0].metadata["revoked"] is True
    assert len(findings[0].metadata["fingerprint"]) == 40


@pytest.mark.asyncio
async def test_pgp_404_is_not_positive(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    adapter = PGPPublicAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("none@example.org", QueryType.EMAIL, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "none@example.org")
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "not_found"
    assert adapter.parse_results(result, tmp_path) == []
