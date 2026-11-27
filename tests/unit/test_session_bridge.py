from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from tool_containers.session_bridge import (
    validate_session_context,
    validate_session_cookies,
    write_instaloader_cookie_bundle,
    write_instaloader_session_bundle,
    write_netscape_cookie_jar,
)


def cookie(domain: str = ".instagram.com") -> dict[str, object]:
    return {
        "name": "sessionid",
        "value": "synthetic-session-value",
        "domain": domain,
        "path": "/",
        "expires": 1_900_000_000,
        "secure": True,
        "http_only": True,
    }


def test_collector_cookie_scopes_are_enforced() -> None:
    assert validate_session_cookies("instaloader", [cookie()])[0]["domain"] == ".instagram.com"
    assert (
        validate_session_cookies("maigret", [cookie(".github.com")])[0]["domain"] == ".github.com"
    )
    with pytest.raises(ValueError, match="outside the collector scope"):
        validate_session_cookies("instaloader", [cookie(".github.com")])
    with pytest.raises(ValueError, match="does not accept"):
        validate_session_cookies("sherlock", [cookie()])


def test_session_files_are_private_and_bounded(tmp_path: Path) -> None:
    cookies = validate_session_cookies("instaloader", [cookie()])
    bundle = tmp_path / "instaloader.json"
    structured = tmp_path / "instaloader-context.json"
    jar = tmp_path / "maigret.txt"
    write_instaloader_cookie_bundle(bundle, cookies)
    context = validate_session_context(
        "instaloader",
        {
            "cookies": cookies,
            "user_agent": "Mozilla/5.0 Synthetic Chrome/149.0",
            "accept_language": "en-IE,en;q=0.9",
        },
    )
    write_instaloader_session_bundle(structured, context)
    write_netscape_cookie_jar(jar, cookies)

    assert json.loads(bundle.read_text(encoding="utf-8"))[0]["name"] == "sessionid"
    structured_payload = json.loads(structured.read_text(encoding="utf-8"))
    assert structured_payload["user_agent"].endswith("Chrome/149.0")
    assert structured_payload["cookies"][0]["name"] == "sessionid"
    assert "#HttpOnly_.instagram.com" in jar.read_text(encoding="utf-8")
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o600
    assert stat.S_IMODE(structured.stat().st_mode) == 0o600
    assert stat.S_IMODE(jar.stat().st_mode) == 0o600


def test_instagram_context_requires_originating_user_agent() -> None:
    with pytest.raises(ValueError, match="originating user agent"):
        validate_session_context("instaloader", {"cookies": [cookie()]})
    with pytest.raises(ValueError, match="session user agent"):
        validate_session_context(
            "instaloader",
            {"cookies": [cookie()], "user_agent": "Mozilla/5.0\nforged"},
        )


def test_cookie_control_characters_are_rejected() -> None:
    unsafe = cookie()
    unsafe["value"] = "value\nforged"
    with pytest.raises(ValueError, match="invalid session cookie value"):
        validate_session_cookies("instaloader", [unsafe])
