from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from defusedxml import DefusedXmlException  # type: ignore[import-untyped]
from defusedxml.ElementTree import fromstring as safe_xml_fromstring  # type: ignore[import-untyped]

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

LOGIN_HOST = "login.microsoftonline.com"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_CERTIFICATES = 20
USER_AGENT = "Corvus/0.1 Microsoft Entra tenant metadata"
TENANT_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")


def _domain(value: str) -> str:
    candidate = value.strip().casefold().rstrip(".")
    if not DOMAIN_RE.fullmatch(candidate):
        raise ValueError("Microsoft Entra tenant metadata requires a bare domain")
    return candidate


def _clean(value: Any, limit: int = 2048) -> str | None:
    if value in (None, ""):
        return None
    cleaned = " ".join(str(value).split())[:limit]
    return cleaned or None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _tenant_id_from_issuer(value: Any) -> str | None:
    issuer = _clean(value)
    if not issuer:
        return None
    parsed = urlsplit(issuer)
    if parsed.scheme.casefold() != "https" or parsed.hostname != LOGIN_HOST:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    candidate = parts[0]
    return candidate.casefold() if TENANT_ID_RE.fullmatch(candidate) else None


def _certificate_fingerprint(value: str) -> str | None:
    compact = "".join(value.split())
    if not compact:
        return None
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return None
    return hashlib.sha256(raw).hexdigest()


def _parse_federation_metadata(raw: bytes) -> dict[str, Any]:
    try:
        root = safe_xml_fromstring(raw, forbid_dtd=True)
    except DefusedXmlException as exc:
        raise ValueError("federation metadata contained a prohibited XML declaration") from exc
    except ET.ParseError as exc:
        raise ValueError("Microsoft federation metadata was not valid XML") from exc
    entity_id = root.attrib.get("entityID")
    certificates: list[str] = []
    endpoints: list[str] = []
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name == "X509Certificate" and element.text and len(certificates) < MAX_CERTIFICATES:
            fingerprint = _certificate_fingerprint(element.text)
            if fingerprint and fingerprint not in certificates:
                certificates.append(fingerprint)
        if local_name in {"PassiveRequestorEndpoint", "SecurityTokenServiceEndpoint"}:
            for child in element.iter():
                if child.tag.rsplit("}", 1)[-1] == "Address" and child.text:
                    address = child.text.strip()
                    if address.startswith("https://") and address not in endpoints:
                        endpoints.append(address[:2048])
    return {
        "entity_id": entity_id,
        "certificate_sha256": certificates,
        "federation_endpoints": endpoints[:20],
    }


class EntraTenantMetadataAdapter(Adapter):
    tool_id = "entra_tenant_metadata"
    display_name = "Microsoft Entra Tenant Metadata"
    executable = "entra-tenant-metadata"
    capabilities = frozenset({"public_oidc_metadata", "public_federation_metadata", "tenant_id_resolution"})
    supported_query_types = frozenset({QueryType.DOMAIN})
    default_selected = False
    default_timeout = 30
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "Microsoft public tenant metadata")

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        if query_type != QueryType.DOMAIN:
            raise ValueError("Microsoft Entra Tenant Metadata accepts domain investigations only")
        domain = _domain(query)
        command = [
            self.executable,
            "--domain",
            domain,
            "--output",
            str(output_dir / "entra-tenant-metadata.json"),
        ]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        accept: str,
    ) -> tuple[int, bytes]:
        async with client.stream("GET", url, headers={"Accept": accept}) as response:
            status = response.status_code
            if response.is_redirect:
                raise ValueError("Microsoft tenant metadata endpoint redirected unexpectedly")
            raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
            if status in {400, 404}:
                return status, raw
            if status == 429:
                raise RuntimeError("rate_limited")
            response.raise_for_status()
            return status, raw

    @staticmethod
    def _write_payload(output_dir: Path, payload: dict[str, Any]) -> Path:
        path = output_dir / "entra-tenant-metadata.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        path.chmod(0o600)
        return path

    async def execute(
        self,
        command: list[str],
        output_dir: Path,
        timeout: int,
        query: str,
        log: LogCallback | None = None,
    ) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            domain = _domain(query)
        except ValueError as exc:
            return ExecutionResult(None, "", "", error_code="invalid_query", error_message=str(exc))
        if len(command) < 5 or command[0] != self.executable or command[2] != domain:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="Microsoft Entra Tenant Metadata command did not match the bounded adapter contract",
            )
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        request_timeout = min(15.0, max(5.0, float(timeout)))
        encoded_domain = quote(domain, safe=".")
        oidc_url = f"https://{LOGIN_HOST}/{encoded_domain}/v2.0/.well-known/openid-configuration"
        federation_url = f"https://{LOGIN_HOST}/{encoded_domain}/FederationMetadata/2007-06/FederationMetadata.xml"
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "queried_domain": domain,
            "status": "unknown",
            "oidc_url": oidc_url,
            "federation_url": federation_url,
            "oidc": None,
            "federation": None,
        }
        client_context = (
            httpx.AsyncClient(
                transport=self.transport,
                timeout=request_timeout,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": USER_AGENT},
            )
            if self.transport is not None
            else safe_client(timeout=request_timeout, user_agent=USER_AGENT, proxy=proxy)
        )
        try:
            async with asyncio.timeout(timeout):
                async with client_context as client:
                    oidc_status, oidc_raw = await self._get(client, oidc_url, accept="application/json")
                    payload["oidc_status"] = oidc_status
                    if oidc_status in {400, 404}:
                        payload["status"] = "not_found"
                        path = self._write_payload(output_dir, payload)
                        return ExecutionResult(0, json.dumps({"status": "not_found"}), "", output_files=[path])
                    try:
                        oidc = json.loads(oidc_raw)
                    except json.JSONDecodeError as exc:
                        raise ValueError("Microsoft OpenID metadata was not valid JSON") from exc
                    if not isinstance(oidc, dict):
                        raise ValueError("Microsoft OpenID metadata was not a JSON object")
                    payload["oidc"] = oidc
                    payload["tenant_id"] = _tenant_id_from_issuer(oidc.get("issuer"))

                    federation_status, federation_raw = await self._get(
                        client,
                        federation_url,
                        accept="application/xml,text/xml;q=0.9",
                    )
                    payload["federation_status"] = federation_status
                    if federation_status == 200:
                        payload["federation"] = _parse_federation_metadata(federation_raw)
                    payload["status"] = "found"
            path = self._write_payload(output_dir, payload)
            if log:
                await log(
                    "stdout",
                    f"Microsoft Entra tenant metadata resolved {domain} to tenant {payload.get('tenant_id') or 'unknown'}",
                )
            return ExecutionResult(
                0,
                json.dumps({"status": "found", "tenant_id": payload.get("tenant_id")}),
                "",
                output_files=[path],
            )
        except TimeoutError:
            payload["status"] = "timeout"
            path = self._write_payload(output_dir, payload)
            return ExecutionResult(None, "", "", output_files=[path], timed_out=True, error_code="timeout", error_message=f"Microsoft Entra Tenant Metadata exceeded {timeout}s")
        except RuntimeError as exc:
            if str(exc) == "rate_limited":
                payload["status"] = "rate_limited"
                path = self._write_payload(output_dir, payload)
                return ExecutionResult(None, "", "", output_files=[path], rate_limited=True, error_code="rate_limited", error_message="Microsoft tenant metadata endpoint rate-limited the request")
            raise
        except (EgressPolicyError, ValueError) as exc:
            payload["status"] = "upstream_response_error"
            payload["error"] = str(exc)[:300]
            path = self._write_payload(output_dir, payload)
            return ExecutionResult(None, "", "", output_files=[path], error_code="upstream_response_error", error_message=str(exc))
        except httpx.HTTPStatusError as exc:
            payload["status"] = "upstream_http_error"
            payload["http_status"] = exc.response.status_code
            path = self._write_payload(output_dir, payload)
            return ExecutionResult(None, "", "", output_files=[path], error_code="upstream_http_error", error_message=f"Microsoft tenant metadata returned HTTP {exc.response.status_code}")
        except httpx.HTTPError:
            payload["status"] = "upstream_error"
            path = self._write_payload(output_dir, payload)
            return ExecutionResult(None, "", "", output_files=[path], error_code="upstream_error", error_message="Microsoft tenant metadata request failed")

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "entra-tenant-metadata.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [payload] if isinstance(payload, dict) and payload.get("status") == "found" else []

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        if not records:
            return []
        payload = records[0]
        domain = _domain(query)
        oidc = _as_dict(payload.get("oidc"))
        federation = _as_dict(payload.get("federation"))
        issuer = _clean(oidc.get("issuer"))
        tenant_id = _clean(payload.get("tenant_id"), 100)
        findings: list[FindingRecord] = [
            FindingRecord(
                query_type=query_type,
                queried_value=domain,
                finding_type="microsoft_entra_tenant",
                platform="Microsoft Entra",
                value=tenant_id or domain,
                canonical_url=issuer,
                display_url=issuer,
                status=FindingStatus.FOUND,
                confidence=0.95 if tenant_id else 0.8,
                title=f"Microsoft Entra tenant for {domain}",
                description="Public tenant-specific identity metadata; a shared Microsoft endpoint alone is not evidence of common ownership.",
                metadata={
                    "tenant_id": tenant_id,
                    "issuer": issuer,
                    "authorization_endpoint": oidc.get("authorization_endpoint"),
                    "token_endpoint": oidc.get("token_endpoint"),
                    "jwks_uri": oidc.get("jwks_uri"),
                    "federation_entity_id": federation.get("entity_id"),
                    "source_scope": "public_tenant_metadata_no_authentication",
                },
                source_tool=self.tool_id,
                source_record_id=f"entra-tenant:{domain}",
                raw_output_reference=raw_reference,
            )
        ]
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            endpoint = _clean(oidc.get(name))
            if not endpoint or not endpoint.startswith("https://"):
                continue
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=domain,
                    finding_type="microsoft_entra_endpoint",
                    platform="Microsoft Entra",
                    value=endpoint,
                    canonical_url=endpoint,
                    display_url=endpoint,
                    status=FindingStatus.FOUND,
                    confidence=0.95,
                    title=name.replace("_", " ").title(),
                    metadata={"endpoint_type": name, "tenant_id": tenant_id, "domain": domain},
                    source_tool=self.tool_id,
                    source_record_id=f"entra-endpoint:{domain}:{name}",
                    raw_output_reference=raw_reference,
                )
            )
        certificates = federation.get("certificate_sha256")
        if isinstance(certificates, list):
            for fingerprint in certificates[:MAX_CERTIFICATES]:
                value = _clean(fingerprint, 128)
                if not value:
                    continue
                findings.append(
                    FindingRecord(
                        query_type=query_type,
                        queried_value=domain,
                        finding_type="microsoft_entra_signing_certificate",
                        platform="Microsoft Entra",
                        value=value,
                        status=FindingStatus.FOUND,
                        confidence=0.95,
                        title=f"Signing certificate SHA-256 {value[:16]}…",
                        metadata={"sha256": value, "tenant_id": tenant_id, "domain": domain},
                        source_tool=self.tool_id,
                        source_record_id=f"entra-cert:{domain}:{value}",
                        raw_output_reference=raw_reference,
                    )
                )
        return findings
