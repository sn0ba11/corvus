"""Regression tests for bounded public Snapchat hydration."""

import json
from pathlib import Path

import httpx
import pytest

from app.adapters.snapchat_public import SnapchatPublicAdapter
from app.models.schemas import FindingStatus, QueryType


@pytest.mark.asyncio
async def test_snapchat_hydration_requires_exact_username(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://www.snapchat.com/@example_user"
        payload = {"props": {"pageProps": {"userProfile": {"publicProfileInfo": {
            "username": "example_user", "title": "Example", "bio": "Public bio",
            "subscriberCount": 42, "businessProfileId": "public-id",
        }}}}}
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text=f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>')

    adapter = SnapchatPublicAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("example_user", QueryType.USERNAME, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "example_user")
    assert result.returncode == 0
    findings = adapter.normalise_results(adapter.parse_results(result, tmp_path), "example_user", QueryType.USERNAME, "raw")
    assert len(findings) == 1
    assert findings[0].status == FindingStatus.FOUND
    assert findings[0].profile.followers == 42
    assert findings[0].source_tool == "snapchat_public"


@pytest.mark.asyncio
async def test_snapchat_hydration_rejects_unrelated_account(tmp_path: Path) -> None:
    payload = {"props": {"pageProps": {"userProfile": {"publicProfileInfo": {
        "username": "other_user", "title": "Other", "bio": "Not this user",
    }}}}}
    adapter = SnapchatPublicAdapter(httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/html"},
        text=f'<script id="__NEXT_DATA__">{json.dumps(payload)}</script>')))
    result = await adapter.execute(adapter.build_command("example_user", QueryType.USERNAME, tmp_path, 30),
                                   tmp_path, 30, "example_user")
    findings = adapter.normalise_results(adapter.parse_results(result, tmp_path), "example_user", QueryType.USERNAME, "raw")
    assert len(findings) == 1
    assert findings[0].status == FindingStatus.UNKNOWN


@pytest.mark.asyncio
async def test_telegram_generic_landing_page_is_not_a_positive(tmp_path: Path) -> None:
    from app.adapters.telegram_public import TelegramPublicAdapter

    adapter = TelegramPublicAdapter(httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/html"}, text='''
        <meta property="og:title" content="Telegram: Contact @example_user">
        <meta property="og:description" content="If you have Telegram, you can contact @example_user right away">
        <meta property="og:url" content="https://t.me/example_user">''')))
    result = await adapter.execute(adapter.build_command("example_user", QueryType.USERNAME, tmp_path, 30),
                                   tmp_path, 30, "example_user")
    findings = adapter.normalise_results(adapter.parse_results(result, tmp_path), "example_user", QueryType.USERNAME, "raw")
    assert len(findings) == 1
    assert findings[0].status == FindingStatus.UNKNOWN


def _snapchat_page(user_profile: dict) -> httpx.Response:
    payload = {"props": {"pageProps": {"userProfile": user_profile}}}
    return httpx.Response(200, headers={"content-type": "text/html"},
                          text=f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>')


async def _snapchat_findings(tmp_path: Path, user_profile: dict, username: str = "example_user"):
    adapter = SnapchatPublicAdapter(httpx.MockTransport(lambda request: _snapchat_page(user_profile)))
    result = await adapter.execute(adapter.build_command(username, QueryType.USERNAME, tmp_path, 30),
                                   tmp_path, 30, username)
    return adapter.normalise_results(adapter.parse_results(result, tmp_path), username, QueryType.USERNAME, "raw")


@pytest.mark.asyncio
async def test_snapchat_ordinary_account_is_found_from_user_info(tmp_path: Path) -> None:
    findings = await _snapchat_findings(tmp_path, {"$case": "userInfo", "userInfo": {
        "username": "example_user", "displayName": "Example Person",
        "snapcodeImageUrl": "https://gcp.api.snapchat.com/web/deeplink/snapcode?username=example_user",
        "bitmoji3d": {"avatarImage": {"url": "https://cf-st.sc-cdn.net/3d/render/a.webp",
                                      "fallbackUrl": "https://cf-st.sc-cdn.net/aps/bolt/a.png"}},
    }})
    assert len(findings) == 1
    assert findings[0].status == FindingStatus.FOUND
    assert findings[0].confidence == 0.8
    assert findings[0].title == "Example Person"
    assert findings[0].profile.avatar_url == "https://cf-st.sc-cdn.net/aps/bolt/a.png"
    details = {item["label"]: item["value"] for item in findings[0].metadata["platform_details"]}
    assert details["Profile type"].startswith("Ordinary account")
    assert "Bitmoji image" in details


@pytest.mark.asyncio
async def test_snapchat_ordinary_account_for_another_username_is_unknown(tmp_path: Path) -> None:
    findings = await _snapchat_findings(tmp_path, {"userInfo": {"username": "someone_else", "displayName": "Else"}})
    assert findings[0].status == FindingStatus.UNKNOWN


@pytest.mark.asyncio
async def test_snapchat_public_profile_keeps_dates_address_and_cross_links(tmp_path: Path) -> None:
    findings = await _snapchat_findings(tmp_path, {"publicProfileInfo": {
        "username": "example_user", "title": "Example", "bio": "Public bio", "subscriberCount": "1200",
        "address": "1 Example Street", "creationTimestampMs": {"value": "1558546707554"},
        "lastUpdateTimestampMs": {"value": "not-a-number"},
        "sameAsLinks": ["https://www.instagram.com/example_user", "javascript:alert(1)", 7],
        "relatedAccountsInfo": [{"publicProfileInfo": {"username": "example_two", "title": "Second"}},
                                {"publicProfileInfo": {"username": "!!"}}, "junk"],
    }})
    finding = findings[0]
    assert finding.status == FindingStatus.FOUND
    assert finding.profile.followers == 1200
    assert finding.metadata["linked_sites"] == ["https://www.instagram.com/example_user"]
    details = [(item["label"], item["value"]) for item in finding.metadata["platform_details"]]
    assert ("Address", "1 Example Street") in details
    assert ("Profile created", "2019-05-22T17:38:27.554000+00:00") in details
    assert ("Same person elsewhere", "https://www.instagram.com/example_user") in details
    assert ("Related Snapchat account", "Second (@example_two)") in details
    assert not any(label == "Profile last updated" for label, _ in details)


@pytest.mark.asyncio
async def test_snapchat_malformed_hydration_is_unknown_not_an_error(tmp_path: Path) -> None:
    for user_profile in ("text", {"publicProfileInfo": "text", "userInfo": 4}, {"userInfo": {"username": "example_user"}}):
        findings = await _snapchat_findings(tmp_path, user_profile)  # type: ignore[arg-type]
        assert findings[0].status == FindingStatus.UNKNOWN
