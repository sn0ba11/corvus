from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlsplit

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.models.schemas import FindingRecord, FindingStatus, ProfileEvidence, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

MAX_RESPONSE_BYTES = 512 * 1024
MAX_ALIASES = 20
USER_AGENT = "Corvus/0.1 federated identity resolver"
MATRIX_RE = re.compile(r"^@(?P<local>[^:\s]{1,255}):(?P<server>[^\s/]{1,255})$")
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::\d{1,5})?$")


def _clean(value: Any, limit: int = 1000) -> str | None:
    if value in (None, ""):
        return None
    cleaned = " ".join(str(value).split())[:limit]
    return cleaned or None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _https_url(value: Any) -> str | None:
    text = _clean(value, 2048)
    if not text:
        return None
    parsed = urlsplit(text)
    if parsed.scheme.casefold() != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return text


def _host_matches(url: str, expected: str) -> bool:
    host = (urlsplit(url).hostname or "").casefold().rstrip(".")
    expected_host = expected.casefold().rstrip(".").split(":", 1)[0]
    return host == expected_host or host.endswith(f".{expected_host}")


def _parse_target(value: str, query_type: QueryType) -> dict[str, str]:
    text = value.strip()
    matrix = MATRIX_RE.fullmatch(text)
    if matrix:
        server = matrix.group("server")
        if not HOST_RE.fullmatch(server):
            raise ValueError("invalid Matrix homeserver name")
        return {"kind": "matrix", "identifier": text, "server": server}

    candidate = text[1:] if text.startswith("@") else text
    if candidate.count("@") != 1:
        raise ValueError("federated identity requires user@domain, @user@domain, or @user:server")
    local, domain = candidate.rsplit("@", 1)
    if not local or len(local) > 255 or not HOST_RE.fullmatch(domain):
        raise ValueError("invalid federated identifier")
    if query_type not in {QueryType.USERNAME, QueryType.EMAIL}:
        raise ValueError("federated identity accepts username or email investigations only")
    return {
        "kind": "webfinger",
        "identifier": f"{local}@{domain}",
        "resource": f"acct:{local}@{domain}",
        "domain": domain,
    }


class FederatedIdentityAdapter(Adapter):
    tool_id = "federated_identity"
    display_name = "Federated Identity Resolver"
    executable = "federated-identity-resolver"
    capabilities = frozenset({"webfinger", "activitypub_profile", "matrix_public_profile"})
    # Normal Corvus username investigations accept bare handles only (for example, "alice").
    # This collector requires an exact federated identifier, so advertising generic USERNAME
    # compatibility causes every ordinary username search to queue an invalid run.  WebFinger
    # identifiers fit the existing EMAIL input contract; Matrix identifiers need their own exact-
    # identifier entry path rather than masquerading as a bare username collector.
    supported_query_types = frozenset({QueryType.EMAIL})
    default_selected = False
    default_timeout = 30
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "WebFinger / Matrix public profile")

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        target = _parse_target(query, query_type)
        command = [
            self.executable,
            "--query-type",
            query_type.value,
            "--identifier",
            target["identifier"],
            "--output",
            str(output_dir / "federated-identity.json"),
        ]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        accept: str = "application/json",
    ) -> tuple[int, Any]:
        async with client.stream("GET", url, headers={"Accept": accept}) as response:
            status_code = response.status_code
            if response.is_redirect:
                raise ValueError("federated identity endpoint redirected unexpectedly")
            if status_code in {403, 404}:
                raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
                try:
                    payload = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    payload = None
                return status_code, payload
            if status_code == 429:
                raise RuntimeError("rate_limited")
            response.raise_for_status()
            raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
        try:
            return status_code, json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("federated identity endpoint returned malformed JSON") from exc

    @staticmethod
    def _write_payload(output_dir: Path, payload: dict[str, Any]) -> Path:
        path = output_dir / "federated-identity.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        path.chmod(0o600)
        return path

    async def _resolve_webfinger(
        self,
        client: httpx.AsyncClient,
        target: dict[str, str],
        payload: dict[str, Any],
    ) -> None:
        params = urlencode({"resource": target["resource"]})
        endpoint = f"https://{target['domain']}/.well-known/webfinger?{params}"
        payload["endpoints"].append(endpoint)
        status, webfinger = await self._get_json(
            client,
            endpoint,
            accept="application/jrd+json, application/json;q=0.9",
        )
        payload["webfinger_status"] = status
        payload["webfinger"] = webfinger
        if status == 404:
            payload["status"] = "not_found"
            return
        if status == 403:
            payload["status"] = "access_restricted"
            return
        if not isinstance(webfinger, dict):
            raise ValueError("WebFinger response was not a JSON object")

        actor_url: str | None = None
        links = webfinger.get("links")
        if isinstance(links, list):
            for link in links:
                if not isinstance(link, dict):
                    continue
                rel = str(link.get("rel") or "").casefold()
                media_type = str(link.get("type") or "").casefold()
                href = _https_url(link.get("href"))
                if (
                    href
                    and rel == "self"
                    and ("activity+json" in media_type or "ld+json" in media_type)
                    and _host_matches(href, target["domain"])
                ):
                    actor_url = href
                    break
        if actor_url:
            payload["endpoints"].append(actor_url)
            actor_status, actor = await self._get_json(
                client,
                actor_url,
                accept="application/activity+json, application/ld+json;q=0.9, application/json;q=0.8",
            )
            payload["actor_status"] = actor_status
            if actor_status == 200 and isinstance(actor, dict):
                payload["actor"] = actor
        payload["status"] = "found"

    async def _resolve_matrix(
        self,
        client: httpx.AsyncClient,
        target: dict[str, str],
        payload: dict[str, Any],
    ) -> None:
        server = target["server"]
        discovery_url = f"https://{server}/.well-known/matrix/client"
        payload["endpoints"].append(discovery_url)
        status, discovery = await self._get_json(client, discovery_url)
        base_url = f"https://{server}"
        if status == 200 and isinstance(discovery, dict):
            homeserver = discovery.get("m.homeserver")
            if isinstance(homeserver, dict):
                discovered = _https_url(homeserver.get("base_url"))
                if discovered:
                    base_url = discovered.rstrip("/")
        elif status not in {403, 404}:
            raise ValueError("Matrix client discovery returned an unexpected response")
        payload["matrix_discovery_status"] = status
        payload["matrix_discovery"] = discovery
        payload["homeserver_base_url"] = base_url

        profile_url = urljoin(base_url.rstrip("/") + "/", f"_matrix/client/v3/profile/{quote(target['identifier'], safe='')}")
        payload["endpoints"].append(profile_url)
        profile_status, profile = await self._get_json(client, profile_url)
        payload["matrix_profile_status"] = profile_status
        payload["matrix_profile"] = profile
        if profile_status == 200 and isinstance(profile, dict):
            payload["status"] = "found"
        elif profile_status == 403:
            payload["status"] = "access_restricted"
        elif profile_status == 404:
            payload["status"] = "not_found"
        else:
            raise ValueError("Matrix profile endpoint returned an unexpected response")

    async def execute(
        self,
        command: list[str],
        output_dir: Path,
        timeout: int,
        query: str,
        log: LogCallback | None = None,
    ) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if len(command) < 7 or command[0] != self.executable:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="Federated Identity command did not match the bounded adapter contract",
            )
        try:
            query_type = QueryType(command[2])
            target = _parse_target(query, query_type)
        except (ValueError, KeyError) as exc:
            return ExecutionResult(None, "", "", error_code="invalid_query", error_message=str(exc))
        if command[4] != target["identifier"]:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="Federated Identity identifier did not match the bounded adapter contract",
            )
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        request_timeout = min(15.0, max(5.0, float(timeout)))
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "query_type": query_type.value,
            "query": query.strip(),
            "kind": target["kind"],
            "identifier": target["identifier"],
            "status": "unknown",
            "endpoints": [],
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
                    if target["kind"] == "matrix":
                        await self._resolve_matrix(client, target, payload)
                    else:
                        await self._resolve_webfinger(client, target, payload)
            path = self._write_payload(output_dir, payload)
            if log:
                await log(
                    "stdout",
                    f"Federated Identity resolved {target['identifier']} with status {payload['status']}",
                )
            return ExecutionResult(
                0,
                json.dumps({"status": payload["status"]}),
                "",
                output_files=[path],
            )
        except TimeoutError:
            payload["status"] = "timeout"
            path = self._write_payload(output_dir, payload)
            return ExecutionResult(
                None,
                "",
                "",
                output_files=[path],
                timed_out=True,
                error_code="timeout",
                error_message=f"Federated Identity exceeded {timeout}s",
            )
        except RuntimeError as exc:
            if str(exc) == "rate_limited":
                payload["status"] = "rate_limited"
                path = self._write_payload(output_dir, payload)
                return ExecutionResult(
                    None,
                    "",
                    "",
                    output_files=[path],
                    rate_limited=True,
                    error_code="rate_limited",
                    error_message="Federated identity endpoint rate-limited the request",
                )
            raise
        except (EgressPolicyError, ValueError) as exc:
            payload["status"] = "upstream_response_error"
            payload["error"] = str(exc)[:300]
            path = self._write_payload(output_dir, payload)
            return ExecutionResult(
                None,
                "",
                "",
                output_files=[path],
                error_code="upstream_response_error",
                error_message=str(exc),
            )
        except httpx.HTTPStatusError as exc:
            payload["status"] = "upstream_http_error"
            payload["http_status"] = exc.response.status_code
            path = self._write_payload(output_dir, payload)
            return ExecutionResult(
                None,
                "",
                "",
                output_files=[path],
                error_code="upstream_http_error",
                error_message=f"Federated identity endpoint returned HTTP {exc.response.status_code}",
            )
        except httpx.HTTPError:
            payload["status"] = "upstream_error"
            path = self._write_payload(output_dir, payload)
            return ExecutionResult(
                None,
                "",
                "",
                output_files=[path],
                error_code="upstream_error",
                error_message="Federated identity request failed",
            )

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "federated-identity.json"
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
        identifier = str(payload.get("identifier") or query).strip()
        findings: list[FindingRecord] = []

        if payload.get("kind") == "matrix":
            profile = _as_dict(payload.get("matrix_profile"))
            display_name = _clean(profile.get("displayname"), 300)
            avatar = _clean(profile.get("avatar_url"), 2048)
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="matrix_public_profile",
                    platform="Matrix",
                    value=identifier,
                    status=FindingStatus.FOUND,
                    confidence=0.95,
                    title=display_name or identifier,
                    profile=ProfileEvidence(
                        display_name=display_name,
                        avatar_url=avatar,
                        account_id=identifier,
                    ),
                    metadata={
                        "homeserver_base_url": payload.get("homeserver_base_url"),
                        "discovery_status": payload.get("matrix_discovery_status"),
                        "source_scope": "public_unauthenticated_matrix_profile",
                    },
                    source_tool=self.tool_id,
                    source_record_id=f"matrix:{identifier.casefold()}",
                    raw_output_reference=raw_reference,
                )
            )
            return findings

        webfinger = _as_dict(payload.get("webfinger"))
        actor = _as_dict(payload.get("actor"))
        subject = _clean(webfinger.get("subject"), 500) or f"acct:{identifier}"
        actor_id = _https_url(actor.get("id"))
        actor_url_value = actor.get("url")
        actor_url = _https_url(actor_url_value) if isinstance(actor_url_value, str) else None
        if actor_url is None and isinstance(actor_url_value, list):
            actor_url = next((_https_url(item) for item in actor_url_value if _https_url(item)), None)
        self_link: str | None = None
        links = webfinger.get("links")
        if isinstance(links, list):
            for link in links:
                if isinstance(link, dict) and str(link.get("rel") or "").casefold() == "self":
                    self_link = _https_url(link.get("href"))
                    if self_link:
                        break
        canonical = actor_id or actor_url or self_link
        display_name = _clean(actor.get("name"), 300)
        preferred_username = _clean(actor.get("preferredUsername"), 300)
        icon = _as_dict(actor.get("icon"))
        avatar = _https_url(icon.get("url"))
        findings.append(
            FindingRecord(
                query_type=query_type,
                queried_value=query,
                finding_type="federated_public_profile",
                platform="Federated / ActivityPub",
                value=identifier,
                canonical_url=canonical,
                display_url=canonical,
                status=FindingStatus.FOUND,
                confidence=0.95,
                title=display_name or preferred_username or identifier,
                profile=ProfileEvidence(
                    display_name=display_name or preferred_username,
                    avatar_url=avatar,
                    account_id=subject,
                ),
                metadata={
                    "webfinger_subject": subject,
                    "actor_type": _clean(actor.get("type"), 100),
                    "preferred_username": preferred_username,
                    "source_scope": "public_webfinger_activitypub",
                },
                source_tool=self.tool_id,
                source_record_id=f"federated:{identifier.casefold()}",
                raw_output_reference=raw_reference,
            )
        )

        alias_values: list[tuple[str, str]] = []
        for alias in webfinger.get("aliases", []) if isinstance(webfinger.get("aliases"), list) else []:
            url = _https_url(alias)
            if url:
                alias_values.append(("webfinger_alias", url))
        for alias in actor.get("alsoKnownAs", []) if isinstance(actor.get("alsoKnownAs"), list) else []:
            url = _https_url(alias)
            if url:
                alias_values.append(("activitypub_alias", url))
        seen: set[str] = set()
        for alias_kind, alias in alias_values[:MAX_ALIASES]:
            if alias in seen:
                continue
            seen.add(alias)
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="federated_alias",
                    platform="Federated / ActivityPub",
                    value=alias,
                    canonical_url=alias,
                    display_url=alias,
                    status=FindingStatus.FOUND,
                    confidence=0.75,
                    title=alias,
                    description="Declared alias relationship; this is a correlation lead, not independent proof of common control.",
                    metadata={"alias_kind": alias_kind, "source_subject": subject},
                    source_tool=self.tool_id,
                    source_record_id=f"alias:{alias}",
                    raw_output_reference=raw_reference,
                )
            )
        moved_to = _https_url(actor.get("movedTo"))
        if moved_to:
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="federated_migration_target",
                    platform="Federated / ActivityPub",
                    value=moved_to,
                    canonical_url=moved_to,
                    display_url=moved_to,
                    status=FindingStatus.FOUND,
                    confidence=0.9,
                    title=moved_to,
                    metadata={"source_subject": subject, "relationship": "moved_to"},
                    source_tool=self.tool_id,
                    source_record_id=f"moved-to:{moved_to}",
                    raw_output_reference=raw_reference,
                )
            )
        return findings
