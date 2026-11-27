from pathlib import Path

import httpx
import pytest

from app.adapters.phone_presence import PhonePresenceAdapter
from app.db.session import SessionLocal, engine
from app.models.db import Base
from app.models.schemas import FindingStatus, QueryType
from app.services.phone_presence_settings import (
    reset_active_phone_presence_platforms,
    save_active_phone_presence_platforms,
)
from app.services.runtime_settings import reset_runtime_options, save_runtime_options

IRISH_NUMBER = "+353871234567"


def _enable_platforms(platforms: list[str]) -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        save_active_phone_presence_platforms(db, platforms)


def _reset_platforms() -> None:
    with SessionLocal.begin() as db:
        reset_active_phone_presence_platforms(db)


def _set_tor_runtime_option(enabled: bool) -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        save_runtime_options(db, {"phone_presence_via_tor": enabled})


def _reset_runtime_options() -> None:
    with SessionLocal.begin() as db:
        reset_runtime_options(db)


@pytest.mark.asyncio
async def test_snapchat_taken_number_is_a_likely_finding(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.host == "accounts.snapchat.com"
            return httpx.Response(200, headers={"Set-Cookie": "xsrf_token=mock-xsrf-value; Path=/"})
        assert request.url.path == "/accounts/validate_phone_number"
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        assert form["phone_country_code"] == "IE"
        assert form["phone_number"] == "871234567"
        assert form["xsrf_token"] == "mock-xsrf-value"  # noqa: S105 - mock CSRF value, not a secret
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"status_code": "TAKEN_NUMBER"},
        )

    adapter = PhonePresenceAdapter(httpx.MockTransport(handler))
    command = adapter.build_command(IRISH_NUMBER, QueryType.PHONE, tmp_path, 20)
    result = await adapter.execute(command, tmp_path, 20, IRISH_NUMBER)
    assert result.returncode == 0
    records = adapter.parse_results(result, tmp_path)
    findings = adapter.normalise_results(records, IRISH_NUMBER, QueryType.PHONE, "ref")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.status == FindingStatus.LIKELY
    assert finding.platform == "Snapchat"
    assert finding.confidence == 0.55


@pytest.mark.asyncio
async def test_snapchat_available_number_is_not_a_finding(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, headers={"Set-Cookie": "xsrf_token=mock-xsrf-value; Path=/"})
        return httpx.Response(200, json={"status_code": "OK"})

    adapter = PhonePresenceAdapter(httpx.MockTransport(handler))
    command = adapter.build_command(IRISH_NUMBER, QueryType.PHONE, tmp_path, 20)
    result = await adapter.execute(command, tmp_path, 20, IRISH_NUMBER)
    assert result.returncode == 0
    records = adapter.parse_results(result, tmp_path)
    assert records[0]["exists"] is False
    assert adapter.normalise_results(records, IRISH_NUMBER, QueryType.PHONE, "ref") == []


@pytest.mark.asyncio
async def test_snapchat_rate_limit_is_structured(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, headers={"Set-Cookie": "xsrf_token=mock-xsrf-value; Path=/"})
        return httpx.Response(429)

    adapter = PhonePresenceAdapter(httpx.MockTransport(handler))
    command = adapter.build_command(IRISH_NUMBER, QueryType.PHONE, tmp_path, 20)
    result = await adapter.execute(command, tmp_path, 20, IRISH_NUMBER)
    assert result.rate_limited is True
    assert result.error_code == "rate_limited"


@pytest.mark.asyncio
async def test_missing_xsrf_token_is_a_bounded_failure(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    adapter = PhonePresenceAdapter(httpx.MockTransport(handler))
    command = adapter.build_command(IRISH_NUMBER, QueryType.PHONE, tmp_path, 20)
    result = await adapter.execute(command, tmp_path, 20, IRISH_NUMBER)
    assert result.returncode is None
    assert result.error_code == "upstream_error"


def test_invalid_phone_number_is_rejected_before_any_request(tmp_path: Path) -> None:
    adapter = PhonePresenceAdapter()
    with pytest.raises(ValueError):
        adapter.build_command("not-a-number", QueryType.PHONE, tmp_path, 20)


@pytest.mark.asyncio
async def test_unsupported_calling_code_skips_without_a_request(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made for an unmapped calling code")

    adapter = PhonePresenceAdapter(httpx.MockTransport(handler))
    number = "+6721234567"  # Australian external territories: deliberately unmapped
    command = adapter.build_command(number, QueryType.PHONE, tmp_path, 20)
    result = await adapter.execute(command, tmp_path, 20, number)
    assert result.returncode == 0
    records = adapter.parse_results(result, tmp_path)
    assert records[0]["skipped"] is True
    assert adapter.normalise_results(records, number, QueryType.PHONE, "ref") == []


@pytest.mark.asyncio
async def test_amazon_recognised_identifier_is_a_likely_finding(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.host == "www.amazon.com"
            return httpx.Response(
                200,
                content=(
                    b"<form><input type='hidden' name='appActionToken' value='tok'>"
                    b"<input type='hidden' name='workflowState' value='state'></form>"
                ),
            )
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        assert "appActionToken" in form
        return httpx.Response(200, content=b"<div id='auth-password-missing-alert'></div>")

    adapter = PhonePresenceAdapter(httpx.MockTransport(handler))
    _enable_platforms(["amazon"])
    try:
        command = adapter.build_command(IRISH_NUMBER, QueryType.PHONE, tmp_path, 20)
        result = await adapter.execute(command, tmp_path, 20, IRISH_NUMBER)
        records = adapter.parse_results(result, tmp_path)
        findings = adapter.normalise_results(records, IRISH_NUMBER, QueryType.PHONE, "ref")
        assert len(findings) == 1
        assert findings[0].platform == "Amazon"
    finally:
        _reset_platforms()


@pytest.mark.asyncio
async def test_amazon_unrecognised_identifier_is_not_a_finding(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=b"<form><input name='x' value='y'></form>")
        return httpx.Response(200, content=b"<html>sign in</html>")

    adapter = PhonePresenceAdapter(httpx.MockTransport(handler))
    _enable_platforms(["amazon"])
    try:
        command = adapter.build_command(IRISH_NUMBER, QueryType.PHONE, tmp_path, 20)
        result = await adapter.execute(command, tmp_path, 20, IRISH_NUMBER)
        records = adapter.parse_results(result, tmp_path)
        assert records[0]["exists"] is False
        assert adapter.normalise_results(records, IRISH_NUMBER, QueryType.PHONE, "ref") == []
    finally:
        _reset_platforms()


@pytest.mark.asyncio
async def test_all_configured_platforms_run_when_enabled(tmp_path: Path) -> None:
    hosts_seen: set[str] = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        hosts_seen.add(request.url.host)
        if request.url.host == "accounts.snapchat.com" and request.method == "GET":
            return httpx.Response(200, headers={"Set-Cookie": "xsrf_token=mock-xsrf-value; Path=/"})
        if request.url.host == "accounts.snapchat.com":
            return httpx.Response(200, json={"status_code": "OK"})
        if request.url.host == "www.amazon.com" and request.method == "GET":
            return httpx.Response(200, content=b"<form></form>")
        return httpx.Response(200, content=b"<html></html>")

    adapter = PhonePresenceAdapter(httpx.MockTransport(handler))
    _enable_platforms(["snapchat", "amazon"])
    try:
        command = adapter.build_command(IRISH_NUMBER, QueryType.PHONE, tmp_path, 30)
        result = await adapter.execute(command, tmp_path, 30, IRISH_NUMBER)
        assert result.returncode == 0
        records = adapter.parse_results(result, tmp_path)
        assert {record["name"] for record in records} == {"snapchat", "amazon"}
        assert hosts_seen == {"accounts.snapchat.com", "www.amazon.com"}
    finally:
        _reset_platforms()


@pytest.mark.asyncio
async def test_default_settings_only_contact_snapchat(tmp_path: Path) -> None:
    hosts_seen: set[str] = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        hosts_seen.add(request.url.host)
        if request.method == "GET":
            return httpx.Response(200, headers={"Set-Cookie": "xsrf_token=mock-xsrf-value; Path=/"})
        return httpx.Response(200, json={"status_code": "OK"})

    adapter = PhonePresenceAdapter(httpx.MockTransport(handler))
    command = adapter.build_command(IRISH_NUMBER, QueryType.PHONE, tmp_path, 20)
    result = await adapter.execute(command, tmp_path, 20, IRISH_NUMBER)
    assert result.returncode == 0
    assert hosts_seen == {"accounts.snapchat.com"}


async def _run_snapchat_only(adapter: PhonePresenceAdapter, tmp_path: Path) -> None:
    command = adapter.build_command(IRISH_NUMBER, QueryType.PHONE, tmp_path, 20)
    await adapter.execute(command, tmp_path, 20, IRISH_NUMBER)


@pytest.mark.asyncio
async def test_tor_is_not_used_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, headers={"Set-Cookie": "xsrf_token=mock-xsrf-value; Path=/"})
        return httpx.Response(200, json={"status_code": "OK"})

    monkeypatch.setattr(
        "app.adapters.phone_presence.get_settings",
        lambda: type("_S", (), {"tor_proxy": "socks5h://tor:9050"})(),
    )
    adapter = PhonePresenceAdapter(httpx.MockTransport(handler))
    await _run_snapchat_only(adapter, tmp_path)
    assert adapter.last_effective_proxy is None


@pytest.mark.asyncio
async def test_tor_is_used_when_enabled_and_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, headers={"Set-Cookie": "xsrf_token=mock-xsrf-value; Path=/"})
        return httpx.Response(200, json={"status_code": "OK"})

    monkeypatch.setattr(
        "app.adapters.phone_presence.get_settings",
        lambda: type("_S", (), {"tor_proxy": "socks5h://tor:9050"})(),
    )
    adapter = PhonePresenceAdapter(httpx.MockTransport(handler))
    _set_tor_runtime_option(True)
    try:
        await _run_snapchat_only(adapter, tmp_path)
        assert adapter.last_effective_proxy == "socks5h://tor:9050"
    finally:
        _reset_runtime_options()


@pytest.mark.asyncio
async def test_tor_enabled_but_unconfigured_falls_back_to_direct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, headers={"Set-Cookie": "xsrf_token=mock-xsrf-value; Path=/"})
        return httpx.Response(200, json={"status_code": "OK"})

    monkeypatch.setattr(
        "app.adapters.phone_presence.get_settings",
        lambda: type("_S", (), {"tor_proxy": None})(),
    )
    adapter = PhonePresenceAdapter(httpx.MockTransport(handler))
    _set_tor_runtime_option(True)
    try:
        await _run_snapchat_only(adapter, tmp_path)
        assert adapter.last_effective_proxy is None
    finally:
        _reset_runtime_options()


@pytest.mark.asyncio
async def test_explicit_case_proxy_wins_over_tor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, headers={"Set-Cookie": "xsrf_token=mock-xsrf-value; Path=/"})
        return httpx.Response(200, json={"status_code": "OK"})

    monkeypatch.setattr(
        "app.adapters.phone_presence.get_settings",
        lambda: type("_S", (), {"tor_proxy": "socks5h://tor:9050"})(),
    )
    adapter = PhonePresenceAdapter(httpx.MockTransport(handler))
    _set_tor_runtime_option(True)
    try:
        command = adapter.build_command(IRISH_NUMBER, QueryType.PHONE, tmp_path, 20, "http://case-proxy:8080")
        await adapter.execute(command, tmp_path, 20, IRISH_NUMBER)
        assert adapter.last_effective_proxy == "http://case-proxy:8080"
    finally:
        _reset_runtime_options()
