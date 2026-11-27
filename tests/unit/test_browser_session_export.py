import importlib
import io
import sys
import tarfile
import types
from pathlib import Path

import pytest

playwright_module = types.ModuleType("playwright")
playwright_async_api = types.ModuleType("playwright.async_api")
for imported_name in ("BrowserContext", "Page", "Playwright", "Request", "Route"):
    setattr(playwright_async_api, imported_name, object)
playwright_async_api.Error = Exception  # type: ignore[attr-defined]
playwright_async_api.async_playwright = None  # type: ignore[attr-defined]
sys.modules.setdefault("playwright", playwright_module)
sys.modules.setdefault("playwright.async_api", playwright_async_api)

browser_agent_main = importlib.import_module("browser_agent.main")
filter_session_cookies = browser_agent_main.filter_session_cookies


def test_profile_lock_clears_stale_chromium_singletons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser_agent_main, "PROFILE_DIR", tmp_path)
    for filename in browser_agent_main.CHROMIUM_SINGLETON_FILES:
        (tmp_path / filename).symlink_to("stale-container")
    retained = tmp_path / "Cookies"
    retained.write_text("keep", encoding="utf-8")

    lock_fd = browser_agent_main.acquire_profile_lock()
    try:
        assert all(
            not (tmp_path / filename).is_symlink()
            for filename in browser_agent_main.CHROMIUM_SINGLETON_FILES
        )
        assert retained.read_text(encoding="utf-8") == "keep"
    finally:
        browser_agent_main.release_profile_lock(lock_fd)


def test_profile_lock_rejects_concurrent_browser_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser_agent_main, "PROFILE_DIR", tmp_path)
    lock_fd = browser_agent_main.acquire_profile_lock()
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            browser_agent_main.acquire_profile_lock()
    finally:
        browser_agent_main.release_profile_lock(lock_fd)


def test_encrypted_browser_profile_backup_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser_agent_main, "PROFILE_DIR", tmp_path)
    identities = tmp_path / ".corvus-identities"
    monkeypatch.setattr(browser_agent_main, "IDENTITIES_DIR", identities)
    profile = identities / "case-a"
    profile.mkdir(parents=True)
    (profile / "Cookies").write_text("synthetic-cookie-store", encoding="utf-8")
    (profile / "Preferences").write_text("synthetic-preferences", encoding="utf-8")

    encrypted = browser_agent_main.encrypt_profile_backup(
        "case-a", "correct horse battery staple"
    )
    assert encrypted.startswith(browser_agent_main.BACKUP_MAGIC)
    assert b"synthetic-cookie-store" not in encrypted
    payload = browser_agent_main.decrypt_profile_backup(
        "case-a", "correct horse battery staple", encrypted
    )
    with pytest.raises(ValueError, match="authentication failed"):
        browser_agent_main.decrypt_profile_backup("case-a", "wrong password here", encrypted)

    browser_agent_main.restore_profile_archive("case-a", payload)
    assert (profile / "Cookies").read_text(encoding="utf-8") == "synthetic-cookie-store"
    assert (profile / "Preferences").read_text(encoding="utf-8") == "synthetic-preferences"


def test_default_browser_profile_restore_preserves_named_identity_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser_agent_main, "PROFILE_DIR", tmp_path)
    identities = tmp_path / ".corvus-identities"
    monkeypatch.setattr(browser_agent_main, "IDENTITIES_DIR", identities)
    (tmp_path / "Cookies").write_text("old-default", encoding="utf-8")
    named = identities / "case-a"
    named.mkdir(parents=True)
    (named / "Cookies").write_text("named-identity", encoding="utf-8")

    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode="w:gz") as archive:
        member = tarfile.TarInfo("Cookies")
        content = b"new-default"
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    browser_agent_main.restore_profile_archive("sock-default", source.getvalue())

    assert (tmp_path / "Cookies").read_text(encoding="utf-8") == "new-default"
    assert (named / "Cookies").read_text(encoding="utf-8") == "named-identity"


def test_browser_profile_restore_rejects_links_and_path_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser_agent_main, "PROFILE_DIR", tmp_path)
    monkeypatch.setattr(browser_agent_main, "IDENTITIES_DIR", tmp_path / ".corvus-identities")

    linked = io.BytesIO()
    with tarfile.open(fileobj=linked, mode="w:gz") as archive:
        member = tarfile.TarInfo("Cookies")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
        archive.addfile(member)
    with pytest.raises(ValueError, match="unsupported links"):
        browser_agent_main.restore_profile_archive("case-a", linked.getvalue())

    escaped = io.BytesIO()
    with tarfile.open(fileobj=escaped, mode="w:gz") as archive:
        member = tarfile.TarInfo("../outside")
        content = b"blocked"
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    with pytest.raises(ValueError, match="unsafe path"):
        browser_agent_main.restore_profile_archive("case-a", escaped.getvalue())
    assert not (tmp_path / "outside").exists()


def test_named_browser_profile_listing_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser_agent_main, "PROFILE_DIR", tmp_path)
    identities = tmp_path / ".corvus-identities"
    monkeypatch.setattr(browser_agent_main, "IDENTITIES_DIR", identities)
    (identities / "case-a").mkdir(parents=True)
    (identities / "bad profile").mkdir()
    assert browser_agent_main.list_profile_names() == ("sock-default", "case-a")
    assert browser_agent_main.profile_path("case-a") == identities / "case-a"
    with pytest.raises(ValueError, match="profile name"):
        browser_agent_main.profile_path("../escape")


def test_chromium_uses_compose_shared_memory() -> None:
    assert "--disable-dev-shm-usage" not in browser_agent_main.CHROMIUM_ARGS
    assert "--disable-dev-shm-usage" in browser_agent_main.CHROMIUM_IGNORED_DEFAULT_ARGS


def test_meta_navigation_hosts_remain_available_with_stale_operator_allowlist() -> None:
    assert browser_agent_main.host_allowed("www.facebook.com")
    assert browser_agent_main.host_allowed("connect.facebook.net")
    assert browser_agent_main.host_allowed("accountscenter.meta.com")
    assert browser_agent_main.host_allowed("static.xx.fbcdn.net")


def test_snapchat_account_navigation_is_allowlisted() -> None:
    assert browser_agent_main.host_allowed("snapchat.com")
    assert browser_agent_main.host_allowed("accounts.snapchat.com")


def test_manual_browser_can_follow_allowlisted_http_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_agent_main,
        "resolve_public_addresses",
        lambda host, port: {"157.240.240.174"},
    )
    assert (
        browser_agent_main.validate_network_target(
            "http://www.instagram.com/",
            require_allowlist=True,
            allow_http=True,
        )
        == "http://www.instagram.com/"
    )
    with pytest.raises(ValueError, match="HTTPS"):
        browser_agent_main.validate_network_target(
            "http://www.instagram.com/",
            require_allowlist=True,
        )


@pytest.mark.asyncio
async def test_popup_navigation_without_frame_is_treated_as_top_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_agent_main,
        "resolve_public_addresses",
        lambda host, port: {"142.250.74.109"},
    )

    class FakeRequest:
        url = "https://accounts.google.com/v3/signin/identifier"

        @staticmethod
        def is_navigation_request() -> bool:
            return True

        @property
        def frame(self) -> object:
            raise browser_agent_main.PlaywrightError("frame is not available yet")

    class FakeRoute:
        request = FakeRequest()
        action: str | None = None

        async def continue_(self) -> None:
            self.action = "continued"

        async def abort(self, _: str) -> None:
            self.action = "aborted"

    route = FakeRoute()
    await browser_agent_main.guard_browser_request(route)
    assert route.action == "continued"


@pytest.mark.asyncio
async def test_child_frame_navigation_is_not_treated_as_top_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_agent_main,
        "resolve_public_addresses",
        lambda host, port: {"157.240.240.174"},
    )

    class FakePage:
        main_frame = object()

    class FakeFrame:
        page = FakePage()

    class FakeRequest:
        url = "https://connect.facebook.net/en_US/sdk/xd_arbiter/"
        frame = FakeFrame()

        @staticmethod
        def is_navigation_request() -> bool:
            return True

    class FakeRoute:
        request = FakeRequest()
        action: str | None = None

        async def continue_(self) -> None:
            self.action = "continued"

        async def abort(self, _: str) -> None:
            self.action = "aborted"

    route = FakeRoute()
    await browser_agent_main.guard_browser_request(route)
    assert route.action == "continued"


def test_platform_policies_require_login_only_for_authenticated_social_sources() -> None:
    assert browser_agent_main.platform_policy("https://www.instagram.com/example/") == (
        "instagram",
        "social_profile",
        False,
    )
    assert browser_agent_main.platform_policy("https://www.facebook.com/example") == (
        "facebook",
        "social_profile",
        True,
    )
    assert browser_agent_main.platform_policy("https://www.tiktok.com/@example") == (
        "tiktok",
        "social_profile",
        False,
    )
    assert browser_agent_main.platform_policy("https://www.strava.com/activities/123") == (
        "strava",
        "location_history",
        False,
    )
    assert browser_agent_main.platform_policy("https://www.strava.com/settings/profile") is None
    assert browser_agent_main.platform_policy("https://www.google.com/") is None
    assert browser_agent_main.platform_policy("https://connect.garmin.com/modern/settings") is None
    assert browser_agent_main.platform_policy("https://www.instagram.com/p/example/") is None
    assert browser_agent_main.platform_policy("https://www.facebook.com/groups/example") is None
    assert browser_agent_main.platform_policy("https://www.tiktok.com/@example/video/123") is None
    assert browser_agent_main.platform_policy("https://www.facebook.com/profile.php?id=123") == (
        "facebook",
        "social_profile",
        True,
    )


def test_browser_language_header_matches_originating_browser() -> None:
    assert browser_agent_main.format_accept_language(["en-IE", "en", "fr"]) == (
        "en-IE,en;q=0.9,fr;q=0.8"
    )
    assert browser_agent_main.format_accept_language([]) == "en-US,en;q=0.9"


@pytest.mark.asyncio
async def test_browser_identity_is_exported_from_context() -> None:
    class FakePage:
        closed = False

        async def evaluate(self, _script: str) -> dict[str, object]:
            return {
                "userAgent": "Mozilla/5.0 Synthetic Chrome/149.0",
                "languages": ["en-IE", "en"],
            }

        async def close(self) -> None:
            self.closed = True

    class FakeContext:
        def __init__(self) -> None:
            self.pages: list[FakePage] = []
            self.page = FakePage()

        async def new_page(self) -> FakePage:
            self.pages.append(self.page)
            return self.page

    context = FakeContext()
    user_agent, accept_language = await browser_agent_main.browser_session_identity(context)

    assert user_agent == "Mozilla/5.0 Synthetic Chrome/149.0"
    assert accept_language == "en-IE,en;q=0.9"
    assert context.page.closed is True


def test_authenticated_session_detection_requires_complete_cookie_pairs() -> None:
    active = browser_agent_main.authenticated_session_names(
        [
            {"name": "sessionid", "domain": ".instagram.com"},
            {"name": "csrftoken", "domain": ".instagram.com"},
            {"name": "c_user", "domain": ".facebook.com"},
            {"name": "xs", "domain": ".facebook.com"},
        ]
    )
    assert active == ("instagram", "facebook")
    assert (
        browser_agent_main.authenticated_session_names(
            [{"name": "csrftoken", "domain": ".instagram.com"}]
        )
        == ()
    )


@pytest.mark.asyncio
async def test_session_export_does_not_authenticate_from_expired_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeContext:
        async def cookies(self) -> list[dict[str, object]]:
            return [
                {
                    "name": "sessionid",
                    "value": "expired-session",
                    "domain": ".instagram.com",
                    "path": "/",
                    "expires": 1,
                },
                {
                    "name": "csrftoken",
                    "value": "active-csrf",
                    "domain": ".instagram.com",
                    "path": "/",
                    "expires": 1_900_000_000,
                },
            ]

    async def fake_identity(_: object) -> tuple[str, str]:
        return "Mozilla/5.0 Synthetic", "en-IE,en;q=0.9"

    monkeypatch.setattr(browser_agent_main, "_context", FakeContext())
    monkeypatch.setattr(browser_agent_main, "browser_session_identity", fake_identity)

    result = await browser_agent_main.export_session(
        browser_agent_main.SessionExportRequest(collector_id="instaloader")
    )

    assert result.authenticated is False
    assert [cookie.name for cookie in result.cookies] == ["csrftoken"]


@pytest.mark.asyncio
async def test_case_scoped_browser_evidence_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    investigation_id = "b0a2da45-4345-4aa8-b707-352efdf34431"
    evidence_root = tmp_path / "evidence"
    case_root = evidence_root / investigation_id / "job-one"
    case_root.mkdir(parents=True)
    (case_root / "page.png").write_bytes(b"image")
    (case_root / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(browser_agent_main, "EVIDENCE_DIR", evidence_root)
    monkeypatch.setattr(browser_agent_main, "_pending_job", None)

    result = await browser_agent_main.delete_investigation_evidence(investigation_id)

    assert result == {
        "status": "deleted",
        "investigation_id": investigation_id,
        "removed_files": 2,
    }
    assert not (evidence_root / investigation_id).exists()
    with pytest.raises(browser_agent_main.HTTPException):
        await browser_agent_main.delete_investigation_evidence("../unsafe")


def test_browser_export_filters_domain_and_expired_cookies() -> None:
    selected = filter_session_cookies(
        [
            {
                "name": "sessionid",
                "value": "synthetic-session",
                "domain": ".instagram.com",
                "path": "/",
                "expires": 1_900_000_000,
                "secure": True,
                "httpOnly": True,
            },
            {
                "name": "expired",
                "value": "synthetic-expired",
                "domain": ".instagram.com",
                "expires": 1,
            },
            {
                "name": "unrelated",
                "value": "synthetic-unrelated",
                "domain": ".google.com",
                "expires": 1_900_000_000,
            },
        ],
        ("instagram.com",),
    )

    assert [cookie.name for cookie in selected] == ["sessionid"]
    assert selected[0].http_only is True
    assert selected[0].value == "synthetic-session"
