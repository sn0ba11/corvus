from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.models.schemas import FindingRecord, FindingStatus, ProfileEvidence, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

TIKTOK_ORIGIN = "https://www.tiktok.com"
TIKTOK_HYDRATION_ID = "__UNIVERSAL_DATA_FOR_REHYDRATION__"
TIKTOK_PROFILE_SCOPE = "webapp.user-detail"
MAX_RESPONSE_BYTES = 1024 * 1024
CURRENT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)


class _HydrationScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._capture = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "script":
            return
        values = {key.casefold(): value for key, value in attrs}
        if values.get("id") == TIKTOK_HYDRATION_ID:
            self._capture = True

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._capture:
            self._capture = False

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    @property
    def payload(self) -> str:
        return "".join(self._parts).strip()


def parse_tiktok_hydration(html: str, expected_username: str) -> dict[str, Any]:
    parser = _HydrationScriptParser()
    parser.feed(html)
    if not parser.payload:
        raise ValueError("TikTok hydration JSON was not present")
    try:
        root = json.loads(parser.payload)
    except json.JSONDecodeError as exc:
        raise ValueError("TikTok hydration JSON was malformed") from exc
    try:
        user_info = root["__DEFAULT_SCOPE__"][TIKTOK_PROFILE_SCOPE]["userInfo"]
    except (KeyError, TypeError) as exc:
        raise ValueError("TikTok profile data was absent from hydration JSON") from exc
    if not isinstance(user_info, dict):
        raise ValueError("TikTok profile data had an unexpected shape")
    user = user_info.get("user")
    stats = user_info.get("statsV2") or user_info.get("stats")
    if not isinstance(user, dict) or not isinstance(stats, dict):
        raise ValueError("TikTok profile user or stats data was absent")
    unique_id = str(user.get("uniqueId") or "").strip()
    if unique_id.casefold() != expected_username.casefold():
        raise ValueError("TikTok returned a different profile identifier")
    return {
        "username": unique_id,
        "account_id": str(user.get("id") or "").strip() or None,
        "display_name": str(user.get("nickname") or "").strip() or None,
        "biography": str(user.get("signature") or "").strip() or None,
        "avatar_url": str(
            user.get("avatarLarger") or user.get("avatarMedium") or user.get("avatarThumb") or ""
        ).strip()
        or None,
        "avatar_medium": str(user.get("avatarMedium") or "").strip() or None,
        "avatar_thumb": str(user.get("avatarThumb") or "").strip() or None,
        "verified": bool(user.get("verified")),
        "private_account": bool(user.get("privateAccount")),
        "followers": _bounded_int(stats.get("followerCount")),
        "following": _bounded_int(stats.get("followingCount")),
        "likes": _bounded_int(stats.get("heartCount") or stats.get("heart")),
        "video_count": _bounded_int(stats.get("videoCount")),
    }


def _bounded_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0 <= parsed <= 10**15 else None


class TikTokPublicAdapter(Adapter):
    tool_id = "tiktok_public"
    display_name = "TikTok Public Profile"
    executable = "tiktok-public-profile"
    capabilities = frozenset({"username_search", "tiktok_profile", "profile_image"})
    supported_query_types = frozenset({QueryType.USERNAME})
    default_timeout = 45
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport
        self.cancel_requested = False

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "native public-profile parser")

    async def cancel(self) -> None:
        self.cancel_requested = True
        await super().cancel()

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        if query_type != QueryType.USERNAME:
            raise ValueError("TikTok Public Profile accepts username investigations only")
        username = query.strip().removeprefix("@").strip()
        if not username or len(username) > 64 or any(ch.isspace() for ch in username):
            raise ValueError("invalid TikTok username")
        command = [
            self.executable,
            "--username",
            username,
            "--output",
            str(output_dir / "tiktok-public-profile.json"),
            "--timeout",
            str(timeout),
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
        username = query.strip().removeprefix("@").strip()
        if len(command) < 7 or command[0] != self.executable or command[2] != username:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="TikTok command did not match the bounded adapter contract",
            )
        self.cancel_requested = False
        profile_url = f"{TIKTOK_ORIGIN}/@{quote(username, safe='._-')}"
        started_at = datetime.now(UTC)
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        try:
            async with asyncio.timeout(timeout):
                request_timeout = min(20.0, max(5.0, float(timeout)))
                client_context = (
                    httpx.AsyncClient(
                        transport=self.transport,
                        timeout=request_timeout,
                        follow_redirects=False,
                        proxy=proxy,
                        trust_env=False,
                        headers={
                            "Accept": "text/html,application/xhtml+xml",
                            "Accept-Language": "en-IE,en;q=0.9",
                            "User-Agent": CURRENT_USER_AGENT,
                        },
                    )
                    if self.transport is not None
                    else safe_client(
                        timeout=request_timeout,
                        user_agent=CURRENT_USER_AGENT,
                        proxy=proxy,
                    )
                )
                async with client_context as client:
                    async with client.stream("GET", profile_url) as response:
                        if response.status_code == 429:
                            return ExecutionResult(
                                None,
                                "",
                                "TikTok returned HTTP 429",
                                rate_limited=True,
                                error_code="rate_limited",
                                error_message="TikTok rate limited the public profile request",
                            )
                        if response.is_redirect:
                            return ExecutionResult(
                                None,
                                "",
                                "",
                                error_code="blocked_or_redirected",
                                error_message="TikTok redirected the public profile request",
                            )
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").casefold()
                        if "text/html" not in content_type:
                            return ExecutionResult(
                                None,
                                "",
                                "",
                                error_code="unexpected_content",
                                error_message="TikTok returned a non-HTML response",
                            )
                        content = await read_bounded_response(
                            response, max_bytes=MAX_RESPONSE_BYTES
                        )
            if self.cancel_requested:
                return ExecutionResult(
                    None,
                    "",
                    "",
                    cancelled=True,
                    error_code="cancelled",
                    error_message="TikTok public profile collection was cancelled",
                )
            html = content.decode("utf-8", errors="replace")
            record = parse_tiktok_hydration(html, username)
            record.update(
                {
                    "site": "TikTok",
                    "status": "found",
                    "url": profile_url,
                    "retrieved_at": started_at.isoformat(),
                    "source_scope": "public_profile_html_hydration_json",
                }
            )
            output_path = output_dir / "tiktok-public-profile.json"
            output_path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
            output_path.chmod(0o600)
            if log:
                await log("stdout", f"TikTok public profile parsed for @{username}")
            return ExecutionResult(
                0,
                json.dumps({"status": "found", "username": username}),
                "",
                output_files=[output_path],
            )
        except TimeoutError:
            return ExecutionResult(
                None,
                "",
                "",
                timed_out=True,
                error_code="timeout",
                error_message=f"TikTok public profile request exceeded {timeout}s",
            )
        except EgressPolicyError as exc:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="response_too_large",
                error_message=str(exc),
            )
        except ValueError as exc:
            message = str(exc)
            if message in {
                "TikTok hydration JSON was not present",
                "TikTok profile data was absent from hydration JSON",
                "TikTok profile user or stats data was absent",
            }:
                if log:
                    await log("output", f"TikTok profile could not be confirmed: {message}")
                return ExecutionResult(
                    0,
                    json.dumps({"status": "unknown", "username": username}),
                    message,
                )
            return ExecutionResult(
                None,
                "",
                "",
                error_code="profile_unavailable",
                error_message=message,
            )
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            return ExecutionResult(
                None,
                "",
                "",
                error_code="upstream_http_error",
                error_message=f"TikTok returned HTTP {code}",
            )
        except httpx.HTTPError as exc:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="upstream_error",
                error_message=f"TikTok public profile request failed: {exc}",
            )

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "tiktok-public-profile.json"
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
            username = str(record.get("username") or "").strip()
            url = str(record.get("url") or "").strip()
            if not username or not url:
                continue
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="tiktok_profile",
                    platform="TikTok",
                    value=username,
                    canonical_url=url,
                    display_url=url,
                    status=FindingStatus.FOUND,
                    confidence=0.95,
                    title=str(record.get("display_name") or username),
                    description=str(record.get("biography") or "") or None,
                    profile=ProfileEvidence(
                        avatar_url=record.get("avatar_url"),
                        display_name=record.get("display_name"),
                        biography=record.get("biography"),
                        account_id=record.get("account_id"),
                        verified=record.get("verified"),
                        followers=record.get("followers"),
                        following=record.get("following"),
                    ),
                    metadata={
                        "private_account": record.get("private_account"),
                        "likes": record.get("likes"),
                        "video_count": record.get("video_count"),
                        "avatar_medium": record.get("avatar_medium"),
                        "avatar_thumb": record.get("avatar_thumb"),
                        "source_scope": record.get("source_scope"),
                        "retrieved_at": record.get("retrieved_at"),
                    },
                    source_tool=self.tool_id,
                    source_record_id=str(record.get("account_id") or username),
                    raw_output_reference=raw_reference,
                )
            )
        return findings
