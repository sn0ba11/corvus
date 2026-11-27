from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.models.schemas import FindingRecord, FindingStatus, ProfileEvidence, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

API_BASE = "https://api.github.com"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_SEARCH_RESULTS = 5
USER_AGENT = "Corvus/0.1 GitHub public discovery"


def _clean(value: Any, limit: int = 1000) -> str | None:
    if value in (None, ""):
        return None
    cleaned = " ".join(str(value).split())[:limit]
    return cleaned or None


class GitHubDiscoveryAdapter(Adapter):
    tool_id = "github_discovery"
    display_name = "GitHub Public Discovery"
    executable = "github-public-discovery"
    capabilities = frozenset({"username_search", "name_search", "email_search", "domain_search"})
    supported_query_types = frozenset(
        {QueryType.USERNAME, QueryType.NAME, QueryType.EMAIL, QueryType.DOMAIN}
    )
    default_selected = False
    default_timeout = 30
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "GitHub REST API")

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        if query_type not in self.supported_query_types:
            raise ValueError("unsupported GitHub discovery query type")
        target = query.strip()
        if not target or len(target) > 320:
            raise ValueError("invalid GitHub discovery query")
        command = [
            self.executable,
            "--query-type",
            query_type.value,
            "--query",
            target,
            "--output",
            str(output_dir / "github-public-discovery.json"),
        ]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    async def _get_json(self, client: httpx.AsyncClient, url: str) -> tuple[int, Any]:
        async with client.stream(
            "GET",
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
        ) as response:
            status_code = response.status_code
            if status_code in {403, 429} and response.headers.get("x-ratelimit-remaining") == "0":
                raise RuntimeError("rate_limited")
            if response.is_redirect:
                raise ValueError("GitHub API redirected unexpectedly")
            if status_code == 404:
                return status_code, None
            response.raise_for_status()
            raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
        try:
            return status_code, json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("GitHub API returned malformed JSON") from exc

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
                error_message="GitHub discovery command did not match the bounded adapter contract",
            )
        query_type = QueryType(command[2])
        target = command[4]
        if target != query.strip() or query_type not in self.supported_query_types:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="GitHub discovery query did not match the bounded adapter contract",
            )
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        request_timeout = min(20.0, max(5.0, float(timeout)))
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": USER_AGENT,
        }
        try:
            async with asyncio.timeout(timeout):
                client_context = (
                    httpx.AsyncClient(
                        transport=self.transport,
                        timeout=request_timeout,
                        follow_redirects=False,
                        trust_env=False,
                        headers=headers,
                    )
                    if self.transport is not None
                    else safe_client(
                        timeout=request_timeout,
                        user_agent=USER_AGENT,
                        proxy=proxy,
                    )
                )
                async with client_context as client:
                    profiles: list[dict[str, Any]] = []
                    if query_type == QueryType.USERNAME:
                        _status, payload = await self._get_json(
                            client, f"{API_BASE}/users/{quote(target, safe='')}"
                        )
                        if isinstance(payload, dict):
                            profiles.append(payload)
                    else:
                        search_term = f'"{target}" in:fullname' if query_type == QueryType.NAME else f'"{target}"'
                        _status, payload = await self._get_json(
                            client,
                            f"{API_BASE}/search/users?q={quote(search_term, safe='')}&per_page={MAX_SEARCH_RESULTS}",
                        )
                        items = payload.get("items") if isinstance(payload, dict) else None
                        if isinstance(items, list):
                            for item in items[:MAX_SEARCH_RESULTS]:
                                if not isinstance(item, dict):
                                    continue
                                login = str(item.get("login") or "").strip()
                                if not login:
                                    continue
                                _detail_status, detail = await self._get_json(
                                    client, f"{API_BASE}/users/{quote(login, safe='')}"
                                )
                                if isinstance(detail, dict):
                                    profiles.append(detail)
            path = output_dir / "github-public-discovery.json"
            record = {
                "status": "found" if profiles else "not_found",
                "query_type": query_type.value,
                "query": target,
                "profiles": profiles,
            }
            path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
            path.chmod(0o600)
            if log:
                await log("stdout", f"GitHub discovery retained {len(profiles)} public profiles")
            return ExecutionResult(0, json.dumps({"status": record["status"]}), "", output_files=[path])
        except TimeoutError:
            return ExecutionResult(
                None,
                "",
                "",
                timed_out=True,
                error_code="timeout",
                error_message=f"GitHub discovery exceeded {timeout}s",
            )
        except RuntimeError as exc:
            if str(exc) == "rate_limited":
                return ExecutionResult(
                    None,
                    "",
                    "",
                    rate_limited=True,
                    error_code="rate_limited",
                    error_message="GitHub search API rate limit was exhausted",
                )
            raise
        except (EgressPolicyError, ValueError) as exc:
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
                error_message=f"GitHub API returned HTTP {exc.response.status_code}",
            )
        except httpx.HTTPError:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="upstream_error",
                error_message="GitHub API request failed",
            )

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "github-public-discovery.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        profiles = payload.get("profiles") if isinstance(payload, dict) else None
        return [item for item in profiles[:MAX_SEARCH_RESULTS] if isinstance(item, dict)] if isinstance(profiles, list) else []

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        findings: list[FindingRecord] = []
        for record in records[:MAX_SEARCH_RESULTS]:
            login = _clean(record.get("login"), 100)
            profile_url = _clean(record.get("html_url"), 2048)
            if not login or not profile_url:
                continue
            exact = query_type == QueryType.USERNAME and login.casefold() == query.casefold().lstrip("@")
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="github_profile_discovery",
                    platform="GitHub",
                    value=login,
                    canonical_url=profile_url,
                    display_url=profile_url,
                    status=FindingStatus.FOUND if exact else FindingStatus.LIKELY,
                    confidence=0.98 if exact else 0.65,
                    title=_clean(record.get("name"), 300) or login,
                    description=_clean(record.get("bio"), 2000),
                    profile=ProfileEvidence(
                        avatar_url=_clean(record.get("avatar_url"), 2048),
                        display_name=_clean(record.get("name"), 300),
                        biography=_clean(record.get("bio"), 2000),
                        location=_clean(record.get("location"), 300),
                        account_id=str(record.get("id")) if record.get("id") is not None else None,
                        followers=record.get("followers") if isinstance(record.get("followers"), int) else None,
                        following=record.get("following") if isinstance(record.get("following"), int) else None,
                    ),
                    metadata={
                        key: record.get(key)
                        for key in (
                            "id",
                            "node_id",
                            "company",
                            "blog",
                            "email",
                            "twitter_username",
                            "public_repos",
                            "public_gists",
                            "created_at",
                            "updated_at",
                            "type",
                        )
                        if record.get(key) not in (None, "")
                    },
                    source_tool=self.tool_id,
                    source_record_id=str(record.get("id") or login),
                    raw_output_reference=raw_reference,
                )
            )
        return findings
