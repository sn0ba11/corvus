from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.services.browser_agent import (
    BrowserAgentClient,
    BrowserAgentError,
    resolve_evidence_path,
    validate_browser_target,
)


def public_resolver(*args, **kwargs):  # type: ignore[no-untyped-def]
    return [(2, 1, 6, "", ("93.184.216.34", 443))]


def private_resolver(*args, **kwargs):  # type: ignore[no-untyped-def]
    return [(2, 1, 6, "", ("127.0.0.1", 443))]


def test_browser_target_requires_allowlisted_public_https() -> None:
    allowed = ("google.com", "tripadvisor.com")
    assert (
        validate_browser_target(
            "https://maps.google.com/example", allowed, resolver=public_resolver
        )
        == "https://maps.google.com/example"
    )
    for target in (
        "http://google.com/example",
        "https://user:secret@google.com/example",
        "https://example.org/example",
    ):
        with pytest.raises(BrowserAgentError):
            validate_browser_target(target, allowed, resolver=public_resolver)
    with pytest.raises(BrowserAgentError, match="non-public"):
        validate_browser_target("https://google.com/example", allowed, resolver=private_resolver)


def test_evidence_reference_cannot_escape_root(tmp_path: Path) -> None:
    assert (
        resolve_evidence_path("case/job/page.png", tmp_path)
        == (tmp_path / "case/job/page.png").resolve()
    )
    for reference in ("../page.png", ""):
        with pytest.raises(BrowserAgentError):
            resolve_evidence_path(reference, tmp_path)
    with pytest.raises(BrowserAgentError):
        resolve_evidence_path(str((tmp_path / "absolute.png").resolve()), tmp_path)


@pytest.mark.asyncio
async def test_disabled_browser_status_and_capture() -> None:
    settings = Settings(_env_file=None, browser_enabled=False)  # type: ignore[call-arg]
    client = BrowserAgentClient(settings)
    status = await client.status()
    assert not status.enabled
    assert not status.available
    assert status.pending_job_id is None
    with pytest.raises(BrowserAgentError, match="disabled"):
        await client.capture(
            job_id="0" * 36,
            investigation_id="1" * 36,
            url="https://google.com/",
        )


@pytest.mark.asyncio
async def test_browser_client_status_capture_and_resume() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "profile_ready": True,
                    "active_job": True,
                    "pending_job_id": "a" * 36,
                    "browser_version": "123.0",
                    "allowed_hosts": ["google.com"],
                    "authenticated_sessions": ["instagram"],
                    "browser_engine": "firefox",
                    "stealth_enabled": True,
                },
            )
        if request.url.path == "/v1/captures/abandon":
            assert request.method == "POST"
            return httpx.Response(
                200,
                json={
                    "status": "abandoned",
                    "job_id": "a" * 36,
                    "investigation_id": "b" * 36,
                },
            )
        if request.url.path == "/v1/sessions/export":
            assert request.headers["authorization"].startswith("Bearer token-")
            return httpx.Response(
                200,
                json={
                    "collector_id": "instaloader",
                    "exported_at": "2026-07-27T08:00:00+00:00",
                    "scope_hosts": ["instagram.com"],
                    "authenticated": True,
                    "user_agent": "Mozilla/5.0 Synthetic Chrome/123.0",
                    "accept_language": "en-IE,en;q=0.9",
                    "cookies": [
                        {
                            "name": "sessionid",
                            "value": "synthetic-session",
                            "domain": ".instagram.com",
                            "path": "/",
                            "expires": 1_900_000_000,
                            "secure": True,
                            "http_only": True,
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "requested_url": "https://google.com/",
                "final_url": "https://www.google.com/",
                "observed_at": "2026-07-27T08:00:00+00:00",
                "page_title": "Google",
                "challenge_type": None,
                "screenshot_reference": f"{'b' * 36}/{'a' * 36}/page.png",
                "html_reference": f"{'b' * 36}/{'a' * 36}/page.html",
                "manifest_reference": f"{'b' * 36}/{'a' * 36}/manifest.json",
                "screenshot_sha256": "1" * 64,
                "html_sha256": "2" * 64,
                "manifest_sha256": "3" * 64,
                "browser_version": "123.0",
                "browser_engine": "firefox",
                "extraction_kind": "social_profile",
                "extraction_platform": "tiktok",
                "authenticated_session": True,
                "extracted": {"platform": "tiktok", "title": "Example"},
            },
        )

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        browser_enabled=True,
        browser_agent_token="token-" + "x" * 40,
        browser_allowed_hosts="google.com",
    )
    client = BrowserAgentClient(settings, transport=httpx.MockTransport(handler))
    status = await client.status()
    assert status.available
    assert status.pending_job_id == "a" * 36
    assert status.authenticated_sessions == ("instagram",)
    assert status.browser_engine == "firefox"
    assert status.stealth_enabled is True
    session = await client.export_session("instaloader")
    assert session.scope_hosts == ("instagram.com",)
    assert session.authenticated is True
    assert session.user_agent == "Mozilla/5.0 Synthetic Chrome/123.0"
    assert session.accept_language == "en-IE,en;q=0.9"
    assert session.cookies[0].runner_payload()["domain"] == ".instagram.com"
    with pytest.raises(BrowserAgentError, match="does not accept"):
        await client.export_session("sherlock")
    result = await client.capture(
        job_id="a" * 36,
        investigation_id="b" * 36,
        url="https://google.com/",
    )
    assert result.manifest_sha256 == "3" * 64
    assert result.browser_engine == "firefox"
    assert result.extraction_kind == "social_profile"
    assert result.extraction_platform == "tiktok"
    assert result.authenticated_session is True
    assert result.extracted == {"platform": "tiktok", "title": "Example"}
    resumed = await client.resume(job_id="a" * 36, investigation_id="b" * 36)
    assert resumed.status == "completed"
    await client.abandon(job_id="a" * 36, investigation_id="b" * 36)


@pytest.mark.asyncio
async def test_browser_client_profile_switch_backup_and_restore() -> None:
    backup_content = b"CORVUS_BROWSER_BACKUP_V1\nsynthetic-encrypted-content"
    digest = "a" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Bearer token-")
        if request.url.path == "/healthz":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "profile_ready": True,
                    "active_job": False,
                    "pending_job_id": None,
                    "browser_version": "123.0",
                    "allowed_hosts": ["google.com"],
                    "authenticated_sessions": [],
                    "active_profile": "sock-default",
                    "profiles": ["sock-default", "case-a"],
                },
            )
        if request.url.path == "/v1/engine/switch":
            assert request.method == "POST"
            assert request.read() == b'{"engine":"firefox"}'
            return httpx.Response(
                200,
                json={"status": "active", "browser_engine": "firefox", "active_profile": "sock-default"},
            )
        if request.url.path == "/v1/profiles/switch":
            assert request.method == "POST"
            return httpx.Response(
                200,
                json={"status": "active", "active_profile": "case-a", "profiles": ["sock-default", "case-a"]},
            )
        if request.url.path == "/v1/profiles/case-a/backup":
            assert request.headers["x-corvus-backup-password"] == "correct horse battery staple"
            return httpx.Response(200, content=backup_content, headers={"X-Content-SHA256": digest})
        if request.url.path == "/v1/profiles/case-a/restore":
            assert request.method == "PUT"
            assert request.content == backup_content
            return httpx.Response(
                200,
                json={"status": "restored", "profile": "case-a", "active_profile": "case-a"},
            )
        raise AssertionError(f"unexpected browser-agent request {request.method} {request.url.path}")

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        browser_enabled=True,
        browser_agent_token="token-" + "x" * 40,
        browser_allowed_hosts="google.com",
    )
    client = BrowserAgentClient(settings, transport=httpx.MockTransport(handler))
    status = await client.status()
    assert status.active_profile == "sock-default"
    assert status.profiles == ("sock-default", "case-a")
    assert await client.switch_engine("firefox") == "firefox"
    assert await client.switch_profile("case-a") == "case-a"
    content, returned_digest = await client.backup_profile(
        "case-a", "correct horse battery staple"
    )
    assert content == backup_content
    assert returned_digest == digest
    assert (
        await client.restore_profile("case-a", "correct horse battery staple", backup_content)
        == "case-a"
    )


@pytest.mark.asyncio
async def test_browser_client_deletes_case_scoped_evidence() -> None:
    investigation_id = "b0a2da45-4345-4aa8-b707-352efdf34431"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == f"/v1/evidence/{investigation_id}"
        assert request.headers["authorization"].startswith("Bearer token-")
        return httpx.Response(
            200,
            json={
                "status": "deleted",
                "investigation_id": investigation_id,
                "removed_files": 3,
            },
        )

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        browser_enabled=True,
        browser_agent_token="token-" + "x" * 40,
    )
    client = BrowserAgentClient(settings, transport=httpx.MockTransport(handler))
    assert await client.delete_investigation_evidence(investigation_id) == 3


@pytest.mark.asyncio
async def test_browser_client_rejects_mismatched_session_bundle() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "collector_id": "maigret",
                "exported_at": "2026-07-27T08:00:00+00:00",
                "scope_hosts": ["instagram.com"],
                "cookies": [],
            },
        )

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        browser_enabled=True,
        browser_agent_token="token-" + "x" * 40,
    )
    client = BrowserAgentClient(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(BrowserAgentError, match="invalid session bundle"):
        await client.export_session("instaloader")
