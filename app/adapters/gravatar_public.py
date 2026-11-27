from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.models.schemas import FindingRecord, FindingStatus, ProfileEvidence, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

API_BASE = "https://api.gravatar.com/v3/profiles"
MAX_RESPONSE_BYTES = 512 * 1024
USER_AGENT = "Corvus/0.1 Gravatar public-profile collector"


def gravatar_identifier(email: str) -> str:
    normalised = email.strip().casefold()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _text(value: Any, limit: int = 1000) -> str | None:
    if value in (None, ""):
        return None
    result = " ".join(str(value).split())[:limit]
    return result or None


class GravatarPublicAdapter(Adapter):
    tool_id = "gravatar_public"
    display_name = "Gravatar Public Profile"
    executable = "gravatar-public-profile"
    capabilities = frozenset({"email_search", "public_profile", "profile_image"})
    supported_query_types = frozenset({QueryType.EMAIL})
    # One unauthenticated request to a public directory, so it runs with every email search.
    default_selected = True
    default_timeout = 30
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "Gravatar Public API v3")

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        if query_type != QueryType.EMAIL or "@" not in query:
            raise ValueError("Gravatar accepts email investigations only")
        command = [
            self.executable,
            "--identifier",
            gravatar_identifier(query),
            "--output",
            str(output_dir / "gravatar-public-profile.json"),
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
        identifier = gravatar_identifier(query)
        if len(command) < 5 or command[0] != self.executable or command[2] != identifier:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="Gravatar command did not match the bounded adapter contract",
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
                        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                    )
                    if self.transport is not None
                    else safe_client(
                        timeout=request_timeout,
                        user_agent=USER_AGENT,
                        proxy=proxy,
                    )
                )
                async with client_context as client:
                    async with client.stream("GET", f"{API_BASE}/{identifier}") as response:
                        if response.status_code == 404:
                            return ExecutionResult(
                                0,
                                json.dumps({"status": "not_found", "identifier": identifier}),
                                "",
                            )
                        if response.status_code == 429:
                            return ExecutionResult(
                                None,
                                "",
                                "",
                                rate_limited=True,
                                error_code="rate_limited",
                                error_message="Gravatar rate limited the profile request",
                            )
                        if response.is_redirect:
                            raise ValueError("Gravatar API redirected unexpectedly")
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").casefold()
                        if "json" not in content_type:
                            raise ValueError("Gravatar API returned a non-JSON response")
                        raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Gravatar API returned an unexpected JSON shape")
            payload["status"] = "found"
            payload["identifier"] = identifier
            path = output_dir / "gravatar-public-profile.json"
            path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            path.chmod(0o600)
            if log:
                await log("stdout", "Gravatar public profile found")
            return ExecutionResult(0, json.dumps({"status": "found"}), "", output_files=[path])
        except TimeoutError:
            return ExecutionResult(
                None,
                "",
                "",
                timed_out=True,
                error_code="timeout",
                error_message=f"Gravatar request exceeded {timeout}s",
            )
        except (EgressPolicyError, ValueError, json.JSONDecodeError) as exc:
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
                error_message=f"Gravatar returned HTTP {exc.response.status_code}",
            )
        except httpx.HTTPError:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="upstream_error",
                error_message="Gravatar request failed",
            )

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "gravatar-public-profile.json"
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
            identifier = str(record.get("identifier") or gravatar_identifier(query))
            profile_url = _text(record.get("profile_url") or record.get("profileUrl"), 2048)
            avatar_url = _text(record.get("avatar_url") or record.get("avatarUrl"), 2048)
            display_name = _text(record.get("display_name") or record.get("displayName"), 300)
            biography = _text(record.get("description") or record.get("about_me"), 2000)
            location = _text(record.get("location"), 300)
            if not any((profile_url, avatar_url, display_name, biography, location)):
                continue
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="gravatar_profile",
                    platform="Gravatar",
                    value=display_name or query,
                    canonical_url=profile_url,
                    display_url=profile_url,
                    status=FindingStatus.FOUND,
                    confidence=0.9,
                    title=display_name or "Gravatar public profile",
                    description=biography,
                    profile=ProfileEvidence(
                        avatar_url=avatar_url,
                        display_name=display_name,
                        biography=biography,
                        location=location,
                        account_id=identifier,
                    ),
                    metadata={
                        "gravatar_identifier_sha256": identifier,
                        "verified_accounts": record.get("verified_accounts"),
                        "links": record.get("links"),
                        "pronunciation": record.get("pronunciation"),
                        "timezone": record.get("timezone"),
                    },
                    source_tool=self.tool_id,
                    source_record_id=identifier,
                    raw_output_reference=raw_reference,
                )
            )
        return findings
