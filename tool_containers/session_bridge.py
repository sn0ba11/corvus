"""Validate and materialise scoped browser sessions inside a tool runner."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SESSION_HOSTS: dict[str, tuple[str, ...]] = {
    "instaloader": ("instagram.com",),
    "gallery_dl": ("instagram.com", "tiktok.com"),
    "maigret": (
        "instagram.com",
        "facebook.com",
        "linkedin.com",
        "x.com",
        "twitter.com",
        "tiktok.com",
        "reddit.com",
        "github.com",
        "gitlab.com",
    ),
}
COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,256}$")
MAX_USER_AGENT_LENGTH = 512
MAX_ACCEPT_LANGUAGE_LENGTH = 256


def domain_allowed(domain: str, allowed_hosts: tuple[str, ...]) -> bool:
    normalised = domain.lower().strip().lstrip(".").rstrip(".")
    return any(normalised == host or normalised.endswith(f".{host}") for host in allowed_hosts)


def _validated_header(value: Any, *, field: str, maximum: int) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid {field}")
    candidate = value.strip()
    if not candidate or len(candidate) > maximum or any(char in candidate for char in "\r\n\x00"):
        raise ValueError(f"invalid {field}")
    return candidate


def validate_session_cookies(tool_id: str, value: Any) -> list[dict[str, Any]]:
    if value in (None, []):
        return []
    if tool_id not in SESSION_HOSTS:
        raise ValueError("this tool does not accept browser sessions")
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("invalid session cookie collection")
    selected: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("invalid session cookie")
        name = str(item.get("name") or "")
        cookie_value = str(item.get("value") or "")
        domain = str(item.get("domain") or "").lower().strip()
        path = str(item.get("path") or "/")
        if not COOKIE_NAME.fullmatch(name):
            raise ValueError("invalid session cookie name")
        if len(cookie_value) > 8192 or any(char in cookie_value for char in "\r\n\t"):
            raise ValueError("invalid session cookie value")
        if not domain_allowed(domain, SESSION_HOSTS[tool_id]):
            raise ValueError("session cookie domain is outside the collector scope")
        if not path.startswith("/") or len(path) > 1024 or any(char in path for char in "\r\n\t"):
            raise ValueError("invalid session cookie path")
        try:
            expires = float(item.get("expires", -1))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid session cookie expiry") from exc
        selected.append(
            {
                "name": name,
                "value": cookie_value,
                "domain": domain,
                "path": path,
                "expires": expires,
                "secure": bool(item.get("secure")),
                "http_only": bool(item.get("http_only")),
            }
        )
    return selected


def validate_session_context(tool_id: str, value: Any) -> dict[str, Any]:
    if value in (None, {}):
        return {"cookies": [], "user_agent": None, "accept_language": None}
    if not isinstance(value, dict):
        raise ValueError("invalid browser session context")
    cookies = validate_session_cookies(tool_id, value.get("cookies"))
    user_agent = _validated_header(
        value.get("user_agent"), field="session user agent", maximum=MAX_USER_AGENT_LENGTH
    )
    accept_language = _validated_header(
        value.get("accept_language"),
        field="session accept language",
        maximum=MAX_ACCEPT_LANGUAGE_LENGTH,
    )
    if tool_id == "instaloader" and cookies and not user_agent:
        raise ValueError("Instagram browser sessions require the originating user agent")
    return {
        "cookies": cookies,
        "user_agent": user_agent,
        "accept_language": accept_language,
    }


def write_netscape_cookie_jar(path: Path, cookies: list[dict[str, Any]]) -> None:
    lines = ["# Netscape HTTP Cookie File", "# Scoped export from Corvus"]
    for cookie in cookies:
        domain = str(cookie["domain"])
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        domain_field = f"#HttpOnly_{domain}" if cookie["http_only"] else domain
        expires = int(cookie["expires"]) if float(cookie["expires"]) > 0 else 0
        lines.append(
            "\t".join(
                (
                    domain_field,
                    include_subdomains,
                    str(cookie["path"]),
                    "TRUE" if cookie["secure"] else "FALSE",
                    str(expires),
                    str(cookie["name"]),
                    str(cookie["value"]),
                )
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def write_instaloader_session_bundle(path: Path, session_context: dict[str, Any]) -> None:
    payload = {
        "cookies": session_context.get("cookies", []),
        "user_agent": session_context.get("user_agent"),
        "accept_language": session_context.get("accept_language"),
    }
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    path.chmod(0o600)


def write_instaloader_cookie_bundle(path: Path, cookies: list[dict[str, Any]]) -> None:
    """Write the legacy cookies-only bundle retained for compatibility."""
    path.write_text(json.dumps(cookies, separators=(",", ":")), encoding="utf-8")
    path.chmod(0o600)
