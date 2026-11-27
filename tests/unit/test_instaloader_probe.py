import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    instaloader = ModuleType("instaloader")
    instaloader.Instaloader = object  # type: ignore[attr-defined]
    exceptions = ModuleType("instaloader.exceptions")
    context = ModuleType("instaloader.instaloadercontext")

    class RateController:
        def __init__(self, loader_context: object) -> None:
            self._context = loader_context

    context.RateController = RateController  # type: ignore[attr-defined]
    for name in (
        "BadResponseException",
        "ConnectionException",
        "LoginRequiredException",
        "ProfileNotExistsException",
        "QueryReturnedBadRequestException",
        "TooManyRequestsException",
    ):
        setattr(exceptions, name, type(name, (Exception,), {}))
    monkeypatch.setitem(sys.modules, "instaloader", instaloader)
    monkeypatch.setitem(sys.modules, "instaloader.exceptions", exceptions)
    monkeypatch.setitem(sys.modules, "instaloader.instaloadercontext", context)
    path = Path(__file__).parents[2] / "tool_containers" / "instaloader_probe.py"
    spec = importlib.util.spec_from_file_location("instaloader_probe_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rate_controller_fails_fast_on_429(probe: ModuleType) -> None:
    controller = probe.FastFailRateController(object())  # type: ignore[attr-defined]
    with pytest.raises(Exception, match="automatic retry disabled"):
        controller.handle_429("profile")


def test_optional_profile_counts_survive_partial_graphql_refusal(probe: ModuleType) -> None:
    class Profile:
        followers = 126
        followees = None
        mediacount = "not returned"

    assert probe.optional_profile_count(Profile(), "followers") == 126
    assert probe.optional_profile_count(Profile(), "followees") is None
    assert probe.optional_profile_count(Profile(), "mediacount") is None

    class BlockedProfile:
        @property
        def mediacount(self) -> int:
            raise probe.QueryReturnedBadRequestException("400 feedback_required")

    assert probe.optional_profile_count(BlockedProfile(), "mediacount") is None
    assert probe.is_rate_limit_error(RuntimeError('400 status "feedback_required"')) is True


def test_wrapped_connection_429_is_recognised_as_rate_limit(probe: ModuleType) -> None:
    error = RuntimeError(
        "JSON Query failed: 429 Too Many Requests when accessing Instagram profile endpoint"
    )
    assert probe.is_rate_limit_error(error) is True  # type: ignore[attr-defined]
    assert probe.is_rate_limit_error(RuntimeError("connection reset")) is False  # type: ignore[attr-defined]


def test_cookie_selection_keeps_matching_root_session_pair(probe: ModuleType) -> None:
    cookies = [
        {
            "name": "sessionid",
            "value": "root-session",
            "domain": ".instagram.com",
            "path": "/",
            "expires": 100,
        },
        {
            "name": "csrftoken",
            "value": "root-csrf",
            "domain": ".instagram.com",
            "path": "/",
            "expires": 100,
        },
        {
            "name": "sessionid",
            "value": "www-session",
            "domain": "www.instagram.com",
            "path": "/",
            "expires": 200,
        },
    ]

    selected = probe._select_browser_cookies(cookies)  # type: ignore[attr-defined]

    assert selected["sessionid"] == "root-session"
    assert selected["csrftoken"] == "root-csrf"


def test_cookie_selection_rejects_out_of_scope_cookie(probe: ModuleType) -> None:
    with pytest.raises(ValueError, match="outside Instagram scope"):
        probe._select_browser_cookies(  # type: ignore[attr-defined]
            [{"name": "sessionid", "value": "secret", "domain": ".example.com"}]
        )


def test_authenticated_profile_prefers_hd_avatar_and_public_contact_fields(
    probe: ModuleType,
) -> None:
    class Profile:
        profile_pic_url = "https://cdn.example/standard.jpg"

    avatar_url, quality, width, height = probe.select_avatar(  # type: ignore[attr-defined]
        Profile(),
        {
            "hd_profile_pic_url_info": {
                "url": "https://cdn.example/hd.jpg",
                "width": 1080,
                "height": 1080,
            },
            "public_phone_country_code": "353",
            "public_phone_number": "871234567",
        },
    )

    assert avatar_url == "https://cdn.example/hd.jpg"
    assert quality == "hd"
    assert (width, height) == (1080, 1080)
    assert (
        probe.public_phone_number(  # type: ignore[attr-defined]
            {"public_phone_country_code": "353", "public_phone_number": "871234567"}
        )
        == "+353871234567"
    )


def test_authenticated_api_identity_uses_browser_user_agent(probe: ModuleType) -> None:
    class Context:
        iphone_headers = {"User-Agent": "Instagram native default"}

    class Loader:
        context = Context()

    loader: Any = Loader()
    probe.configure_instagram_api_identity(  # type: ignore[attr-defined]
        loader, "Mozilla/5.0 Synthetic Chrome/149.0"
    )

    assert loader.context.iphone_headers["User-Agent"] == "Mozilla/5.0 Synthetic Chrome/149.0"


def test_probe_uses_embedded_page_data_and_never_queries_blocked_profile_properties(
    probe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProfile:
        _node = {
            "pk": "44263741625", "username": "lazeredwithlove",
            "full_name": "Lazered With Love",
            "biography": "Irish gifts", "profile_pic_url": "https://cdn.example/profile.jpg",
            "follower_count": 3379, "following_count": 472, "all_media_count": 392,
            "is_verified": False, "is_private": False,
        }

        @property
        def profile_pic_url(self) -> str:
            raise AssertionError("Property causes forbidden iPhone API request")

        @property
        def full_name(self) -> str:
            raise AssertionError("Property causes forbidden GraphQL request")

    class FakeProfileType:
        @staticmethod
        def from_username(context: object, username: str) -> FakeProfile:
            assert username == "lazeredwithlove"
            return FakeProfile()

    class FakeLoader:
        def __init__(self, **kwargs: object) -> None:
            self.context = type("Context", (), {})()

    monkeypatch.setattr(probe.instaloader, "Profile", FakeProfileType, raising=False)
    monkeypatch.setattr(probe.instaloader, "Instaloader", FakeLoader)
    monkeypatch.setattr(probe, "load_optional_session", lambda *a, **kw: (True, None))
    monkeypatch.setattr(probe, "read_browser_session_context", lambda: (None, None))
    monkeypatch.setattr(probe, "download_avatar", lambda *a: ("profile_pic.jpg", None))
    monkeypatch.setattr(sys, "argv", ["instaloader-osint", "--username", "lazeredwithlove"])
    monkeypatch.chdir(tmp_path)
    assert probe.main() == 0
    record = json.loads((tmp_path / "profile.json").read_text(encoding="utf-8"))
    assert record["status"] == "found"
    assert record["account_id"] == "44263741625"
    assert record["followers"] == 3379
    assert record["following"] == 472
    assert record["media_count"] == 392
    assert record["avatar_url"] == "https://cdn.example/profile.jpg"


def test_public_profile_node_never_triggers_blocked_api_properties(
    probe: ModuleType,
) -> None:
    class BrokenProfile:
        @property
        def profile_pic_url(self) -> str:
            raise probe.QueryReturnedBadRequestException("400 feedback_required")

    node = {
        "username": "lazeredwithlove",
        "id": "44263741625",
        "full_name": "lazeredwithlove",
        "profile_pic_url": "https://cdn.example/profile.jpg",
        "follower_count": 3379,
        "following_count": 472,
        "all_media_count": 392,
    }
    assert probe.select_avatar(node, {}) == (
        "https://cdn.example/profile.jpg", "standard", None, None
    )
    assert probe.select_avatar(BrokenProfile(), {}) == (None, "unavailable", None, None)
    assert probe.optional_node_count(node, "edge_followed_by", "follower_count") == 3379
    assert probe.optional_node_count(node, "edge_follow", "following_count") == 472
    assert probe.optional_node_count(node, "edge_owner_to_timeline_media", "all_media_count") == 392
    assert probe.optional_node_count({"edge_followed_by": {"count": None}},
                                     "edge_followed_by", "follower_count") is None


def test_anonymous_profile_uses_standard_avatar(probe: ModuleType) -> None:
    class Profile:
        profile_pic_url = "https://cdn.example/standard.jpg"

    assert probe.select_avatar(Profile(), {}) == (  # type: ignore[attr-defined]
        "https://cdn.example/standard.jpg",
        "standard",
        None,
        None,
    )


def test_browser_session_is_verified_with_browser_identity_and_proxy(
    probe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie_file = tmp_path / "session.json"
    cookie_file.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "sessionid", "value": "session", "domain": ".instagram.com"},
                    {"name": "csrftoken", "value": "csrf", "domain": ".instagram.com"},
                ],
                "user_agent": "Mozilla/5.0 Synthetic Chrome/149.0",
                "accept_language": "en-IE,en;q=0.9",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OSINT_SESSION_COOKIE_FILE", str(cookie_file))
    browser_context, warning = probe.read_browser_session_context()  # type: ignore[attr-defined]

    class Session:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.proxies: dict[str, str] = {}
            self.trust_env = True

    class Context:
        username: str | None = None
        _session = Session()

        def get_anonymous_session(self) -> Session:
            return Session()

    class Loader:
        context = Context()
        loaded: tuple[str, dict[str, str]] | None = None

        def load_session(self, username: str, session: dict[str, str]) -> None:
            self.loaded = (username, session)
            self.context._session = Session()

        def test_login(self) -> str:
            raise AssertionError("legacy GraphQL test_login must never be called")

    loader: Any = Loader()
    loaded, load_warning = probe.load_browser_session(  # type: ignore[attr-defined]
        loader,
        browser_context,
        proxy="socks5h://127.0.0.1:9050",
    )

    assert warning is None
    assert loaded is True
    assert load_warning is not None and "not independently verified" in load_warning
    assert loader.loaded is not None
    assert loader.context._session.headers["Accept-Language"] == "en-IE,en;q=0.9"
    assert loader.context._session.proxies["https"] == "socks5h://127.0.0.1:9050"
    assert loader.context._session.trust_env is False
    assert browser_context["user_agent"] == "Mozilla/5.0 Synthetic Chrome/149.0"


def test_logged_in_browser_api_challenge_is_not_mistaken_for_logout(probe: ModuleType) -> None:
    class Session:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.proxies: dict[str, str] = {}
            self.trust_env = True

    class Context:
        username: str | None = None
        _session = Session()

        def get_anonymous_session(self) -> Session:
            return Session()

    class Loader:
        context = Context()

        def load_session(self, username: str, session: dict[str, str]) -> None:
            assert username == "managed-browser"
            assert "sessionid" in session

        def test_login(self) -> str:
            raise probe.QueryReturnedBadRequestException('400 Bad Request - "feedback_required"')

    context = {
        "cookies": [
            {"name": "sessionid", "value": "synthetic-session", "domain": ".instagram.com"},
            {"name": "csrftoken", "value": "synthetic-token", "domain": ".instagram.com"},
        ],
        "user_agent": "Mozilla/5.0 Synthetic Chrome/149.0",
        "accept_language": "en-IE,en;q=0.9",
    }
    loaded, warning = probe.load_browser_session(Loader(), context, proxy=None)
    assert loaded is True
    assert warning is not None
    assert "not independently verified" in warning


def test_instaloader_login_check_returning_none_is_inconclusive(
    probe: ModuleType,
) -> None:
    class Session:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.proxies: dict[str, str] = {}
            self.trust_env = True

    class Context:
        username: str | None = None
        _session = Session()

        def get_anonymous_session(self) -> Session:
            return Session()

    class Loader:
        context = Context()

        def load_session(self, username: str, session: dict[str, str]) -> None:
            assert "sessionid" in session

        def test_login(self) -> None:
            return None

    browser_context = {
        "cookies": [
            {"name": "sessionid", "value": "fake", "domain": ".instagram.com"},
            {"name": "csrftoken", "value": "fake", "domain": ".instagram.com"},
        ],
        "user_agent": "Mozilla/5.0 Synthetic Browser",
        "accept_language": "en-IE,en;q=0.9",
    }
    loaded, warning = probe.load_browser_session(Loader(), browser_context, proxy=None)
    assert loaded
    assert warning is not None
    assert "not independently verified" in warning
    assert "reopen the sock browser" not in warning


def test_instaloader_proxy_validation_is_bounded(probe: ModuleType) -> None:
    assert (
        probe.validated_proxy("socks5h://127.0.0.1:9050")  # type: ignore[attr-defined]
        == "socks5h://127.0.0.1:9050"
    )
    assert probe.validated_proxy(None) is None  # type: ignore[attr-defined]
    for value in ("file:///tmp/proxy", "https://proxy.example/path", "https://proxy.example\n"):
        with pytest.raises(ValueError):
            probe.validated_proxy(value)  # type: ignore[attr-defined]


def test_legacy_cookie_only_bundle_is_rejected_as_unfaithful(
    probe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie_file = tmp_path / "legacy.json"
    cookie_file.write_text(
        json.dumps([{"name": "sessionid", "value": "session", "domain": ".instagram.com"}]),
        encoding="utf-8",
    )
    monkeypatch.setenv("OSINT_SESSION_COOKIE_FILE", str(cookie_file))

    context, warning = probe.read_browser_session_context()  # type: ignore[attr-defined]

    assert context is None
    assert warning is not None
    assert "browser identity" in warning


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self._response


def _loader_with_session(response: _FakeResponse) -> Any:
    class Context:
        def __init__(self) -> None:
            self._session = _FakeSession(response)

    class Loader:
        def __init__(self) -> None:
            self.context = Context()

    return Loader()


def test_account_recovery_hint_extracts_obfuscated_contact_fields(probe: ModuleType) -> None:
    loader = _loader_with_session(
        _FakeResponse(
            payload={
                "obfuscated_email": "me********s@example.org",
                "obfuscated_phone_number": "+00 0xx xxx xx 00",
            }
        )
    )

    hint, warning = probe.account_recovery_hint(loader, "someusername")  # type: ignore[attr-defined]

    assert hint == {
        "obfuscated_email": "me********s@example.org",
        "obfuscated_phone_number": "+00 0xx xxx xx 00",
    }
    assert warning is None
    call = loader.context._session.calls[0]
    assert call["url"] == probe.RECOVERY_LOOKUP_URL  # type: ignore[attr-defined]
    assert call["headers"]["X-IG-App-ID"] == probe.RECOVERY_LOOKUP_APP_ID  # type: ignore[attr-defined]
    assert "someusername" in call["data"]


def test_account_recovery_hint_reports_rate_limit_without_raising(probe: ModuleType) -> None:
    loader = _loader_with_session(_FakeResponse(status_code=429))

    hint, warning = probe.account_recovery_hint(loader, "someusername")  # type: ignore[attr-defined]

    assert hint == {}
    assert warning is not None
    assert "rate limited" in warning


def test_account_recovery_hint_survives_unreadable_response(probe: ModuleType) -> None:
    loader = _loader_with_session(_FakeResponse(payload=None))

    hint, warning = probe.account_recovery_hint(loader, "someusername")  # type: ignore[attr-defined]

    assert hint == {}
    assert warning is not None
