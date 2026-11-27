from pathlib import Path

import httpx
import pytest

from app.adapters.telegram_public import TelegramPublicAdapter
from app.models.schemas import FindingStatus, QueryType


@pytest.mark.asyncio
async def test_telegram_public_page_is_manual_and_normalises_profile(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("https://t.me/s/example_user"):
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text="""
            <div class="tgme_channel_info_counter"><span class="counter_value">12</span> <span class="counter_type">photos</span></div>
            <div class="tgme_widget_message" data-post="other_channel/9"><div class="tgme_widget_message_text">Forwarded</div></div>
            <div class="tgme_widget_message" data-post="example_user/41">
              <div class="tgme_widget_message_text js-message_text" dir="auto">First &amp; oldest <a href="https://t.me/example_user/40">1</a></div>
              <time datetime="2026-11-01T10:00:00+00:00" class="time">10:00</time></div>
            <div class="tgme_widget_message" data-post="example_user/42">
              <div class="tgme_widget_message_text js-message_text" dir="auto">Read <b>more</b> at <a href="https://example.org/about?a=1&amp;b=2">site</a></div>
              <time datetime="2026-11-12T08:30:00+00:00" class="time">08:30</time></div>
            """)
        assert request.url == httpx.URL("https://t.me/example_user")
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text="""
            <html><head>
            <meta property="og:title" content="Example User">
            <meta property="og:description" content="Public Telegram profile">
            <meta property="og:image" content="https://cdn4.telesco.pe/file/avatar.jpg">
            <meta property="og:url" content="https://t.me/example_user">
            </head><body><div class="tgme_page_extra">1 234 subscribers</div></body></html>
            """,
        )

    adapter = TelegramPublicAdapter(httpx.MockTransport(handler))
    assert adapter.default_selected is True
    command = adapter.build_command("example_user", QueryType.USERNAME, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "example_user")
    assert result.returncode == 0
    findings = adapter.normalise_results(
        adapter.parse_results(result, tmp_path), "example_user", QueryType.USERNAME, "raw"
    )
    assert len(findings) == 1
    assert findings[0].status == FindingStatus.LIKELY
    assert findings[0].canonical_url == "https://t.me/example_user"
    assert findings[0].profile.account_id == "example_user"
    assert findings[0].metadata["page_extra"] == "1 234 subscribers"
    posts = findings[0].metadata["recent_posts"]
    assert [post["post_id"] for post in posts] == [41, 42]
    assert posts[0]["text"] == "First & oldest 1"
    assert posts[1]["posted_at"] == "2026-11-12T08:30:00+00:00"
    assert findings[0].metadata["linked_sites"] == ["https://example.org/about?a=1&b=2"]
    details = {item["label"]: item["value"] for item in findings[0].metadata["platform_details"]}
    assert details["Latest public post"] == "2026-11-12T08:30:00+00:00"
    assert details["Photos"] == "12"
    assert details["Link in a recent post"] == "https://example.org/about?a=1&b=2"


@pytest.mark.asyncio
async def test_telegram_public_skips_handles_telegram_cannot_hold(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be made for an unsupported handle")

    adapter = TelegramPublicAdapter(httpx.MockTransport(handler))
    with pytest.raises(ValueError):
        adapter.build_command("bad name/../x", QueryType.USERNAME, tmp_path, 30)
    for handle in ("abc", "john.doe"):
        result = await adapter.execute(
            adapter.build_command(handle, QueryType.USERNAME, tmp_path, 30), tmp_path, 30, handle
        )
        assert result.returncode == 0 and result.error_code is None
        assert adapter.normalise_results(
            adapter.parse_results(result, tmp_path), handle, QueryType.USERNAME, "raw"
        ) == []


@pytest.mark.asyncio
async def test_telegram_channel_preview_failure_keeps_the_profile(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/s/"):
            return httpx.Response(302, headers={"Location": "https://t.me/example_user"})
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text="""
            <meta property="og:title" content="Example Channel">
            <meta property="og:description" content="Public channel">
            <meta property="og:url" content="https://t.me/example_user">
            <div class="tgme_page_extra">50 subscribers</div>""")

    adapter = TelegramPublicAdapter(httpx.MockTransport(handler))
    result = await adapter.execute(
        adapter.build_command("example_user", QueryType.USERNAME, tmp_path, 30), tmp_path, 30, "example_user"
    )
    findings = adapter.normalise_results(
        adapter.parse_results(result, tmp_path), "example_user", QueryType.USERNAME, "raw"
    )
    assert findings[0].status == FindingStatus.LIKELY
    assert findings[0].metadata["preview_state"] == "unavailable (HTTP 302)"
    assert "recent_posts" not in findings[0].metadata
