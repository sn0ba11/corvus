from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

BOOTSTRAP_ENDPOINT = "https://data.iana.org/rdap/dns.json"
MAX_BOOTSTRAP_BYTES = 2 * 1024 * 1024
MAX_RDAP_BYTES = 2 * 1024 * 1024
USER_AGENT = "Corvus/0.1 domain RDAP registration"


def _domain(value: str) -> str:
    target = value.strip().casefold().rstrip(".")
    if not target or "/" in target or ":" in target or len(target) > 253:
        raise ValueError("domain RDAP requires a bare domain")
    labels = target.split(".")
    if len(labels) < 2 or any(not label or len(label) > 63 for label in labels):
        raise ValueError("invalid domain")
    return target


def _authoritative_rdap_base(payload: Any, tld: str) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("services"), list):
        raise ValueError("IANA RDAP bootstrap returned an unexpected shape")
    for service in payload["services"][:10000]:
        if not isinstance(service, list) or len(service) != 2:
            continue
        suffixes, bases = service
        if not isinstance(suffixes, list) or not isinstance(bases, list):
            continue
        if tld.casefold() not in {str(item).casefold().lstrip(".") for item in suffixes[:100]}:
            continue
        for raw_base in bases[:10]:
            base = str(raw_base).strip()
            parsed = urlsplit(base)
            if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                return base if base.endswith("/") else f"{base}/"
    raise ValueError(f"IANA RDAP bootstrap did not contain an HTTPS service for .{tld}")


def _clean(value: Any, limit: int = 500) -> str | None:
    if value in (None, ""):
        return None
    cleaned = " ".join(str(value).split())[:limit]
    return cleaned or None


def _normalise_events(payload: dict[str, Any]) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    for item in payload.get("events", [])[:50] if isinstance(payload.get("events"), list) else []:
        if not isinstance(item, dict):
            continue
        action = _clean(item.get("eventAction"), 100)
        date = _clean(item.get("eventDate"), 100)
        if action and date:
            events.append({"action": action, "date": date})
    return events


def _normalise_entities(payload: dict[str, Any]) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    for item in payload.get("entities", [])[:50] if isinstance(payload.get("entities"), list) else []:
        if not isinstance(item, dict):
            continue
        handle = _clean(item.get("handle"), 200)
        roles = [str(role)[:100] for role in item.get("roles", [])[:20]] if isinstance(item.get("roles"), list) else []
        public_ids = []
        for public_id in item.get("publicIds", [])[:20] if isinstance(item.get("publicIds"), list) else []:
            if isinstance(public_id, dict):
                public_ids.append({"type": _clean(public_id.get("type"), 100), "identifier": _clean(public_id.get("identifier"), 200)})
        if handle or roles or public_ids:
            entities.append({"handle": handle, "roles": roles, "public_ids": public_ids})
    return entities


def _normalise_nameservers(payload: dict[str, Any]) -> list[str]:
    output: list[str] = []
    for item in payload.get("nameservers", [])[:100] if isinstance(payload.get("nameservers"), list) else []:
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("ldhName"), 253)
        if name and name.casefold() not in {existing.casefold() for existing in output}:
            output.append(name)
    return output


class DomainRDAPAdapter(Adapter):
    tool_id = "domain_rdap"
    display_name = "Domain Registration (RDAP)"
    executable = "domain-rdap"
    capabilities = frozenset({"domain_registration", "rdap", "nameserver_evidence", "registration_events"})
    supported_query_types = frozenset({QueryType.DOMAIN})
    default_selected = False
    default_timeout = 45
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "IANA RDAP bootstrap + authoritative RDAP")

    def build_command(self, query: str, query_type: QueryType, output_dir: Path, timeout: int, proxy: str | None = None) -> list[str]:
        if query_type != QueryType.DOMAIN:
            raise ValueError("Domain RDAP accepts domain investigations only")
        target = _domain(query)
        command = [self.executable, target, "--output", str(output_dir / "domain-rdap.json"), "--timeout", str(timeout)]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    async def _json(self, client: httpx.AsyncClient, url: str, max_bytes: int) -> dict[str, Any]:
        request = client.build_request("GET", url, headers={"Accept": "application/rdap+json,application/json"})
        response = await client.send(request, stream=True)
        try:
            if response.status_code == 429:
                raise RuntimeError("rate_limited")
            if response.is_redirect:
                raise ValueError("RDAP endpoint redirected unexpectedly")
            response.raise_for_status()
            raw = await read_bounded_response(response, max_bytes=max_bytes)
        finally:
            await response.aclose()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("RDAP endpoint returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("RDAP endpoint returned an unexpected JSON shape")
        return payload

    async def execute(self, command: list[str], output_dir: Path, timeout: int, query: str, log: LogCallback | None = None) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = _domain(query)
        if len(command) < 6 or command[0] != self.executable or command[1] != target:
            return ExecutionResult(None, "", "", error_code="invalid_command", error_message="Domain RDAP command did not match the bounded adapter contract")
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        request_timeout = min(20.0, max(5.0, float(timeout) / 2))
        client_context = httpx.AsyncClient(transport=self.transport, timeout=request_timeout, follow_redirects=False, trust_env=False, headers={"User-Agent": USER_AGENT}) if self.transport is not None else safe_client(timeout=request_timeout, user_agent=USER_AGENT, proxy=proxy)
        try:
            async with asyncio.timeout(timeout):
                async with client_context as client:
                    bootstrap = await self._json(client, BOOTSTRAP_ENDPOINT, MAX_BOOTSTRAP_BYTES)
                    tld = target.rsplit(".", 1)[-1]
                    base = _authoritative_rdap_base(bootstrap, tld)
                    endpoint = urljoin(base, f"domain/{quote(target, safe='.-')}")
                    payload = await self._json(client, endpoint, MAX_RDAP_BYTES)
            nameservers = _normalise_nameservers(payload)
            record = {
                "schema_version": "1.0",
                "source": "authoritative RDAP selected through IANA bootstrap",
                "queried_domain": target,
                "queried_at": datetime.now(UTC).isoformat(),
                "rdap_base": base,
                "handle": _clean(payload.get("handle"), 300),
                "ldh_name": _clean(payload.get("ldhName"), 300),
                "unicode_name": _clean(payload.get("unicodeName"), 300),
                "status": [str(item)[:100] for item in payload.get("status", [])[:50]] if isinstance(payload.get("status"), list) else [],
                "events": _normalise_events(payload),
                "entities": _normalise_entities(payload),
                "nameservers": nameservers,
                "port43": _clean(payload.get("port43"), 300),
                "notices": [
                    {"title": _clean(item.get("title"), 200), "description": [_clean(value, 500) for value in item.get("description", [])[:10] if _clean(value, 500)]}
                    for item in payload.get("notices", [])[:20] if isinstance(item, dict)
                ] if isinstance(payload.get("notices"), list) else [],
            }
            path = output_dir / "domain-rdap.json"
            path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
            path.chmod(0o600)
            if log:
                await log("stdout", f"Domain RDAP retained registration metadata and {len(nameservers)} nameserver(s)")
            return ExecutionResult(0, json.dumps({"status": "found", "nameservers": len(nameservers)}), "", output_files=[path])
        except TimeoutError:
            return ExecutionResult(None, "", "", timed_out=True, error_code="timeout", error_message=f"Domain RDAP exceeded {timeout}s")
        except RuntimeError as exc:
            if str(exc) == "rate_limited":
                return ExecutionResult(None, "", "", rate_limited=True, error_code="rate_limited", error_message="RDAP service rate limited the request")
            raise
        except (httpx.HTTPError, EgressPolicyError, ValueError) as exc:
            return ExecutionResult(None, "", "", error_code="upstream_error", error_message=f"Domain RDAP failed: {exc}")

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "domain-rdap.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [payload] if isinstance(payload, dict) else []

    def normalise_results(self, records: list[dict[str, Any]], query: str, query_type: QueryType, raw_reference: str | None) -> list[FindingRecord]:
        if not records:
            return []
        record = records[0]
        target = _domain(query)
        findings = [FindingRecord(query_type=query_type, queried_value=target, finding_type="domain_registration", platform="RDAP", value=target, status=FindingStatus.FOUND, confidence=0.95, title=f"Domain registration · {target}", description="Public domain registration metadata from the authoritative RDAP service selected through IANA bootstrap.", metadata={key: record.get(key) for key in ("handle", "ldh_name", "unicode_name", "status", "events", "entities", "nameservers", "port43", "rdap_base", "queried_at")}, source_tool=self.tool_id, source_record_id=str(record.get("handle") or target), raw_output_reference=raw_reference)]
        for nameserver in record.get("nameservers", [])[:100] if isinstance(record.get("nameservers"), list) else []:
            value = str(nameserver).strip()
            if value:
                findings.append(FindingRecord(query_type=query_type, queried_value=target, finding_type="domain_nameserver", platform="RDAP", value=value, status=FindingStatus.FOUND, confidence=0.95, title=value, description=f"Nameserver listed in the RDAP registration record for {target}.", metadata={"registered_domain": target, "source_scope": "authoritative_rdap_nameserver"}, source_tool=self.tool_id, source_record_id=f"ns:{value}", raw_output_reference=raw_reference))
        return findings
