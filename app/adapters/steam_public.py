from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.db.session import SessionLocal
from app.models.schemas import FindingRecord, FindingStatus, ProfileEvidence, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client
from app.services.steam_settings import get_steam_api_key

STEAM_API_ORIGIN = "https://api.steampowered.com"
RESOLVE_ENDPOINT = f"{STEAM_API_ORIGIN}/ISteamUser/ResolveVanityURL/v1/"
SUMMARY_ENDPOINT = f"{STEAM_API_ORIGIN}/ISteamUser/GetPlayerSummaries/v2/"
MAX_RESPONSE_BYTES = 512 * 1024
STEAM_ID64_RE = re.compile(r"^7656119\d{10}$")
VANITY_RE = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")
USER_AGENT = "Corvus/0.1 Steam public-profile collector"


def _clean_text(value: Any, limit: int = 500) -> str | None:
    if value in (None, ""):
        return None
    cleaned = " ".join(str(value).split())[:limit]
    return cleaned or None


def _bounded_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0 <= parsed <= 2**63 - 1 else None


def _profile_record(player: dict[str, Any], queried_value: str) -> dict[str, Any]:
    steam_id = str(player.get("steamid") or "").strip()
    if not STEAM_ID64_RE.fullmatch(steam_id):
        raise ValueError("Steam returned an invalid SteamID64")
    persona = _clean_text(player.get("personaname"), 200)
    profile_url = _clean_text(player.get("profileurl"), 1000)
    if not profile_url or not profile_url.startswith("https://steamcommunity.com/"):
        profile_url = f"https://steamcommunity.com/profiles/{steam_id}/"
    return {
        "site": "Steam",
        "status": "found",
        "queried_value": queried_value,
        "steam_id": steam_id,
        "username": queried_value if not STEAM_ID64_RE.fullmatch(queried_value) else None,
        "display_name": persona,
        "real_name": _clean_text(player.get("realname"), 200),
        "country_code": _clean_text(player.get("loccountrycode"), 8),
        "state_code": _clean_text(player.get("locstatecode"), 32),
        "city_id": _bounded_int(player.get("loccityid")),
        "profile_url": profile_url,
        "avatar_url": _clean_text(player.get("avatarfull"), 1000)
        or _clean_text(player.get("avatarmedium"), 1000)
        or _clean_text(player.get("avatar"), 1000),
        "avatar_medium": _clean_text(player.get("avatarmedium"), 1000),
        "avatar_thumb": _clean_text(player.get("avatar"), 1000),
        "community_visibility_state": _bounded_int(player.get("communityvisibilitystate")),
        "profile_state": _bounded_int(player.get("profilestate")),
        "persona_state": _bounded_int(player.get("personastate")),
        "account_created_at": _bounded_int(player.get("timecreated")),
        "last_logoff_at": _bounded_int(player.get("lastlogoff")),
        "primary_clan_id": _clean_text(player.get("primaryclanid"), 32),
        "comment_permission": _bounded_int(player.get("commentpermission")),
    }


class SteamPublicAdapter(Adapter):
    tool_id = "steam_public"
    display_name = "Steam Public Profile"
    executable = "steam-public-profile"
    capabilities = frozenset({"username_search", "steam_profile", "profile_image"})
    supported_query_types = frozenset({QueryType.USERNAME})
    default_timeout = 45
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport
        self.cancel_requested = False

    async def healthcheck(self) -> HealthResult:
        with SessionLocal() as db:
            configured = get_steam_api_key(db) is not None
        if not configured:
            return HealthResult(False, "Steam Web API key is not configured", None)
        return HealthResult(True, "ready", "Steamworks Web API")

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
            raise ValueError("Steam Public Profile accepts username investigations only")
        target = query.strip().removeprefix("@").strip()
        if not (STEAM_ID64_RE.fullmatch(target) or VANITY_RE.fullmatch(target)):
            raise ValueError("Steam username must be a vanity name or SteamID64")
        command = [
            self.executable,
            "--username",
            target,
            "--output",
            str(output_dir / "steam-public-profile.json"),
            "--timeout",
            str(timeout),
        ]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        async with client.stream("GET", url, params=params) as response:
            if response.status_code == 429:
                raise RuntimeError("rate_limited")
            if response.status_code in {401, 403}:
                raise PermissionError("Steam rejected the configured Web API key")
            if response.is_redirect:
                raise ValueError("Steam Web API redirected unexpectedly")
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").casefold()
            if "json" not in content_type:
                raise ValueError("Steam Web API returned a non-JSON response")
            raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Steam Web API returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Steam Web API returned an unexpected JSON shape")
        return payload

    async def execute(
        self,
        command: list[str],
        output_dir: Path,
        timeout: int,
        query: str,
        log: LogCallback | None = None,
    ) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = query.strip().removeprefix("@").strip()
        if len(command) < 7 or command[0] != self.executable or command[2] != target:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="Steam command did not match the bounded adapter contract",
            )
        with SessionLocal() as db:
            api_key = get_steam_api_key(db)
        if not api_key:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="not_configured",
                error_message="Steam Web API key is not configured in Settings",
            )

        self.cancel_requested = False
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        try:
            async with asyncio.timeout(timeout):
                request_timeout = min(20.0, max(5.0, float(timeout)))
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
                    steam_id = target
                    if not STEAM_ID64_RE.fullmatch(target):
                        resolved = await self._get_json(
                            client,
                            RESOLVE_ENDPOINT,
                            {"key": api_key, "vanityurl": target, "url_type": "1"},
                        )
                        response = resolved.get("response")
                        if not isinstance(response, dict) or int(response.get("success") or 0) != 1:
                            return ExecutionResult(
                                0,
                                json.dumps({"status": "not_found", "username": target}),
                                "",
                            )
                        steam_id = str(response.get("steamid") or "").strip()
                        if not STEAM_ID64_RE.fullmatch(steam_id):
                            raise ValueError("Steam vanity resolution returned an invalid SteamID64")

                    summary = await self._get_json(
                        client,
                        SUMMARY_ENDPOINT,
                        {"key": api_key, "steamids": steam_id},
                    )
            if self.cancel_requested:
                return ExecutionResult(
                    None,
                    "",
                    "",
                    cancelled=True,
                    error_code="cancelled",
                    error_message="Steam profile collection was cancelled",
                )
            response = summary.get("response")
            players = response.get("players") if isinstance(response, dict) else None
            if not isinstance(players, list) or not players or not isinstance(players[0], dict):
                return ExecutionResult(
                    0,
                    json.dumps({"status": "not_found", "username": target}),
                    "",
                )
            record = _profile_record(players[0], target)
            record.update(
                {
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "source_scope": "steamworks_web_api_public_profile",
                }
            )
            output_path = output_dir / "steam-public-profile.json"
            output_path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
            output_path.chmod(0o600)
            if log:
                await log("stdout", f"Steam public profile resolved for {target}")
            return ExecutionResult(
                0,
                json.dumps({"status": "found", "steam_id": record["steam_id"]}),
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
                error_message=f"Steam public profile request exceeded {timeout}s",
            )
        except RuntimeError as exc:
            if str(exc) == "rate_limited":
                return ExecutionResult(
                    None,
                    "",
                    "",
                    rate_limited=True,
                    error_code="rate_limited",
                    error_message="Steam rate limited the Web API request",
                )
            raise
        except PermissionError as exc:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="api_key_rejected",
                error_message=str(exc),
            )
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
                error_message=f"Steam Web API returned HTTP {exc.response.status_code}",
            )
        except httpx.HTTPError:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="upstream_error",
                error_message="Steam Web API request failed",
            )

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "steam-public-profile.json"
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
            steam_id = str(record.get("steam_id") or "").strip()
            url = str(record.get("profile_url") or "").strip()
            if not steam_id or not url:
                continue
            location_parts = [record.get("city_id"), record.get("state_code"), record.get("country_code")]
            location = ", ".join(str(part) for part in location_parts if part not in (None, "")) or None
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=query,
                    finding_type="steam_profile",
                    platform="Steam",
                    value=str(record.get("username") or steam_id),
                    canonical_url=url,
                    display_url=url,
                    status=FindingStatus.FOUND,
                    confidence=0.98,
                    title=str(record.get("display_name") or steam_id),
                    description=record.get("real_name"),
                    profile=ProfileEvidence(
                        avatar_url=record.get("avatar_url"),
                        display_name=record.get("display_name"),
                        location=location,
                        account_id=steam_id,
                    ),
                    metadata={
                        "steam_id": steam_id,
                        "real_name": record.get("real_name"),
                        "country_code": record.get("country_code"),
                        "state_code": record.get("state_code"),
                        "city_id": record.get("city_id"),
                        "community_visibility_state": record.get("community_visibility_state"),
                        "profile_state": record.get("profile_state"),
                        "persona_state": record.get("persona_state"),
                        "account_created_at": record.get("account_created_at"),
                        "last_logoff_at": record.get("last_logoff_at"),
                        "primary_clan_id": record.get("primary_clan_id"),
                        "comment_permission": record.get("comment_permission"),
                        "avatar_medium": record.get("avatar_medium"),
                        "avatar_thumb": record.get("avatar_thumb"),
                        "source_scope": record.get("source_scope"),
                        "retrieved_at": record.get("retrieved_at"),
                    },
                    source_tool=self.tool_id,
                    source_record_id=steam_id,
                    raw_output_reference=raw_reference,
                )
            )
        return findings
