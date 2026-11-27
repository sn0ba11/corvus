import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.adapters import steam_public
from app.adapters.steam_public import SteamPublicAdapter
from app.models.schemas import FindingStatus, QueryType

API_KEY = "A" * 32
STEAM_ID = "76561198000000000"


@pytest.mark.asyncio
async def test_resolves_vanity_and_collects_public_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(steam_public, "get_steam_api_key", lambda _db: API_KEY)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        params = parse_qs(request.url.query.decode())
        assert params["key"] == [API_KEY]
        if "ResolveVanityURL" in request.url.path:
            assert params["vanityurl"] == ["example_user"]
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"response": {"success": 1, "steamid": STEAM_ID}},
            )
        assert params["steamids"] == [STEAM_ID]
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "response": {
                    "players": [
                        {
                            "steamid": STEAM_ID,
                            "communityvisibilitystate": 3,
                            "profilestate": 1,
                            "personaname": "Example Player",
                            "realname": "Example Person",
                            "profileurl": "https://steamcommunity.com/id/example_user/",
                            "avatarfull": "https://cdn.example/avatar.jpg",
                            "loccountrycode": "IE",
                            "timecreated": 1_400_000_000,
                            "lastlogoff": 1_700_000_000,
                        }
                    ]
                }
            },
        )

    adapter = SteamPublicAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("example_user", QueryType.USERNAME, tmp_path, 45)
    result = await adapter.execute(command, tmp_path, 45, "example_user")
    records = adapter.parse_results(result, tmp_path)
    findings = adapter.normalise_results(records, "example_user", QueryType.USERNAME, "raw")

    assert result.returncode == 0
    assert len(requests) == 2
    assert len(findings) == 1
    finding = findings[0]
    assert finding.status == FindingStatus.FOUND
    assert finding.platform == "Steam"
    assert finding.profile.account_id == STEAM_ID
    assert finding.profile.display_name == "Example Player"
    assert finding.profile.location == "IE"
    assert finding.metadata["real_name"] == "Example Person"
    assert API_KEY not in (tmp_path / "steam-public-profile.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_direct_steamid_skips_vanity_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(steam_public, "get_steam_api_key", lambda _db: API_KEY)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"response": {"players": [{"steamid": STEAM_ID, "personaname": "Player"}]}},
        )

    adapter = SteamPublicAdapter(httpx.MockTransport(handler))
    command = adapter.build_command(STEAM_ID, QueryType.USERNAME, tmp_path, 45)
    result = await adapter.execute(command, tmp_path, 45, STEAM_ID)

    assert result.returncode == 0
    assert len(paths) == 1
    assert "GetPlayerSummaries" in paths[0]


@pytest.mark.asyncio
async def test_missing_key_reports_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(steam_public, "get_steam_api_key", lambda _db: None)
    adapter = SteamPublicAdapter()
    command = adapter.build_command("example_user", QueryType.USERNAME, tmp_path, 45)

    result = await adapter.execute(command, tmp_path, 45, "example_user")

    assert result.error_code == "not_configured"
    assert "Settings" in (result.error_message or "")


@pytest.mark.asyncio
async def test_unknown_vanity_is_clean_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(steam_public, "get_steam_api_key", lambda _db: API_KEY)
    adapter = SteamPublicAdapter(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"response": {"success": 42, "message": "No match"}},
            )
        )
    )
    command = adapter.build_command("missing_user", QueryType.USERNAME, tmp_path, 45)

    result = await adapter.execute(command, tmp_path, 45, "missing_user")

    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "not_found"
    assert adapter.parse_results(result, tmp_path) == []


def test_rejects_unsafe_username(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="vanity name"):
        SteamPublicAdapter().build_command("bad user", QueryType.USERNAME, tmp_path, 45)
