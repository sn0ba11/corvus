from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import struct
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

KEYSERVER = "https://keys.openpgp.org"
MAX_KEY_BYTES = 1024 * 1024
USER_AGENT = "Corvus/0.1 OpenPGP public-key collector"
ARMOR_RE = re.compile(
    r"-----BEGIN PGP PUBLIC KEY BLOCK-----\s+(?:[^\n]*\n)*?([A-Za-z0-9+/=\r\n]+)-----END PGP PUBLIC KEY BLOCK-----",
    re.MULTILINE,
)


def _decode_armored_key(text: str) -> bytes:
    lines = text.replace("\r", "").splitlines()
    try:
        start = lines.index("-----BEGIN PGP PUBLIC KEY BLOCK-----")
        end = lines.index("-----END PGP PUBLIC KEY BLOCK-----")
    except ValueError as exc:
        raise ValueError("keyserver returned invalid OpenPGP armor") from exc
    payload_lines: list[str] = []
    in_payload = False
    for line in lines[start + 1 : end]:
        stripped = line.strip()
        if not in_payload:
            if not stripped:
                in_payload = True
            elif ":" in stripped:
                continue
            else:
                in_payload = True
        if not stripped or stripped.startswith("="):
            continue
        payload_lines.append(stripped)
    try:
        return base64.b64decode("".join(payload_lines), validate=True)
    except ValueError as exc:
        raise ValueError("keyserver returned invalid OpenPGP base64") from exc


def _packet_length(data: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(data):
        raise ValueError("truncated OpenPGP packet length")
    first = data[offset]
    if first < 192:
        return first, 1
    if first <= 223:
        if offset + 1 >= len(data):
            raise ValueError("truncated OpenPGP packet length")
        return ((first - 192) << 8) + data[offset + 1] + 192, 2
    if first == 255:
        if offset + 4 >= len(data):
            raise ValueError("truncated OpenPGP packet length")
        return struct.unpack(">I", data[offset + 1 : offset + 5])[0], 5
    raise ValueError("partial OpenPGP packet lengths are not accepted")


def _packets(data: bytes) -> Iterator[tuple[int, bytes]]:
    offset = 0
    while offset < len(data):
        header = data[offset]
        offset += 1
        if not header & 0x80:
            raise ValueError("invalid OpenPGP packet header")
        if header & 0x40:
            tag = header & 0x3F
            length, consumed = _packet_length(data, offset)
            offset += consumed
        else:
            tag = (header >> 2) & 0x0F
            length_type = header & 0x03
            if length_type == 0:
                if offset >= len(data):
                    raise ValueError("truncated OpenPGP packet")
                length, consumed = data[offset], 1
            elif length_type == 1:
                if offset + 1 >= len(data):
                    raise ValueError("truncated OpenPGP packet")
                length, consumed = struct.unpack(">H", data[offset : offset + 2])[0], 2
            elif length_type == 2:
                if offset + 3 >= len(data):
                    raise ValueError("truncated OpenPGP packet")
                length, consumed = struct.unpack(">I", data[offset : offset + 4])[0], 4
            else:
                length, consumed = len(data) - offset, 0
            offset += consumed
        end = offset + length
        if end > len(data):
            raise ValueError("truncated OpenPGP packet body")
        yield tag, data[offset:end]
        offset = end


def _fingerprint(public_key_body: bytes) -> str:
    if not public_key_body:
        raise ValueError("empty OpenPGP public-key packet")
    version = public_key_body[0]
    if version == 4:
        prefix = b"\x99" + struct.pack(">H", len(public_key_body))
        # OpenPGP v4 fingerprints are defined as SHA-1 over the framed public-key packet.
        return hashlib.sha1(prefix + public_key_body, usedforsecurity=False).hexdigest().upper()  # noqa: S324
    if version == 5:
        prefix = b"\x9a" + struct.pack(">I", len(public_key_body))
        return hashlib.sha256(prefix + public_key_body).hexdigest().upper()
    if version == 6:
        prefix = b"\x9b" + struct.pack(">I", len(public_key_body))
        return hashlib.sha256(prefix + public_key_body).hexdigest().upper()
    raise ValueError(f"unsupported OpenPGP public-key version {version}")


def _signature_subpackets(body: bytes) -> tuple[int | None, bool]:
    if len(body) < 6 or body[0] != 4:
        return None, False
    signature_type = body[1]
    hashed_length = struct.unpack(">H", body[4:6])[0]
    start = 6
    end = min(len(body), start + hashed_length)
    offset = start
    key_expiry: int | None = None
    while offset < end:
        length, consumed = _packet_length(body, offset)
        offset += consumed
        if length <= 0 or offset + length > end:
            break
        packet = body[offset : offset + length]
        offset += length
        if not packet:
            continue
        subpacket_type = packet[0] & 0x7F
        if subpacket_type == 9 and len(packet) >= 5:
            key_expiry = struct.unpack(">I", packet[1:5])[0]
    return key_expiry, signature_type == 0x20


def parse_openpgp_key(armored: str) -> dict[str, Any]:
    binary = _decode_armored_key(armored)
    fingerprint: str | None = None
    created_at: datetime | None = None
    uids: list[str] = []
    revoked = False
    expiry_seconds: int | None = None
    for tag, body in _packets(binary):
        if tag == 6 and fingerprint is None:
            fingerprint = _fingerprint(body)
            if len(body) >= 5:
                created_at = datetime.fromtimestamp(struct.unpack(">I", body[1:5])[0], tz=UTC)
        elif tag == 13:
            uid = " ".join(body.decode("utf-8", errors="replace").split())[:500]
            if uid and uid not in uids:
                uids.append(uid)
        elif tag == 2:
            candidate_expiry, key_revocation = _signature_subpackets(body)
            revoked = revoked or key_revocation
            if candidate_expiry is not None:
                expiry_seconds = candidate_expiry
    if fingerprint is None:
        raise ValueError("OpenPGP response did not contain a primary public key")
    expires_at = created_at + timedelta(seconds=expiry_seconds) if created_at and expiry_seconds else None
    return {
        "fingerprint": fingerprint,
        "key_id": fingerprint[-16:],
        "uids": uids[:20],
        "created_at": created_at.isoformat() if created_at else None,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "revoked": revoked,
    }


class PGPPublicAdapter(Adapter):
    tool_id = "pgp_public"
    display_name = "OpenPGP Public Key"
    executable = "openpgp-public-key"
    capabilities = frozenset({"email_search", "pgp_key"})
    supported_query_types = frozenset({QueryType.EMAIL})
    # One unauthenticated request to a public directory, so it runs with every email search.
    default_selected = True
    default_timeout = 30
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "keys.openpgp.org VKS")

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        email = query.strip().casefold()
        if query_type != QueryType.EMAIL or "@" not in email or len(email) > 320:
            raise ValueError("OpenPGP key lookup accepts email investigations only")
        command = [
            self.executable,
            "--email",
            email,
            "--output",
            str(output_dir / "openpgp-public-key.json"),
        ]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    async def execute(
        self,
        command: list[str],
        output_dir: Path,
        timeout: int,
        query: str,
        log: LogCallback | None = None,
    ) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        email = query.strip().casefold()
        if len(command) < 5 or command[0] != self.executable or command[2] != email:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="OpenPGP command did not match the bounded adapter contract",
            )
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        request_timeout = min(20.0, max(5.0, float(timeout)))
        try:
            async with asyncio.timeout(timeout):
                client_context = (
                    httpx.AsyncClient(
                        transport=self.transport,
                        timeout=request_timeout,
                        follow_redirects=False,
                        trust_env=False,
                        headers={"Accept": "application/pgp-keys", "User-Agent": USER_AGENT},
                    )
                    if self.transport is not None
                    else safe_client(
                        timeout=request_timeout,
                        user_agent=USER_AGENT,
                        proxy=proxy,
                    )
                )
                async with client_context as client:
                    url = f"{KEYSERVER}/vks/v1/by-email/{quote(email, safe='')}"
                    async with client.stream(
                        "GET", url, headers={"Accept": "application/pgp-keys"}
                    ) as response:
                        if response.status_code == 404:
                            return ExecutionResult(0, json.dumps({"status": "not_found"}), "")
                        if response.status_code == 429:
                            return ExecutionResult(
                                None,
                                "",
                                "",
                                rate_limited=True,
                                error_code="rate_limited",
                                error_message="OpenPGP keyserver rate limited the email lookup",
                            )
                        if response.is_redirect:
                            raise ValueError("OpenPGP keyserver redirected unexpectedly")
                        response.raise_for_status()
                        raw = await read_bounded_response(response, max_bytes=MAX_KEY_BYTES)
            armored = raw.decode("utf-8", errors="strict")
            parsed = parse_openpgp_key(armored)
            parsed.update(
                {
                    "status": "found",
                    "email": email,
                    "source_server": KEYSERVER,
                    "armored_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
            path = output_dir / "openpgp-public-key.json"
            path.write_text(json.dumps(parsed, indent=2, sort_keys=True), encoding="utf-8")
            path.chmod(0o600)
            armor_path = output_dir / "openpgp-public-key.asc"
            armor_path.write_text(armored, encoding="utf-8")
            armor_path.chmod(0o600)
            if log:
                await log("stdout", f"OpenPGP key found: {parsed['fingerprint']}")
            return ExecutionResult(
                0,
                json.dumps({"status": "found", "fingerprint": parsed["fingerprint"]}),
                "",
                output_files=[path, armor_path],
            )
        except TimeoutError:
            return ExecutionResult(
                None,
                "",
                "",
                timed_out=True,
                error_code="timeout",
                error_message=f"OpenPGP lookup exceeded {timeout}s",
            )
        except (EgressPolicyError, UnicodeDecodeError, ValueError) as exc:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="upstream_response_error",
                error_message=str(exc),
            )
        except httpx.HTTPStatusError as exc:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="upstream_http_error",
                error_message=f"OpenPGP keyserver returned HTTP {exc.response.status_code}",
            )
        except httpx.HTTPError:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="upstream_error",
                error_message="OpenPGP keyserver request failed",
            )

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "openpgp-public-key.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [payload] if isinstance(payload, dict) else []

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        findings: list[FindingRecord] = []
        for record in records[:1]:
            fingerprint = str(record.get("fingerprint") or "").strip()
            if not re.fullmatch(r"[A-F0-9]{40,64}", fingerprint):
                continue
            uids = record.get("uids") if isinstance(record.get("uids"), list) else []
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="openpgp_public_key",
                    platform="OpenPGP",
                    value=fingerprint,
                    canonical_url=f"https://keys.openpgp.org/search?q={quote(query.strip(), safe='')}",
                    status=FindingStatus.FOUND,
                    confidence=0.98,
                    title=f"OpenPGP key {fingerprint[-16:]}",
                    description=str(uids[0]) if uids else None,
                    metadata={
                        "fingerprint": fingerprint,
                        "key_id": record.get("key_id"),
                        "uids": uids,
                        "created_at": record.get("created_at"),
                        "expires_at": record.get("expires_at"),
                        "revoked": record.get("revoked"),
                        "source_server": record.get("source_server"),
                        "armored_sha256": record.get("armored_sha256"),
                    },
                    source_tool=self.tool_id,
                    source_record_id=fingerprint,
                    raw_output_reference=raw_reference,
                )
            )
        return findings
