from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.adapters.domain_rdap import _domain
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

DOH_ENDPOINT = "https://dns.google/resolve"
CT_ENDPOINT = "https://crt.sh/"
MAX_DNS_BYTES = 256 * 1024
MAX_CT_BYTES = 2 * 1024 * 1024
MAX_POLICY_BYTES = 128 * 1024
MAX_SPF_DEPTH = 3
MAX_SPF_DOMAINS = 16
MAX_CT_CERTIFICATES = 50
MAX_CT_NAMES = 200
USER_AGENT = "Corvus/0.1 passive domain relationship evidence"


def _decode_txt_data(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    quoted = re.findall(r'"(?:\\.|[^"\\])*"', raw)
    if quoted:
        parts: list[str] = []
        for item in quoted:
            try:
                decoded = json.loads(item)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, str):
                parts.append(decoded)
        if parts:
            return "".join(parts).strip()
    return raw.strip('"').strip()


def _txt_records(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    answers = payload.get("Answer")
    if not isinstance(answers, list):
        return []
    records: list[str] = []
    for item in answers[:100]:
        if not isinstance(item, dict) or int(item.get("type") or 0) != 16:
            continue
        value = _decode_txt_data(item.get("data"))
        if value and value not in records:
            records.append(value[:4096])
    return records


def _normalise_relationship_domain(value: str) -> str | None:
    candidate = value.strip().rstrip(".").casefold()
    if candidate.startswith("*."):
        candidate = candidate[2:]
    if not candidate or "%" in candidate:
        return None
    try:
        return _domain(candidate)
    except ValueError:
        return None


def _spf_links(record: str) -> tuple[list[str], list[str]]:
    includes: list[str] = []
    redirects: list[str] = []
    for raw in re.findall(r"(?:^|\s)include:([^\s]+)", record, flags=re.IGNORECASE):
        target = _normalise_relationship_domain(raw)
        if target and target not in includes:
            includes.append(target)
    match = re.search(r"(?:^|\s)redirect=([^\s]+)", record, flags=re.IGNORECASE)
    if match:
        target = _normalise_relationship_domain(match.group(1))
        if target:
            redirects.append(target)
    return includes, redirects


def _tag_record(record: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for part in record.split(";")[:50]:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip().casefold()
        value = value.strip()
        if key and value:
            tags[key[:40]] = value[:2048]
    return tags


def _mailto_addresses(value: str) -> list[str]:
    output: list[str] = []
    for item in value.split(",")[:50]:
        candidate = item.strip()
        if not candidate.casefold().startswith("mailto:"):
            continue
        address = candidate[7:].split("!", 1)[0].strip().casefold()
        if address.count("@") != 1 or len(address) > 320:
            continue
        local, domain = address.rsplit("@", 1)
        if not local or not _normalise_relationship_domain(domain):
            continue
        if address not in output:
            output.append(address)
    return output


def _host_from_https_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return _normalise_relationship_domain(parsed.hostname)


def _parse_mta_sts_policy(text: str) -> dict[str, Any]:
    version: str | None = None
    mode: str | None = None
    max_age: int | None = None
    mx: list[str] = []
    for line in text.splitlines()[:200]:
        clean = line.strip()
        if not clean or clean.startswith("#") or ":" not in clean:
            continue
        key, value = clean.split(":", 1)
        key = key.strip().casefold()
        value = value.strip()
        if key == "version":
            version = value[:100]
        elif key == "mode":
            mode = value[:100]
        elif key == "max_age":
            try:
                max_age = max(0, min(int(value), 315576000))
            except ValueError:
                max_age = None
        elif key == "mx":
            target = _normalise_relationship_domain(value)
            if target and target not in mx:
                mx.append(target)
    return {"version": version, "mode": mode, "max_age": max_age, "mx": mx[:50]}


def _ct_names(record: dict[str, Any], root: str) -> list[str]:
    names: list[str] = []
    values: list[str] = []
    if record.get("common_name"):
        values.append(str(record["common_name"]))
    if record.get("name_value"):
        values.extend(str(record["name_value"]).splitlines())
    for raw in values[:200]:
        target = _normalise_relationship_domain(raw)
        if not target:
            continue
        if target != root and not target.endswith(f".{root}"):
            continue
        if target not in names:
            names.append(target)
    return names[:MAX_CT_NAMES]


class DomainRelationshipAdapter(Adapter):
    tool_id = "domain_relationships"
    display_name = "Certificate & Email Relationships"
    executable = "domain-relationships"
    capabilities = frozenset(
        {
            "certificate_transparency",
            "spf_relationships",
            "dmarc_relationships",
            "mta_sts",
            "bimi",
        }
    )
    supported_query_types = frozenset({QueryType.DOMAIN})
    default_selected = False
    default_timeout = 60
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "passive DNS + Certificate Transparency")

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        if query_type != QueryType.DOMAIN:
            raise ValueError("Certificate & Email Relationships accepts domain investigations only")
        target = _domain(query)
        command = [
            self.executable,
            target,
            "--output",
            str(output_dir / "domain-relationships.json"),
            "--timeout",
            str(timeout),
        ]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    async def _json_get(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        params: dict[str, str],
        max_bytes: int,
    ) -> Any:
        request = client.build_request("GET", url, params=params, headers={"Accept": "application/json"})
        response = await client.send(request, stream=True)
        try:
            if response.status_code == 429:
                raise RuntimeError("rate_limited")
            if response.is_redirect:
                raise ValueError(f"unexpected redirect from {url}")
            response.raise_for_status()
            raw = await read_bounded_response(response, max_bytes=max_bytes)
        finally:
            await response.aclose()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed JSON from {url}") from exc

    async def _text_get(self, client: httpx.AsyncClient, url: str, max_bytes: int) -> str:
        request = client.build_request("GET", url, headers={"Accept": "text/plain"})
        response = await client.send(request, stream=True)
        try:
            if response.status_code == 429:
                raise RuntimeError("rate_limited")
            if response.is_redirect:
                raise ValueError(f"unexpected redirect from {url}")
            response.raise_for_status()
            raw = await read_bounded_response(response, max_bytes=max_bytes)
        finally:
            await response.aclose()
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)

    async def _dns_txt(self, client: httpx.AsyncClient, name: str) -> list[str]:
        payload = await self._json_get(
            client,
            DOH_ENDPOINT,
            params={"name": name, "type": "TXT", "cd": "0", "do": "0"},
            max_bytes=MAX_DNS_BYTES,
        )
        return _txt_records(payload)

    async def _collect_spf(
        self,
        client: httpx.AsyncClient,
        root: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        records: list[dict[str, Any]] = []
        relationships: list[dict[str, str]] = []
        queue: list[tuple[str, int]] = [(root, 0)]
        visited: set[str] = set()
        while queue and len(visited) < MAX_SPF_DOMAINS:
            current, depth = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            txt = await self._dns_txt(client, current)
            spf = next((value for value in txt if value.casefold().startswith("v=spf1")), None)
            if not spf:
                records.append({"domain": current, "depth": depth, "record": None, "includes": [], "redirects": []})
                continue
            includes, redirects = _spf_links(spf)
            records.append(
                {
                    "domain": current,
                    "depth": depth,
                    "record": spf,
                    "includes": includes,
                    "redirects": redirects,
                }
            )
            for target in includes:
                relationships.append(
                    {
                        "kind": "spf_include",
                        "source": current,
                        "target": target,
                        "target_type": "domain",
                        "label": "SPF includes",
                    }
                )
            for target in redirects:
                relationships.append(
                    {
                        "kind": "spf_redirect",
                        "source": current,
                        "target": target,
                        "target_type": "domain",
                        "label": "SPF redirects to",
                    }
                )
            if depth >= MAX_SPF_DEPTH:
                continue
            for target in [*includes, *redirects]:
                if target not in visited and len(visited) + len(queue) < MAX_SPF_DOMAINS:
                    queue.append((target, depth + 1))
        return records, relationships

    async def execute(
        self,
        command: list[str],
        output_dir: Path,
        timeout: int,
        query: str,
        log: LogCallback | None = None,
    ) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = _domain(query)
        if len(command) < 6 or command[0] != self.executable or command[1] != target:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="domain relationship command did not match the bounded adapter contract",
            )
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        request_timeout = min(20.0, max(5.0, float(timeout) / 3))
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
        relationships: list[dict[str, str]] = []
        errors: list[str] = []
        spf_records: list[dict[str, Any]] = []
        dmarc: dict[str, Any] = {"record": None, "reporting_addresses": []}
        mta_sts: dict[str, Any] = {"dns_record": None, "policy_url": None, "policy": None}
        bimi: dict[str, Any] = {"record": None, "logo_url": None, "authority_url": None}
        certificates: list[dict[str, Any]] = []
        successful_sources = 0
        rate_limited = False

        try:
            async with asyncio.timeout(timeout):
                async with client_context as client:
                    try:
                        spf_records, spf_relationships = await self._collect_spf(client, target)
                        relationships.extend(spf_relationships)
                        successful_sources += 1
                    except RuntimeError as exc:
                        if str(exc) == "rate_limited":
                            rate_limited = True
                        errors.append(f"SPF collection: {exc}")
                    except (httpx.HTTPError, EgressPolicyError, ValueError) as exc:
                        errors.append(f"SPF collection: {exc}")

                    try:
                        dmarc_records = await self._dns_txt(client, f"_dmarc.{target}")
                        dmarc_record = next(
                            (value for value in dmarc_records if value.casefold().startswith("v=dmarc1")),
                            None,
                        )
                        dmarc["record"] = dmarc_record
                        if dmarc_record:
                            tags = _tag_record(dmarc_record)
                            addresses: list[str] = []
                            for key in ("rua", "ruf"):
                                for address in _mailto_addresses(tags.get(key, "")):
                                    if address not in addresses:
                                        addresses.append(address)
                                    relationships.append(
                                        {
                                            "kind": "dmarc_reporting_address",
                                            "source": target,
                                            "target": address,
                                            "target_type": "email",
                                            "label": f"DMARC {key} reports to",
                                        }
                                    )
                            dmarc["reporting_addresses"] = addresses
                            dmarc["policy"] = tags.get("p")
                            dmarc["subdomain_policy"] = tags.get("sp")
                        successful_sources += 1
                    except RuntimeError as exc:
                        if str(exc) == "rate_limited":
                            rate_limited = True
                        errors.append(f"DMARC collection: {exc}")
                    except (httpx.HTTPError, EgressPolicyError, ValueError) as exc:
                        errors.append(f"DMARC collection: {exc}")

                    try:
                        sts_records = await self._dns_txt(client, f"_mta-sts.{target}")
                        sts_record = next(
                            (value for value in sts_records if value.casefold().startswith("v=stsv1")),
                            None,
                        )
                        mta_sts["dns_record"] = sts_record
                        if sts_record:
                            policy_host = f"mta-sts.{target}"
                            policy_url = f"https://{policy_host}/.well-known/mta-sts.txt"
                            relationships.append(
                                {
                                    "kind": "mta_sts_policy_host",
                                    "source": target,
                                    "target": policy_host,
                                    "target_type": "domain",
                                    "label": "MTA-STS policy host",
                                }
                            )
                            mta_sts["policy_url"] = policy_url
                            policy_text = await self._text_get(client, policy_url, MAX_POLICY_BYTES)
                            policy = _parse_mta_sts_policy(policy_text)
                            mta_sts["policy"] = policy
                            for mx in policy.get("mx", []):
                                relationships.append(
                                    {
                                        "kind": "mta_sts_mx",
                                        "source": target,
                                        "target": mx,
                                        "target_type": "domain",
                                        "label": "MTA-STS authorises MX",
                                    }
                                )
                        successful_sources += 1
                    except RuntimeError as exc:
                        if str(exc) == "rate_limited":
                            rate_limited = True
                        errors.append(f"MTA-STS collection: {exc}")
                    except (httpx.HTTPError, EgressPolicyError, ValueError) as exc:
                        errors.append(f"MTA-STS collection: {exc}")

                    try:
                        bimi_records = await self._dns_txt(client, f"default._bimi.{target}")
                        bimi_record = next(
                            (value for value in bimi_records if value.casefold().startswith("v=bimi1")),
                            None,
                        )
                        bimi["record"] = bimi_record
                        if bimi_record:
                            tags = _tag_record(bimi_record)
                            bimi["logo_url"] = tags.get("l")
                            bimi["authority_url"] = tags.get("a")
                            for field, kind, label in (
                                ("l", "bimi_logo_host", "BIMI logo host"),
                                ("a", "bimi_authority_host", "BIMI authority host"),
                            ):
                                host = _host_from_https_url(tags.get(field, ""))
                                if host:
                                    relationships.append(
                                        {
                                            "kind": kind,
                                            "source": target,
                                            "target": host,
                                            "target_type": "domain",
                                            "label": label,
                                        }
                                    )
                        successful_sources += 1
                    except RuntimeError as exc:
                        if str(exc) == "rate_limited":
                            rate_limited = True
                        errors.append(f"BIMI collection: {exc}")
                    except (httpx.HTTPError, EgressPolicyError, ValueError) as exc:
                        errors.append(f"BIMI collection: {exc}")

                    try:
                        payload = await self._json_get(
                            client,
                            CT_ENDPOINT,
                            params={"q": f"%.{target}", "output": "json"},
                            max_bytes=MAX_CT_BYTES,
                        )
                        if not isinstance(payload, list):
                            raise ValueError("Certificate Transparency endpoint returned an unexpected shape")
                        seen_certificates: set[str] = set()
                        seen_names: set[str] = set()
                        for item in payload[:500]:
                            if not isinstance(item, dict):
                                continue
                            cert_id = str(item.get("id") or item.get("serial_number") or "").strip()
                            if not cert_id or cert_id in seen_certificates:
                                continue
                            names = _ct_names(item, target)
                            if not names:
                                continue
                            seen_certificates.add(cert_id)
                            certificate_record = {
                                "id": cert_id[:200],
                                "serial_number": str(item.get("serial_number") or "")[:256],
                                "issuer_name": str(item.get("issuer_name") or "")[:1000],
                                "common_name": str(item.get("common_name") or "")[:253],
                                "entry_timestamp": str(item.get("entry_timestamp") or "")[:100],
                                "not_before": str(item.get("not_before") or "")[:100],
                                "not_after": str(item.get("not_after") or "")[:100],
                                "names": names,
                            }
                            certificates.append(certificate_record)
                            for name in names:
                                if name == target or name in seen_names:
                                    continue
                                seen_names.add(name)
                                relationships.append(
                                    {
                                        "kind": "certificate_san",
                                        "source": target,
                                        "target": name,
                                        "target_type": "domain",
                                        "label": "co-observed in CT certificate",
                                    }
                                )
                            if len(certificates) >= MAX_CT_CERTIFICATES:
                                break
                        successful_sources += 1
                    except RuntimeError as exc:
                        if str(exc) == "rate_limited":
                            rate_limited = True
                        errors.append(f"Certificate Transparency collection: {exc}")
                    except (httpx.HTTPError, EgressPolicyError, ValueError) as exc:
                        errors.append(f"Certificate Transparency collection: {exc}")
        except TimeoutError:
            return ExecutionResult(
                None,
                "",
                "",
                timed_out=True,
                error_code="timeout",
                error_message=f"Certificate & Email Relationships exceeded {timeout}s",
            )

        if successful_sources == 0:
            return ExecutionResult(
                None,
                "",
                "",
                rate_limited=rate_limited,
                error_code="rate_limited" if rate_limited else "upstream_error",
                error_message="; ".join(errors)[:500] or "No passive relationship source completed",
            )

        unique_relationships: list[dict[str, str]] = []
        seen_relationships: set[tuple[str, str, str]] = set()
        for item in relationships:
            relationship_key = (
                item["kind"],
                item["source"].casefold(),
                item["target"].casefold(),
            )
            if relationship_key in seen_relationships:
                continue
            seen_relationships.add(relationship_key)
            unique_relationships.append(item)

        record: dict[str, Any] = {
            "schema_version": "1.0",
            "queried_domain": target,
            "queried_at": datetime.now(UTC).isoformat(),
            "sources": {
                "dns_over_https": DOH_ENDPOINT,
                "certificate_transparency": CT_ENDPOINT,
            },
            "spf": spf_records,
            "dmarc": dmarc,
            "mta_sts": mta_sts,
            "bimi": bimi,
            "certificates": certificates,
            "relationships": unique_relationships,
            "errors": errors,
            "limits": {
                "spf_depth": MAX_SPF_DEPTH,
                "spf_domains": MAX_SPF_DOMAINS,
                "ct_certificates": MAX_CT_CERTIFICATES,
                "ct_names": MAX_CT_NAMES,
            },
        }
        path = output_dir / "domain-relationships.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        path.chmod(0o600)
        if log:
            await log(
                "stdout",
                f"Retained {len(unique_relationships)} passive relationship(s) and {len(certificates)} CT certificate observation(s)",
            )
        return ExecutionResult(
            0,
            json.dumps(
                {
                    "status": "found",
                    "relationships": len(unique_relationships),
                    "certificates": len(certificates),
                    "partial_errors": len(errors),
                }
            ),
            "",
            output_files=[path],
            rate_limited=rate_limited,
        )

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "domain-relationships.json"
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
        if not records:
            return []
        record = records[0]
        target = _domain(query)
        findings: list[FindingRecord] = [
            FindingRecord(
                query_type=query_type,
                queried_value=target,
                finding_type="domain_control_posture",
                platform="Passive DNS / CT",
                value=target,
                status=FindingStatus.FOUND,
                confidence=0.9,
                title=f"Certificate and email-control posture · {target}",
                description=(
                    "Passive DNS, published mail-policy metadata and Certificate Transparency observations. "
                    "Shared providers and shared certificates are relationship leads, not proof of common ownership."
                ),
                metadata={
                    "spf": record.get("spf", []),
                    "dmarc": record.get("dmarc", {}),
                    "mta_sts": record.get("mta_sts", {}),
                    "bimi": record.get("bimi", {}),
                    "errors": record.get("errors", []),
                    "queried_at": record.get("queried_at"),
                },
                source_tool=self.tool_id,
                source_record_id=f"summary:{target}",
                raw_output_reference=raw_reference,
            )
        ]

        for spf in record.get("spf", [])[:MAX_SPF_DOMAINS] if isinstance(record.get("spf"), list) else []:
            if not isinstance(spf, dict) or not spf.get("record"):
                continue
            domain = str(spf.get("domain") or "")
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=target,
                    finding_type="spf_record",
                    platform="DNS",
                    value=str(spf.get("record")),
                    status=FindingStatus.FOUND,
                    confidence=0.95,
                    title=f"SPF · {domain}",
                    description="Published SPF policy observed through passive DNS-over-HTTPS.",
                    metadata={"domain": domain, "depth": spf.get("depth")},
                    source_tool=self.tool_id,
                    source_record_id=f"spf:{domain}",
                    raw_output_reference=raw_reference,
                )
            )

        raw_dmarc = record.get("dmarc")
        dmarc: dict[str, Any] = raw_dmarc if isinstance(raw_dmarc, dict) else {}
        if dmarc.get("record"):
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=target,
                    finding_type="dmarc_policy",
                    platform="DNS",
                    value=str(dmarc["record"]),
                    status=FindingStatus.FOUND,
                    confidence=0.95,
                    title=f"DMARC · {target}",
                    description="Published DMARC policy and reporting destinations.",
                    metadata={
                        "policy": dmarc.get("policy"),
                        "subdomain_policy": dmarc.get("subdomain_policy"),
                        "reporting_addresses": dmarc.get("reporting_addresses", []),
                    },
                    source_tool=self.tool_id,
                    source_record_id=f"dmarc:{target}",
                    raw_output_reference=raw_reference,
                )
            )

        raw_mta_sts = record.get("mta_sts")
        mta_sts: dict[str, Any] = raw_mta_sts if isinstance(raw_mta_sts, dict) else {}
        if mta_sts.get("dns_record"):
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=target,
                    finding_type="mta_sts_policy",
                    platform="MTA-STS",
                    value=str(mta_sts.get("policy_url") or mta_sts.get("dns_record")),
                    canonical_url=str(mta_sts.get("policy_url") or "") or None,
                    status=FindingStatus.FOUND,
                    confidence=0.95,
                    title=f"MTA-STS · {target}",
                    description="Published MTA-STS declaration and bounded public policy document.",
                    metadata={"dns_record": mta_sts.get("dns_record"), "policy": mta_sts.get("policy")},
                    source_tool=self.tool_id,
                    source_record_id=f"mta-sts:{target}",
                    raw_output_reference=raw_reference,
                )
            )

        raw_bimi = record.get("bimi")
        bimi: dict[str, Any] = raw_bimi if isinstance(raw_bimi, dict) else {}
        if bimi.get("record"):
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=target,
                    finding_type="bimi_record",
                    platform="DNS",
                    value=str(bimi["record"]),
                    status=FindingStatus.FOUND,
                    confidence=0.9,
                    title=f"BIMI · {target}",
                    description="Published default-selector BIMI metadata; linked hosts are relationship leads only.",
                    metadata={"logo_url": bimi.get("logo_url"), "authority_url": bimi.get("authority_url")},
                    source_tool=self.tool_id,
                    source_record_id=f"bimi:{target}",
                    raw_output_reference=raw_reference,
                )
            )

        for certificate in record.get("certificates", [])[:MAX_CT_CERTIFICATES] if isinstance(record.get("certificates"), list) else []:
            if not isinstance(certificate, dict):
                continue
            cert_id = str(certificate.get("id") or "").strip()
            if not cert_id:
                continue
            names = [str(value) for value in certificate.get("names", [])[:MAX_CT_NAMES]] if isinstance(certificate.get("names"), list) else []
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=target,
                    finding_type="certificate_transparency_observation",
                    platform="Certificate Transparency",
                    value=cert_id,
                    canonical_url=f"https://crt.sh/?id={quote(cert_id, safe='')}",
                    status=FindingStatus.FOUND,
                    confidence=0.8,
                    title=str(certificate.get("common_name") or (names[0] if names else cert_id)),
                    description="Certificate-log observation; this does not prove the named host is currently live or commonly owned.",
                    metadata={
                        "serial_number": certificate.get("serial_number"),
                        "issuer_name": certificate.get("issuer_name"),
                        "entry_timestamp": certificate.get("entry_timestamp"),
                        "not_before": certificate.get("not_before"),
                        "not_after": certificate.get("not_after"),
                        "names": names,
                    },
                    source_tool=self.tool_id,
                    source_record_id=f"ct:{cert_id}",
                    raw_output_reference=raw_reference,
                )
            )

        confidence_by_kind = {
            "spf_include": 0.55,
            "spf_redirect": 0.7,
            "dmarc_reporting_address": 0.8,
            "mta_sts_policy_host": 0.9,
            "mta_sts_mx": 0.8,
            "bimi_logo_host": 0.65,
            "bimi_authority_host": 0.7,
            "certificate_san": 0.6,
        }
        for relationship in record.get("relationships", []) if isinstance(record.get("relationships"), list) else []:
            if not isinstance(relationship, dict):
                continue
            kind = str(relationship.get("kind") or "").strip()
            source = str(relationship.get("source") or "").strip()
            relation_target = str(relationship.get("target") or "").strip()
            target_type = str(relationship.get("target_type") or "domain").strip().casefold()
            label = str(relationship.get("label") or kind.replace("_", " ")).strip()
            if not kind or not source or not relation_target:
                continue
            finding_type = "email_control_email" if target_type == "email" else (
                "certificate_san_domain" if kind == "certificate_san" else "mail_control_domain"
            )
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=target,
                    finding_type=finding_type,
                    platform="Passive relationship",
                    value=relation_target,
                    status=FindingStatus.FOUND,
                    confidence=confidence_by_kind.get(kind, 0.6),
                    title=f"{label} · {relation_target}",
                    description=(
                        "Published relationship evidence. Common providers, delegated infrastructure and shared "
                        "certificates must not be treated as proof of common ownership without independent support."
                    ),
                    metadata={
                        "relationship": {
                            "kind": kind,
                            "source": source,
                            "source_type": "domain",
                            "target": relation_target,
                            "target_type": target_type,
                            "label": label,
                        }
                    },
                    source_tool=self.tool_id,
                    source_record_id=f"rel:{kind}:{source}:{relation_target}"[:500],
                    raw_output_reference=raw_reference,
                )
            )
        return findings
