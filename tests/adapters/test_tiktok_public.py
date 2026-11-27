import json
from pathlib import Path

import httpx
import pytest

from app.adapters import REGISTRY
from app.adapters.tiktok_public import (
    TIKTOK_HYDRATION_ID,
    TikTokPublicAdapter,
    parse_tiktok_hydration,
)
from app.models.schemas import FindingStatus, QueryType


def hydration_html(username: str = "example") -> str:
    payload = {
        "__DEFAULT_SCOPE__": {
            "webapp.user-detail": {
                "userInfo": {
                    "user": {
                        "id": "7372405988308567046",
                        "uniqueId": username,
                        "nickname": "Example Person",
                        "signature": "Public biography",
                        "avatarLarger": "https://p16-sign.example/avatar-large.jpeg",
                        "avatarMedium": "https://p16-sign.example/avatar-medium.jpeg",
                        "avatarThumb": "https://p16-sign.example/avatar-thumb.jpeg",
                        "verified": True,
                        "privateAccount": False,
                    },
                    "stats": {
                        "followerCount": 1234,
                        "followingCount": 56,
                        "heartCount": 7890,
                        "videoCount": 12,
                    },
                }
            }
        }
    }
    return (
        "<!doctype html><html><body>"
        f'<script id="{TIKTOK_HYDRATION_ID}" type="application/json">'
        f"{json.dumps(payload)}"
        "</script></body></html>"
    )


def test_tiktok_adapter_is_registered() -> None:
    assert REGISTRY["tiktok_public"] is TikTokPublicAdapter


def test_parse_tiktok_hydration_extracts_bounded_public_fields() -> None:
    record = parse_tiktok_hydration(hydration_html(), "example")

    assert record == {
        "username": "example",
        "account_id": "7372405988308567046",
        "display_name": "Example Person",
        "biography": "Public biography",
        "avatar_url": "https://p16-sign.example/avatar-large.jpeg",
        "avatar_medium": "https://p16-sign.example/avatar-medium.jpeg",
        "avatar_thumb": "https://p16-sign.example/avatar-thumb.jpeg",
        "verified": True,
        "private_account": False,
        "followers": 1234,
        "following": 56,
        "likes": 7890,
        "video_count": 12,
    }


def test_parse_tiktok_hydration_rejects_identifier_mismatch() -> None:
    with pytest.raises(ValueError, match="different profile identifier"):
        parse_tiktok_hydration(hydration_html("other"), "example")


@pytest.mark.asyncio
async def test_tiktok_public_collects_and_normalises_profile(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == "https://www.tiktok.com/@example"
        assert request.headers["accept-language"] == "en-IE,en;q=0.9"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=hydration_html(),
        )

    adapter = TikTokPublicAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("example", QueryType.USERNAME, tmp_path, 45)
    result = await adapter.execute(command, tmp_path, 45, "example")
    records = adapter.parse_results(result, tmp_path)
    findings = adapter.normalise_results(records, "example", QueryType.USERNAME, "raw")

    assert result.returncode == 0
    assert result.error_code is None
    assert len(records) == 1
    assert len(findings) == 1
    finding = findings[0]
    assert finding.platform == "TikTok"
    assert finding.finding_type == "tiktok_profile"
    assert finding.status == FindingStatus.FOUND
    assert finding.confidence == 0.95
    assert finding.canonical_url == "https://www.tiktok.com/@example"
    assert finding.profile.account_id == "7372405988308567046"
    assert finding.profile.display_name == "Example Person"
    assert finding.profile.avatar_url == "https://p16-sign.example/avatar-large.jpeg"
    assert finding.profile.followers == 1234
    assert finding.metadata["likes"] == 7890
    assert finding.metadata["video_count"] == 12
    assert (tmp_path / "tiktok-public-profile.json").is_file()


@pytest.mark.asyncio
async def test_tiktok_public_missing_hydration_is_unknown_not_failed(tmp_path: Path) -> None:
    adapter = TikTokPublicAdapter(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text="<html><body>public page without profile hydration</body></html>",
            )
        )
    )
    command = adapter.build_command("example", QueryType.USERNAME, tmp_path, 45)

    result = await adapter.execute(command, tmp_path, 45, "example")

    assert result.returncode == 0
    assert result.error_code is None
    assert '"status": "unknown"' in result.stdout
    assert "hydration JSON was not present" in result.stderr
    assert adapter.parse_results(result, tmp_path) == []


@pytest.mark.asyncio
async def test_tiktok_public_rejects_redirect_challenge(tmp_path: Path) -> None:
    adapter = TikTokPublicAdapter(
        httpx.MockTransport(
            lambda _: httpx.Response(
                302,
                headers={"location": "https://www.tiktok.com/login"},
            )
        )
    )
    command = adapter.build_command("example", QueryType.USERNAME, tmp_path, 45)

    result = await adapter.execute(command, tmp_path, 45, "example")

    assert result.returncode is None
    assert result.error_code == "blocked_or_redirected"
    assert adapter.parse_results(result, tmp_path) == []


@pytest.mark.asyncio
async def test_tiktok_public_rejects_oversized_page(tmp_path: Path) -> None:
    adapter = TikTokPublicAdapter(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"x" * (1024 * 1024 + 1),
            )
        )
    )
    command = adapter.build_command("example", QueryType.USERNAME, tmp_path, 45)

    result = await adapter.execute(command, tmp_path, 45, "example")

    assert result.returncode is None
    assert result.error_code == "response_too_large"
