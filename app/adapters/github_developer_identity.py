from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

API_BASE = "https://api.github.com"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REPOSITORIES = 3
MAX_COMMITS_PER_REPOSITORY = 5
MAX_KEYS = 20
USER_AGENT = "Corvus/0.1 GitHub developer identity"
USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


def _clean(value: Any, limit: int = 1000) -> str | None:
    if value in (None, ""):
        return None
    cleaned = " ".join(str(value).split())[:limit]
    return cleaned or None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _username(value: str) -> str:
    candidate = value.strip().lstrip("@")
    if not USERNAME_RE.fullmatch(candidate) or "--" in candidate:
        raise ValueError("GitHub developer identity requires one valid GitHub username")
    return candidate


def _ssh_fingerprint(value: str) -> str:
    parts = value.strip().split()
    candidate = parts[1] if len(parts) >= 2 and parts[0].startswith("ssh-") else parts[-1]
    try:
        raw = base64.b64decode(candidate.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        return f"SHA256-TEXT:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
    return f"SHA256:{digest}"


class GitHubDeveloperIdentityAdapter(Adapter):
    tool_id = "github_developer_identity"
    display_name = "GitHub Developer Identity"
    executable = "github-developer-identity"
    capabilities = frozenset(
        {
            "public_commit_identity",
            "public_signing_keys",
            "bounded_repository_history",
        }
    )
    supported_query_types = frozenset({QueryType.USERNAME})
    default_selected = False
    default_timeout = 45
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
        if query_type != QueryType.USERNAME:
            raise ValueError("GitHub Developer Identity accepts username investigations only")
        username = _username(query)
        command = [
            self.executable,
            "--username",
            username,
            "--output",
            str(output_dir / "github-developer-identity.json"),
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
            remaining = response.headers.get("x-ratelimit-remaining")
            if status_code in {403, 429} and (remaining == "0" or status_code == 429):
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

    @staticmethod
    def _write_payload(output_dir: Path, payload: dict[str, Any]) -> Path:
        path = output_dir / "github-developer-identity.json"
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
            username = _username(query)
        except ValueError as exc:
            return ExecutionResult(None, "", "", error_code="invalid_query", error_message=str(exc))
        if len(command) < 5 or command[0] != self.executable or command[2].casefold() != username.casefold():
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="GitHub developer identity command did not match the bounded adapter contract",
            )
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        request_timeout = min(20.0, max(5.0, float(timeout)))
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": USER_AGENT,
        }
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "query": username,
            "limits": {
                "repositories": MAX_REPOSITORIES,
                "commits_per_repository": MAX_COMMITS_PER_REPOSITORY,
                "keys_per_type": MAX_KEYS,
                "response_bytes": MAX_RESPONSE_BYTES,
            },
            "profile": None,
            "repositories": [],
            "gpg_keys": [],
            "ssh_signing_keys": [],
            "commit_batches": [],
            "request_count": 0,
        }
        client_context = (
            httpx.AsyncClient(
                transport=self.transport,
                timeout=request_timeout,
                follow_redirects=False,
                trust_env=False,
                headers=headers,
            )
            if self.transport is not None
            else safe_client(timeout=request_timeout, user_agent=USER_AGENT, proxy=proxy)
        )
        try:
            async with asyncio.timeout(timeout):
                async with client_context as client:
                    payload["request_count"] += 1
                    _status, profile = await self._get_json(
                        client, f"{API_BASE}/users/{quote(username, safe='')}"
                    )
                    if not isinstance(profile, dict):
                        payload["status"] = "not_found"
                        path = self._write_payload(output_dir, payload)
                        return ExecutionResult(
                            0,
                            json.dumps({"status": "not_found"}),
                            "",
                            output_files=[path],
                        )
                    payload["profile"] = profile
                    login = str(profile.get("login") or username).strip()
                    encoded_login = quote(login, safe="")

                    payload["request_count"] += 1
                    _status, repositories = await self._get_json(
                        client,
                        f"{API_BASE}/users/{encoded_login}/repos?type=owner&sort=pushed&direction=desc&per_page={MAX_REPOSITORIES}",
                    )
                    if isinstance(repositories, list):
                        payload["repositories"] = repositories[:MAX_REPOSITORIES]

                    payload["request_count"] += 1
                    _status, gpg_keys = await self._get_json(
                        client,
                        f"{API_BASE}/users/{encoded_login}/gpg_keys?per_page={MAX_KEYS}",
                    )
                    if isinstance(gpg_keys, list):
                        payload["gpg_keys"] = gpg_keys[:MAX_KEYS]

                    payload["request_count"] += 1
                    _status, ssh_keys = await self._get_json(
                        client,
                        f"{API_BASE}/users/{encoded_login}/ssh_signing_keys?per_page={MAX_KEYS}",
                    )
                    if isinstance(ssh_keys, list):
                        payload["ssh_signing_keys"] = ssh_keys[:MAX_KEYS]

                    for repository in payload["repositories"][:MAX_REPOSITORIES]:
                        if not isinstance(repository, dict):
                            continue
                        full_name = str(repository.get("full_name") or "").strip()
                        if not full_name or "/" not in full_name:
                            continue
                        owner, repo = full_name.split("/", 1)
                        payload["request_count"] += 1
                        _status, commits = await self._get_json(
                            client,
                            f"{API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/commits?author={encoded_login}&per_page={MAX_COMMITS_PER_REPOSITORY}",
                        )
                        payload["commit_batches"].append(
                            {
                                "repository": full_name,
                                "repository_url": repository.get("html_url"),
                                "commits": commits[:MAX_COMMITS_PER_REPOSITORY]
                                if isinstance(commits, list)
                                else [],
                            }
                        )
            payload["status"] = "found"
            path = self._write_payload(output_dir, payload)
            if log:
                commit_count = sum(
                    len(batch.get("commits", []))
                    for batch in payload["commit_batches"]
                    if isinstance(batch, dict)
                )
                await log(
                    "stdout",
                    "GitHub Developer Identity retained "
                    f"{len(payload['repositories'])} repositor(ies), {commit_count} commit(s), "
                    f"{len(payload['gpg_keys'])} GPG key(s), and {len(payload['ssh_signing_keys'])} SSH signing key(s)",
                )
            return ExecutionResult(
                0,
                json.dumps({"status": "found", "request_count": payload["request_count"]}),
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
                error_message=f"GitHub Developer Identity exceeded {timeout}s",
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
                    error_message="GitHub public API rate limit was exhausted",
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
                error_message=f"GitHub API returned HTTP {exc.response.status_code}",
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
                error_message="GitHub API request failed",
            )

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "github-developer-identity.json"
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
        username = _username(query)
        findings: list[FindingRecord] = []

        profile = _as_dict(payload.get("profile"))
        profile_url = _clean(profile.get("html_url"), 2048) or f"https://github.com/{username}"
        findings.append(
            FindingRecord(
                query_type=query_type,
                queried_value=query,
                finding_type="github_developer_identity_summary",
                platform="GitHub",
                value=username,
                canonical_url=profile_url,
                display_url=profile_url,
                status=FindingStatus.FOUND,
                confidence=0.98,
                title=_clean(profile.get("name"), 300) or username,
                description="Bounded public GitHub repository, commit-identity and signing-key enrichment.",
                metadata={
                    "repository_count_sampled": len(payload.get("repositories", [])),
                    "gpg_key_count": len(payload.get("gpg_keys", [])),
                    "ssh_signing_key_count": len(payload.get("ssh_signing_keys", [])),
                    "request_count": payload.get("request_count"),
                    "source_scope": "public_github_api_bounded",
                },
                source_tool=self.tool_id,
                source_record_id=f"github-developer:{username.casefold()}",
                raw_output_reference=raw_reference,
            )
        )

        for key in payload.get("gpg_keys", [])[:MAX_KEYS]:
            if not isinstance(key, dict):
                continue
            key_id = _clean(key.get("key_id"), 200) or _clean(key.get("id"), 200)
            if not key_id:
                continue
            public_key = str(key.get("public_key") or key.get("raw_key") or "")
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="github_gpg_signing_key",
                    platform="GitHub",
                    value=key_id,
                    canonical_url=profile_url,
                    display_url=profile_url,
                    status=FindingStatus.FOUND,
                    confidence=0.98,
                    title=f"GPG signing key {key_id}",
                    metadata={
                        "key_id": key.get("key_id"),
                        "primary_key_id": key.get("primary_key_id"),
                        "created_at": key.get("created_at"),
                        "expires_at": key.get("expires_at"),
                        "revoked": key.get("revoked"),
                        "can_sign": key.get("can_sign"),
                        "emails": key.get("emails") if isinstance(key.get("emails"), list) else [],
                        "public_key_sha256": hashlib.sha256(public_key.encode("utf-8")).hexdigest()
                        if public_key
                        else None,
                        "source_scope": "public_github_signing_key",
                    },
                    source_tool=self.tool_id,
                    source_record_id=f"gpg:{key_id}",
                    raw_output_reference=raw_reference,
                )
            )

        for key in payload.get("ssh_signing_keys", [])[:MAX_KEYS]:
            if not isinstance(key, dict):
                continue
            key_value = _clean(key.get("key"), 4096)
            key_id = _clean(key.get("id"), 200)
            if not key_value:
                continue
            fingerprint = _ssh_fingerprint(key_value)
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="github_ssh_signing_key",
                    platform="GitHub",
                    value=fingerprint,
                    canonical_url=profile_url,
                    display_url=profile_url,
                    status=FindingStatus.FOUND,
                    confidence=0.98,
                    title=f"SSH signing key {fingerprint}",
                    metadata={
                        "github_key_id": key_id,
                        "title": _clean(key.get("title"), 300),
                        "created_at": key.get("created_at"),
                        "fingerprint": fingerprint,
                        "source_scope": "public_github_signing_key",
                    },
                    source_tool=self.tool_id,
                    source_record_id=f"ssh-signing:{key_id or fingerprint}",
                    raw_output_reference=raw_reference,
                )
            )

        for batch in payload.get("commit_batches", []):
            if not isinstance(batch, dict):
                continue
            repository = _clean(batch.get("repository"), 300)
            repository_url = _clean(batch.get("repository_url"), 2048)
            for commit in batch.get("commits", [])[:MAX_COMMITS_PER_REPOSITORY]:
                if not isinstance(commit, dict):
                    continue
                sha = _clean(commit.get("sha"), 100)
                commit_payload = _as_dict(commit.get("commit"))
                verification = _as_dict(commit_payload.get("verification"))
                commit_url = f"{repository_url}/commit/{sha}" if repository_url and sha else repository_url
                for role in ("author", "committer"):
                    identity = _as_dict(commit_payload.get(role))
                    name = _clean(identity.get("name"), 300)
                    email = _clean(identity.get("email"), 320)
                    date = _clean(identity.get("date"), 100)
                    account = _as_dict(commit.get(role))
                    account_login = _clean(account.get("login"), 100)
                    if not any((name, email, account_login)):
                        continue
                    value = email or account_login or name
                    if not value:
                        continue
                    noreply = bool(email and email.casefold().endswith("@users.noreply.github.com"))
                    findings.append(
                        FindingRecord(
                            query_type=query_type,
                            queried_value=query,
                            finding_type="github_commit_identity",
                            platform="GitHub",
                            value=value,
                            canonical_url=commit_url,
                            display_url=commit_url,
                            status=FindingStatus.FOUND,
                            confidence=0.92 if account_login and account_login.casefold() == username.casefold() else 0.72,
                            title=f"{role.title()} identity: {name or value}",
                            description=(
                                "Public commit metadata; a matching name or email is a correlation signal, not proof of account ownership."
                            ),
                            metadata={
                                "repository": repository,
                                "commit_sha": sha,
                                "role": role,
                                "name": name,
                                "email": email,
                                "date": date,
                                "github_account_login": account_login,
                                "github_account_id": account.get("id"),
                                "noreply_email": noreply,
                                "signature_verified": verification.get("verified"),
                                "signature_reason": verification.get("reason"),
                                "signature_verified_at": verification.get("verified_at"),
                                "source_scope": "public_git_commit_metadata",
                            },
                            source_tool=self.tool_id,
                            source_record_id=f"commit:{repository}:{sha}:{role}",
                            raw_output_reference=raw_reference,
                        )
                    )
        return findings
