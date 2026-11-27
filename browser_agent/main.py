from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import tarfile
import tempfile
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
from uuid import UUID

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Response,
    status,
)
from fastapi import (
    Request as FastAPIRequest,
)
from nacl import pwhash, secret
from nacl.exceptions import CryptoError
from playwright.async_api import (  # type: ignore[import-not-found]
    BrowserContext,
    Page,
    Playwright,
    Request,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, Field

from browser_agent.social_profiles import collect_profile_objects, visible_contact_profile

ExtractionKind = Literal["social_profile", "location_history", "search_results"]
LOGGER = logging.getLogger("corvus.browser_agent")

PROFILE_DIR = Path(os.getenv("BROWSER_PROFILE_DIR", "/profile"))
EVIDENCE_DIR = Path(os.getenv("BROWSER_EVIDENCE_DIR", "/evidence"))
AGENT_TOKEN = os.getenv("BROWSER_AGENT_TOKEN", "")
STEALTH_BROWSER_ENABLED = os.getenv("STEALTH_BROWSER_ENABLED", "false").strip().casefold() in {
    "1",
    "true",
    "yes",
}
CONFIGURED_BROWSER_ENGINE = os.getenv("BROWSER_ENGINE", "chromium").strip().casefold()
if CONFIGURED_BROWSER_ENGINE not in {"chromium", "firefox"}:
    raise RuntimeError("BROWSER_ENGINE must be 'chromium' or 'firefox'")
BROWSER_LOCALE = os.getenv("BROWSER_LOCALE", "en-IE").strip() or "en-IE"
# Built-in hosts always apply; BROWSER_ALLOWED_HOSTS adds to them and cannot remove any, so
# an environment file written by an older version cannot leave newer hosts refused.
BUILT_IN_ALLOWED_HOSTS = (
    "google.com,google.ie,bing.com,mojeek.com,duckduckgo.com,brave.com,qwant.com,startpage.com,tripadvisor.com,tripadvisor.ie,tripadvisor.co.uk,strava.com,alltrails.com,komoot.com,garmin.com,instagram.com,facebook.com,facebook.net,fb.com,fbcdn.net,fbsbx.com,messenger.com,meta.com,linkedin.com,x.com,twitter.com,tiktok.com,pinterest.com,threads.com,threads.net,snapchat.com,t.me,telegram.org,discord.com,discord.gg,whatsapp.com,wa.me,reddit.com,github.com,gitlab.com"
)
CONFIGURED_ALLOWED_HOSTS = tuple(
    dict.fromkeys(
        item.strip().lower().lstrip(".")
        for item in f"{BUILT_IN_ALLOWED_HOSTS},{os.getenv('BROWSER_ALLOWED_HOSTS', '')}".split(",")
        if item.strip()
    )
)
MAX_HTML_BYTES = min(max(int(os.getenv("BROWSER_MAX_HTML_BYTES", "1048576")), 4096), 2_097_152)
NAVIGATION_TIMEOUT_MS = min(
    max(int(os.getenv("BROWSER_NAVIGATION_TIMEOUT_MS", "30000")), 5000), 120_000
)
MAIGRET_SESSION_HOSTS = tuple(
    item.strip().lower().lstrip(".")
    for item in os.getenv(
        "BROWSER_MAIGRET_SESSION_HOSTS",
        "instagram.com,facebook.com,linkedin.com,x.com,twitter.com,tiktok.com,reddit.com,github.com,gitlab.com",
    ).split(",")
    if item.strip()
)
SESSION_EXPORT_HOSTS: dict[str, tuple[str, ...]] = {
    "instaloader": ("instagram.com",),
    "gallery_dl": ("instagram.com", "tiktok.com"),
    "maigret": MAIGRET_SESSION_HOSTS,
}
INTEGRAL_NAVIGATION_HOSTS = (
    "instagram.com",
    "facebook.com",
    "facebook.net",
    "fb.com",
    "fbcdn.net",
    "fbsbx.com",
    "messenger.com",
    "meta.com",
)
ALLOWED_HOSTS = tuple(
    dict.fromkeys((*CONFIGURED_ALLOWED_HOSTS, *INTEGRAL_NAVIGATION_HOSTS, *MAIGRET_SESSION_HOSTS))
)
CHROMIUM_ARGS = ("--disable-crash-reporter", "--disable-crashpad")
CHROMIUM_IGNORED_DEFAULT_ARGS = ("--disable-dev-shm-usage",)
CHROMIUM_SINGLETON_FILES = ("SingletonCookie", "SingletonLock", "SingletonSocket")
PROFILE_LOCK_FILE = ".osint-browser-agent.lock"
IDENTITIES_DIR = PROFILE_DIR / ".corvus-identities"
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,49}$")
DEFAULT_PROFILE_NAME = "sock-default"
BACKUP_MAGIC = b"CORVUS_BROWSER_BACKUP_V1\n"
BACKUP_SALT_BYTES = 16
MAX_BACKUP_BYTES = 512 * 1024 * 1024
PLATFORM_POLICIES: tuple[tuple[str, str, ExtractionKind, bool], ...] = (
    ("snapchat.com", "snapchat", "social_profile", False),
    ("t.me", "telegram", "social_profile", False),
    ("discord.com", "discord", "social_profile", False),
    ("web.whatsapp.com", "whatsapp", "social_profile", False),
    ("instagram.com", "instagram", "social_profile", False),
    ("facebook.com", "facebook", "social_profile", True),
    ("tiktok.com", "tiktok", "social_profile", False),
    ("pinterest.com", "pinterest", "social_profile", False),
    ("threads.com", "threads", "social_profile", False),
    ("threads.net", "threads", "social_profile", False),
    ("google.com", "google_maps", "location_history", False),
    ("google.ie", "google_maps", "location_history", False),
    ("tripadvisor.com", "tripadvisor", "location_history", False),
    ("tripadvisor.ie", "tripadvisor", "location_history", False),
    ("tripadvisor.co.uk", "tripadvisor", "location_history", False),
    ("strava.com", "strava", "location_history", False),
    ("alltrails.com", "alltrails", "location_history", False),
    ("komoot.com", "komoot", "location_history", False),
    ("connect.garmin.com", "garmin_connect", "location_history", False),
    ("google.com", "google_search", "search_results", False),
    ("google.ie", "google_search", "search_results", False),
    ("bing.com", "bing_search", "search_results", False),
    ("mojeek.com", "mojeek_search", "search_results", False),
    ("duckduckgo.com", "duckduckgo_search", "search_results", False),
    ("brave.com", "brave_search", "search_results", False),
    ("qwant.com", "qwant_search", "search_results", False),
    ("startpage.com", "startpage_search", "search_results", False),
)
PLATFORM_PATH_MARKERS: dict[str, tuple[str, ...]] = {
    "google_maps": ("/maps/", "/local/"),
    "tripadvisor": ("/profile/", "/members/", "/showuserreviews"),
    "strava": ("/athletes/", "/activities/"),
    "alltrails": ("/members/", "/trail/"),
    "komoot": ("/user/", "/tour/"),
    "garmin_connect": (
        "/modern/profile/",
        "/modern/activity/",
        "/app/profile/",
        "/app/activity/",
    ),
    "google_search": ("/search",),
    "bing_search": ("/search",),
    "duckduckgo_search": ("/",),
    "brave_search": ("/search",),
    "qwant_search": ("/",),
    "startpage_search": ("/sp/search",),
}

CHALLENGE_MARKERS = (
    "captcha",
    "verify you are human",
    "verifying your request",
    "checking your browser",
    "cf-chl-",
    "challenge-platform",
    "unusual traffic",
    "security check",
    "too many requests",
)

_playwright: Playwright | None = None
_context: BrowserContext | None = None
_invisible_manager: Any | None = None
_active_browser_engine: Literal["chromium", "firefox"] = CONFIGURED_BROWSER_ENGINE  # type: ignore[assignment]
_active_profile_name = DEFAULT_PROFILE_NAME
_profile_lock_fd: int | None = None
_job_lock = asyncio.Lock()
_pending_job: PendingJob | None = None


class CaptureRequest(BaseModel):
    job_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    investigation_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    url: str = Field(min_length=8, max_length=2048)
    collector_id: str = Field(default="generic_capture", pattern=r"^[a-z0-9_-]{1,50}$")


class ResumeRequest(BaseModel):
    job_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    investigation_id: str = Field(pattern=r"^[0-9a-f-]{36}$")


class SessionExportRequest(BaseModel):
    collector_id: Literal["instaloader", "gallery_dl", "maigret"]


class ProfileSwitchRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50)


class EngineSwitchRequest(BaseModel):
    engine: Literal["chromium", "firefox"]


class SessionCookie(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(max_length=8192)
    domain: str = Field(min_length=1, max_length=253)
    path: str = Field(default="/", min_length=1, max_length=1024)
    expires: float = -1
    secure: bool = False
    http_only: bool = False


class SessionExportResult(BaseModel):
    collector_id: str
    exported_at: datetime
    scope_hosts: tuple[str, ...]
    authenticated: bool
    user_agent: str = Field(min_length=1, max_length=512)
    accept_language: str = Field(min_length=1, max_length=256)
    cookies: list[SessionCookie]


class CaptureResult(BaseModel):
    job_id: str
    status: Literal["completed", "manual_action_required"]
    requested_url: str
    final_url: str
    observed_at: datetime
    page_title: str | None
    challenge_type: str | None
    screenshot_reference: str
    html_reference: str | None
    manifest_reference: str
    screenshot_sha256: str
    html_sha256: str | None
    manifest_sha256: str
    browser_version: str
    browser_engine: Literal["chromium", "firefox"] = "chromium"
    extraction_kind: ExtractionKind | None = None
    extraction_platform: str | None = None
    authenticated_session: bool | None = None
    extracted: dict[str, Any] | None = None


@dataclass
class PendingJob:
    request: CaptureRequest
    page: Page
    artifact_dir: Path
    challenges: list[dict[str, str]] = field(default_factory=list)


def require_token(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {AGENT_TOKEN}"
    if not AGENT_TOKEN or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid browser-agent token")


def host_allowed(host: str) -> bool:
    normalised = host.lower().rstrip(".")
    return any(normalised == item or normalised.endswith(f".{item}") for item in ALLOWED_HOSTS)


def cookie_domain_allowed(domain: str, scope_hosts: tuple[str, ...]) -> bool:
    normalised = domain.lower().strip().lstrip(".").rstrip(".")
    return any(normalised == host or normalised.endswith(f".{host}") for host in scope_hosts)


def authenticated_session_names(
    raw_cookies: Sequence[dict[str, object] | SessionCookie],
) -> tuple[str, ...]:
    def cookie_value(cookie: dict[str, object] | SessionCookie, field: str) -> object:
        if isinstance(cookie, SessionCookie):
            return getattr(cookie, field)
        return cookie.get(field)

    instagram_names = {
        str(cookie_value(cookie, "name") or "")
        for cookie in raw_cookies
        if cookie_domain_allowed(
            str(cookie_value(cookie, "domain") or ""), ("instagram.com",)
        )
    }
    facebook_names = {
        str(cookie_value(cookie, "name") or "")
        for cookie in raw_cookies
        if cookie_domain_allowed(
            str(cookie_value(cookie, "domain") or ""), ("facebook.com",)
        )
    }
    tiktok_names = {
        str(cookie_value(cookie, "name") or "")
        for cookie in raw_cookies
        if cookie_domain_allowed(str(cookie_value(cookie, "domain") or ""), ("tiktok.com",))
    }
    active: list[str] = []
    if {"sessionid", "csrftoken"}.issubset(instagram_names):
        active.append("instagram")
    if {"c_user", "xs"}.issubset(facebook_names):
        active.append("facebook")
    if tiktok_names.intersection({"sessionid", "sessionid_ss", "sid_tt"}):
        active.append("tiktok")
    return tuple(active)


def format_accept_language(raw_languages: object) -> str:
    if not isinstance(raw_languages, list):
        return "en-US,en;q=0.9"
    languages = [
        str(item).strip()[:35]
        for item in raw_languages[:6]
        if isinstance(item, str) and item.strip()
    ]
    if not languages:
        return "en-US,en;q=0.9"
    weighted = [languages[0]]
    for index, language in enumerate(languages[1:], start=1):
        quality = max(0.5, 1.0 - (index * 0.1))
        weighted.append(f"{language};q={quality:.1f}")
    return ",".join(weighted)[:256]


async def browser_session_identity(context: BrowserContext) -> tuple[str, str]:
    created_page = not context.pages
    page = context.pages[0] if context.pages else await context.new_page()
    try:
        payload = await page.evaluate(
            "() => ({userAgent: navigator.userAgent, languages: Array.from(navigator.languages || [])})"
        )
    finally:
        if created_page:
            await page.close()
    if not isinstance(payload, dict):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "browser identity could not be exported"
        )
    user_agent = " ".join(str(payload.get("userAgent") or "").split())[:512]
    if not user_agent:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "browser user agent could not be exported"
        )
    return user_agent, format_accept_language(payload.get("languages"))


def filter_session_cookies(
    raw_cookies: list[dict[str, object]], scope_hosts: tuple[str, ...]
) -> list[SessionCookie]:
    now = time.time()
    selected: list[SessionCookie] = []
    for raw in raw_cookies:
        name = str(raw.get("name") or "")
        domain = str(raw.get("domain") or "")
        value = str(raw.get("value") or "")
        path = str(raw.get("path") or "/")
        raw_expires = raw.get("expires", -1)
        try:
            expires = float(raw_expires) if isinstance(raw_expires, str | int | float) else -1
        except ValueError:
            expires = -1
        if (
            not name
            or len(name) > 256
            or len(value) > 8192
            or not domain
            or len(domain) > 253
            or len(path) > 1024
            or not cookie_domain_allowed(domain, scope_hosts)
        ):
            continue
        if expires > 0 and expires <= now:
            continue
        if len(selected) >= 100:
            break
        selected.append(
            SessionCookie(
                name=name,
                value=value,
                domain=domain,
                path=path if path.startswith("/") else "/",
                expires=expires,
                secure=bool(raw.get("secure")),
                http_only=bool(raw.get("httpOnly")),
            )
        )
    return selected


def acquire_profile_lock() -> int:
    lock_path = PROFILE_DIR / PROFILE_LOCK_FILE
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(lock_fd)
        raise RuntimeError("browser profile is already in use by another browser agent") from exc
    try:
        for filename in CHROMIUM_SINGLETON_FILES:
            (PROFILE_DIR / filename).unlink(missing_ok=True)
    except OSError as exc:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        raise RuntimeError("could not clear stale Chromium profile lock") from exc
    return lock_fd


def release_profile_lock(lock_fd: int) -> None:
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)


def validate_profile_name(value: str) -> str:
    name = value.strip()
    if not PROFILE_NAME_RE.fullmatch(name):
        raise ValueError("profile name must be 1-50 letters, numbers, dots, dashes or underscores")
    return name


def profile_path(name: str, engine: Literal["chromium", "firefox"] | None = None) -> Path:
    canonical = validate_profile_name(name)
    selected = engine or _active_browser_engine
    if selected == "firefox":
        return PROFILE_DIR / ".corvus-firefox" / canonical
    if canonical == DEFAULT_PROFILE_NAME:
        return PROFILE_DIR
    return IDENTITIES_DIR / canonical


def list_profile_names() -> tuple[str, ...]:
    names = [DEFAULT_PROFILE_NAME]
    for root in (IDENTITIES_DIR, PROFILE_DIR / ".corvus-firefox"):
        if root.is_dir():
            for candidate in sorted(root.iterdir()):
                if candidate.is_dir() and PROFILE_NAME_RE.fullmatch(candidate.name):
                    names.append(candidate.name)
    return tuple(dict.fromkeys(names))


def clear_chromium_singletons(path: Path) -> None:
    for filename in CHROMIUM_SINGLETON_FILES:
        (path / filename).unlink(missing_ok=True)


async def _close_active_context() -> None:
    global _context, _invisible_manager
    if _context is not None:
        try:
            await _context.close()
        except PlaywrightError:
            pass
        _context = None
    if _invisible_manager is not None:
        try:
            await _invisible_manager.__aexit__(None, None, None)
        except Exception as exc:
            LOGGER.warning("Invisible Playwright shutdown failed: %s", exc)
        _invisible_manager = None


async def activate_profile(name: str) -> None:
    global _context, _active_profile_name, _invisible_manager
    if _active_browser_engine == "chromium" and _playwright is None:
        raise RuntimeError("browser runtime is not ready")
    canonical = validate_profile_name(name)
    if (_job_lock.locked() or _pending_job is not None) and canonical != _active_profile_name:
        raise RuntimeError("cannot switch browser identity while a capture is active")
    if _context is not None and canonical == _active_profile_name:
        return
    await _close_active_context()
    path = profile_path(canonical)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if _active_browser_engine == "firefox":
        from invisible_playwright.async_api import (  # type: ignore[import-not-found]
            InvisiblePlaywright,
        )

        seed = int.from_bytes(hashlib.sha256(canonical.encode("utf-8")).digest()[:8], "big")
        _invisible_manager = InvisiblePlaywright(
            seed=seed,
            profile_dir=str(path),
            headless=False,
            humanize=True,
            locale=BROWSER_LOCALE,
        )
        invisible_browser = await _invisible_manager.__aenter__()
        bootstrap_page = await invisible_browser.new_page()
        _context = bootstrap_page.context
        await bootstrap_page.close()
    else:
        assert _playwright is not None
        clear_chromium_singletons(path)
        _context = await _playwright.chromium.launch_persistent_context(
            str(path),
            headless=False,
            viewport={"width": 1440, "height": 900},
            accept_downloads=False,
            args=list(CHROMIUM_ARGS),
            ignore_default_args=list(CHROMIUM_IGNORED_DEFAULT_ARGS),
        )
    await _context.route("**/*", guard_browser_request)
    _active_profile_name = canonical


async def switch_browser_engine(engine: Literal["chromium", "firefox"]) -> None:
    global _playwright, _active_browser_engine
    if (_job_lock.locked() or _pending_job is not None) and engine != _active_browser_engine:
        raise RuntimeError("cannot switch browser engine while a capture is active")
    if engine == _active_browser_engine and _context is not None:
        return
    current_profile = _active_profile_name
    await _close_active_context()
    if _playwright is not None:
        try:
            await _playwright.stop()
        except PlaywrightError:
            pass
        _playwright = None
    _active_browser_engine = engine
    if engine == "chromium":
        _playwright = await _async_playwright_factory()().start()
    await activate_profile(current_profile)


def _backup_key(password: str, salt: bytes) -> bytes:
    if not 12 <= len(password) <= 256:
        raise ValueError("backup password must contain 12 to 256 characters")
    return pwhash.argon2id.kdf(
        secret.SecretBox.KEY_SIZE,
        password.encode(),
        salt,
        opslimit=pwhash.argon2id.OPSLIMIT_INTERACTIVE,
        memlimit=pwhash.argon2id.MEMLIMIT_INTERACTIVE,
    )


def _tar_profile(path: Path, profile_name: str) -> bytes:
    output = io.BytesIO()
    identities_root = IDENTITIES_DIR.resolve()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for candidate in sorted(path.rglob("*")):
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if profile_name == DEFAULT_PROFILE_NAME and (
                resolved == identities_root or identities_root in resolved.parents
            ):
                continue
            if candidate.name in CHROMIUM_SINGLETON_FILES or candidate.name == PROFILE_LOCK_FILE:
                continue
            if candidate.is_symlink() or not (resolved.is_file() or resolved.is_dir()):
                continue
            archive.add(resolved, arcname=resolved.relative_to(path).as_posix(), recursive=False)
            if output.tell() > MAX_BACKUP_BYTES:
                raise ValueError("browser profile backup exceeds the 512 MiB limit")
    return output.getvalue()


def encrypt_profile_backup(profile_name: str, password: str) -> bytes:
    path = profile_path(profile_name)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = _tar_profile(path, profile_name)
    salt = os.urandom(BACKUP_SALT_BYTES)
    encrypted = bytes(secret.SecretBox(_backup_key(password, salt)).encrypt(payload))
    return BACKUP_MAGIC + salt + encrypted


def decrypt_profile_backup(profile_name: str, password: str, content: bytes) -> bytes:
    minimum = len(BACKUP_MAGIC) + BACKUP_SALT_BYTES + secret.SecretBox.NONCE_SIZE + secret.SecretBox.MACBYTES
    if len(content) < minimum or len(content) > MAX_BACKUP_BYTES + 16 * 1024 * 1024:
        raise ValueError("invalid or oversized browser profile backup")
    if not content.startswith(BACKUP_MAGIC):
        raise ValueError("invalid browser profile backup format")
    cursor = len(BACKUP_MAGIC)
    salt = content[cursor : cursor + BACKUP_SALT_BYTES]
    cursor += BACKUP_SALT_BYTES
    try:
        return bytes(secret.SecretBox(_backup_key(password, salt)).decrypt(content[cursor:]))
    except CryptoError as exc:
        raise ValueError("browser profile backup authentication failed") from exc


def restore_profile_archive(profile_name: str, content: bytes) -> None:
    target = profile_path(profile_name)
    IDENTITIES_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="corvus-browser-restore-", dir=IDENTITIES_DIR
    ) as temporary:
        staging = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > 100_000:
                raise ValueError("browser profile backup contains too many entries")
            for member in members:
                if member.issym() or member.islnk() or member.isdev():
                    raise ValueError("browser profile backup contains unsupported links or devices")
                destination = (staging / member.name).resolve()
                if staging.resolve() not in destination.parents and destination != staging.resolve():
                    raise ValueError("browser profile backup contains an unsafe path")
            archive.extractall(staging, members=members, filter="data")
        if target == PROFILE_DIR:
            for candidate in list(target.iterdir()):
                if candidate.name in {IDENTITIES_DIR.name, PROFILE_LOCK_FILE}:
                    continue
                if candidate.is_dir():
                    shutil.rmtree(candidate)
                else:
                    candidate.unlink(missing_ok=True)
            for candidate in staging.iterdir():
                shutil.move(str(candidate), str(target / candidate.name))
        else:
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(staging), str(target))
        target.chmod(0o700)
        clear_chromium_singletons(target)


def resolve_public_addresses(host: str, port: int) -> set[str]:
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("target host did not resolve") from exc
    addresses = {str(item[4][0]).split("%", 1)[0] for item in answers}
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise ValueError("target resolved to a non-public address")
    return addresses


def validate_network_target(
    raw_url: str, *, require_allowlist: bool, allow_http: bool = False
) -> str:
    parsed = urlsplit(raw_url)
    allowed_schemes = {"https", "http"} if allow_http else {"https"}
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("target must be a public HTTPS URL")
    if require_allowlist and not host_allowed(parsed.hostname):
        raise ValueError("target host is not allowlisted")
    default_port = 80 if parsed.scheme == "http" else 443
    resolve_public_addresses(parsed.hostname, parsed.port or default_port)
    return raw_url


def validate_public_target(raw_url: str) -> str:
    try:
        return validate_network_target(raw_url, require_allowlist=True)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


def is_top_level_navigation(request: Request) -> bool:
    if not request.is_navigation_request():
        return False
    try:
        frame = request.frame
    except PlaywrightError:
        # Popup navigation can be intercepted before Playwright has created its
        # Frame object. Treat that first request as top-level so the normal
        # navigation allowlist applies instead of leaving a blank popup.
        return True
    return bool(frame == frame.page.main_frame)


async def guard_browser_request(route: Route) -> None:
    request = route.request
    scheme = urlsplit(request.url).scheme.lower()
    if scheme in {"about", "blob", "data"}:
        await route.continue_()
        return
    try:
        is_navigation = request.is_navigation_request()
        validate_network_target(
            request.url,
            require_allowlist=is_top_level_navigation(request),
            allow_http=is_navigation,
        )
    except ValueError:
        await route.abort("blockedbyclient")
        return
    await route.continue_()


def safe_artifact_dir(investigation_id: str, job_id: str) -> Path:
    destination = (EVIDENCE_DIR / investigation_id / job_id).resolve()
    if EVIDENCE_DIR.resolve() not in destination.parents:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid evidence path")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    return destination


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


AUTOMATIC_CHECK_MARKERS = (
    "verifying your request",
    "making sure you're not a bot",
    "checking your browser",
)
AUTOMATIC_CHECK_WAIT_MS = 20_000


def is_automatic_browser_check(visible_text: str) -> bool:
    """True for an interstitial that clears by itself, such as an Anubis proof-of-work page."""
    lowered = visible_text.casefold()
    return any(marker in lowered for marker in AUTOMATIC_CHECK_MARKERS)


async def wait_for_automatic_browser_check(page: Page) -> None:
    """Give a self-completing browser check time to finish before the page is captured.

    Some sites show a short "verifying your request" page that the visitor's own browser
    completes without any interaction. Capturing during it records an empty page. This only
    waits; it never clicks, types or answers anything, and a page still showing a check or
    a CAPTCHA afterwards goes through the normal manual-action path.
    """
    waited = 0
    while waited < AUTOMATIC_CHECK_WAIT_MS:
        try:
            text = await page.evaluate(
                "document.body ? document.body.innerText.slice(0, 2000) : ''"
            )
        except PlaywrightError:
            return
        if not isinstance(text, str) or not is_automatic_browser_check(text):
            return
        await page.wait_for_timeout(1000)
        waited += 1000
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except PlaywrightError:
        pass


async def detect_challenge(page: Page) -> str | None:
    title = (await page.title()).lower()
    try:
        body = (await page.locator("body").inner_text(timeout=3000))[:100_000].lower()
    except Exception:  # Playwright raises platform-specific errors for detached or blocked pages.
        body = ""
    content = f"{title}\n{body}"
    marker = next((item for item in CHALLENGE_MARKERS if item in content), None)
    if marker:
        return marker
    parsed = urlsplit(page.url)
    login_path = parsed.path.lower()
    if any(item in login_path for item in ("/login", "/signin", "/sign-in")):
        return "login_wall"
    try:
        if await page.locator('input[type="password"]:visible').count():
            return "login_wall"
    except Exception:
        return None
    return None


def relative_root(job: PendingJob) -> str:
    return f"{job.request.investigation_id}/{job.request.job_id}"


INSTAGRAM_RESERVED_PATHS = {
    "about",
    "accounts",
    "developer",
    "direct",
    "explore",
    "legal",
    "p",
    "reel",
    "reels",
    "stories",
    "web",
}
FACEBOOK_RESERVED_PATHS = {
    "ads",
    "business",
    "events",
    "friends",
    "gaming",
    "groups",
    "help",
    "home.php",
    "login",
    "marketplace",
    "messages",
    "notifications",
    "pages",
    "privacy",
    "reel",
    "reels",
    "settings",
    "share",
    "stories",
    "watch",
}


def _path_segments(raw_url: str) -> tuple[str, ...]:
    return tuple(part for part in unquote(urlsplit(raw_url).path).split("/") if part)


def _instagram_profile_url(raw_url: str) -> bool:
    segments = _path_segments(raw_url)
    return len(segments) == 1 and segments[0].casefold() not in INSTAGRAM_RESERVED_PATHS


def _facebook_profile_url(raw_url: str) -> bool:
    parsed = urlsplit(raw_url)
    path = unquote(parsed.path)
    segments = tuple(part for part in path.split("/") if part)
    if path.casefold() == "/profile.php":
        identifiers = parse_qs(parsed.query).get("id", [])
        return bool(identifiers and identifiers[0].isdigit())
    if len(segments) == 3 and segments[0].casefold() == "people":
        return segments[2].isdigit()
    return len(segments) == 1 and segments[0].casefold() not in FACEBOOK_RESERVED_PATHS


def _tiktok_profile_url(raw_url: str) -> bool:
    segments = _path_segments(raw_url)
    return len(segments) == 1 and segments[0].startswith("@") and len(segments[0]) > 1


def platform_policy(raw_url: str) -> tuple[str, ExtractionKind, bool] | None:
    parsed = urlsplit(raw_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    path = unquote(parsed.path).casefold()
    for suffix, platform, kind, requires_auth in PLATFORM_POLICIES:
        if host != suffix and not host.endswith(f".{suffix}"):
            continue
        if platform == "instagram" and not _instagram_profile_url(raw_url):
            continue
        if platform == "facebook" and not _facebook_profile_url(raw_url):
            continue
        if platform == "tiktok" and not _tiktok_profile_url(raw_url):
            continue
        if platform == "pinterest" and not re.fullmatch(r"/[A-Za-z0-9_]{3,30}/?", unquote(parsed.path)):
            continue
        if platform == "threads" and not re.fullmatch(r"/@[A-Za-z0-9._]{1,30}/?", unquote(parsed.path)):
            continue
        if platform == "snapchat" and not re.fullmatch(r"/(?:add|@)/?[A-Za-z0-9._-]+/?", unquote(parsed.path)):
            continue
        if platform == "telegram" and not re.fullmatch(r"/[A-Za-z][A-Za-z0-9_]{4,31}/?", parsed.path):
            continue
        if platform == "discord" and not re.fullmatch(r"/users/\d{17,20}/?", parsed.path):
            continue
        if platform == "whatsapp" and parsed.path not in {"/", "/send"}:
            continue
        if kind in {"location_history", "search_results"} and not any(
            marker in path for marker in PLATFORM_PATH_MARKERS.get(platform, ())
        ):
            continue
        return platform, kind, requires_auth
    return None


async def _extract_structured_page_protocol(page: Page, platform: str, kind: str) -> dict[str, Any]:
    """CSP-safe extraction using only Playwright DOM protocol operations."""
    meta: dict[str, str] = {}
    meta_nodes = page.locator("meta[name],meta[property]")
    for index in range(min(await meta_nodes.count(), 80)):
        node = meta_nodes.nth(index)
        key = await node.get_attribute("property") or await node.get_attribute("name")
        value = await node.get_attribute("content")
        if key and value:
            meta[key] = value.strip()[:4000]
    headings: list[str] = []
    heading_nodes = page.locator("h1,h2,h3")
    for index in range(min(await heading_nodes.count(), 50)):
        text = ((await heading_nodes.nth(index).text_content()) or "").strip()[:500]
        if text:
            headings.append(text)
    links: list[dict[str, str | None]] = []
    search_results: list[dict[str, Any]] = []
    seen_results: set[str] = set()
    source_host = (urlsplit(page.url).hostname or "").lower()
    anchor_nodes = page.locator("a[href]")
    for index in range(min(await anchor_nodes.count(), 200)):
        node = anchor_nodes.nth(index)
        raw_href = await node.get_attribute("href")
        if not raw_href:
            continue
        href = urljoin(page.url, raw_href)[:2048]
        if not href.startswith("http"):
            continue
        text = ((await node.text_content()) or "").strip()[:300] or None
        links.append({"href": href, "text": text})
        target_host = (urlsplit(href).hostname or "").lower()
        if kind == "search_results" and text and href not in seen_results and target_host != source_host:
            seen_results.add(href)
            search_results.append({"href": href, "title": text[:500], "snippet": None, "rank": len(search_results) + 1})
            if len(search_results) >= 50:
                break
    canonical = None
    canonical_node = page.locator('link[rel="canonical"]').first
    if await canonical_node.count():
        canonical = await canonical_node.get_attribute("href")
    try:
        visible_text = (await page.locator("body").inner_text())[:120000]
    except PlaywrightError:
        visible_text = ""
    return {
        "schema_version": 1,
        "platform": platform,
        "kind": kind,
        "url": page.url,
        "title": (await page.title())[:500] or None,
        "canonical_url": urljoin(page.url, canonical) if canonical else None,
        "meta": meta,
        "jsonld": [],
        "embedded_state": {},
        "headings": headings,
        "links": links,
        "search_results": search_results,
        "visible_text": visible_text,
        "extraction_fallback": "protocol",
    }


async def extract_structured_page(page: Page, platform: str, kind: str) -> dict[str, Any]:
    try:
        payload = await page.evaluate(
        """
        ({platform, kind}) => {
          const text = (value, limit = 2000) => typeof value === 'string' ? value.trim().slice(0, limit) : null;
          const meta = {};
          for (const node of document.querySelectorAll('meta[name],meta[property]')) {
            const key = node.getAttribute('property') || node.getAttribute('name');
            const value = node.getAttribute('content');
            if (key && value && Object.keys(meta).length < 80) meta[key] = text(value, 4000);
          }
          const jsonld = [];
          for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
            if (jsonld.length >= 25) break;
            try {
              const value = JSON.parse(node.textContent || 'null');
              jsonld.push(value);
            } catch (_) {}
          }
          const embedded_state = {};
          for (const id of ['__NEXT_DATA__', '__UNIVERSAL_DATA_FOR_REHYDRATION__', '__DEFAULT_SCOPE__']) {
            const node = document.getElementById(id);
            const raw = node?.textContent || '';
            if (!raw || raw.length > 750000) continue;
            try { embedded_state[id] = JSON.parse(raw); } catch (_) {}
          }
          const links = [];
          for (const node of document.querySelectorAll('a[href]')) {
            if (links.length >= 200) break;
            const href = node.href;
            if (!href || !href.startsWith('http')) continue;
            links.push({href: href.slice(0, 2048), text: text(node.innerText || node.textContent || '', 300)});
          }
          const search_results = [];
          if (kind === 'search_results') {
            const seen = new Set();
            const addResult = (anchor, snippetNode) => {
              if (!anchor || search_results.length >= 50) return;
              const href = anchor.href || '';
              if (!href.startsWith('http') || seen.has(href)) return;
              const titleNode = anchor.querySelector('h3, h2, .title, .result-title') || anchor;
              const title = text(titleNode.innerText || titleNode.textContent || '', 500);
              if (!title) return;
              seen.add(href);
              search_results.push({
                href: href.slice(0, 2048),
                title,
                snippet: text(snippetNode?.innerText || snippetNode?.textContent || '', 1200),
                rank: search_results.length + 1,
              });
            };
            if (platform === 'google_search') {
              for (const card of document.querySelectorAll('div.MjjYud, div.tF2Cxc')) {
                const heading = card.querySelector('a[href] h3');
                addResult(heading?.closest('a[href]'), card.querySelector('.VwiC3b, .yXK7lf, [data-sncf]'));
              }
            } else if (platform === 'bing_search') {
              for (const card of document.querySelectorAll('#b_results > li.b_algo')) {
                addResult(card.querySelector('h2 a[href]'), card.querySelector('.b_caption p, .b_snippet'));
              }
            } else if (platform === 'mojeek_search') {
              for (const card of document.querySelectorAll('.result, .results-standard .result')) {
                addResult(card.querySelector('a.title, h2 a[href], h3 a[href]'), card.querySelector('.s, .snippet, p'));
              }
            } else if (platform === 'duckduckgo_search') {
              for (const card of document.querySelectorAll('article[data-testid="result"], li[data-layout="organic"] article, .result.results_links')) {
                addResult(
                  card.querySelector('a[data-testid="result-title-a"], h2 a[href], a.result__a'),
                  card.querySelector('[data-result="snippet"], .result__snippet'));
              }
            } else if (platform === 'brave_search') {
              for (const card of document.querySelectorAll('#results .snippet[data-type="web"], #results div.snippet[data-pos]')) {
                const titleNode = card.querySelector('.title, .heading-serpresult, h2, h3');
                addResult(
                  titleNode?.closest('a[href]') || card.querySelector('a[href]'),
                  card.querySelector('.snippet-description, .snippet-content, .generic-snippet'));
              }
            } else if (platform === 'startpage_search') {
              for (const card of document.querySelectorAll('.w-gl .result, .w-gl__result, section.w-gl > div')) {
                addResult(
                  card.querySelector('a.result-title, a.result-link, a.w-gl__result-title, h2 a[href]'),
                  card.querySelector('p.description, .w-gl__description, p'));
              }
            }
            if (search_results.length === 0) {
              // No engine-specific card matched (markup changes often). Fall back on structure:
              // a heading that links to another site is a result title, and the text of the
              // block around it, minus the title, is its snippet.
              const pageHost = location.hostname.replace(/^www\\./, '');
              for (const heading of document.querySelectorAll('h2, h3')) {
                if (search_results.length >= 50) break;
                const anchor = heading.closest('a[href]') || heading.querySelector('a[href]');
                const href = anchor?.href || '';
                if (!href.startsWith('http') || seen.has(href)) continue;
                let host = '';
                try { host = new URL(href).hostname.replace(/^www\\./, ''); } catch (_) { continue; }
                if (!host || host === pageHost || host.endsWith('.' + pageHost)) continue;
                const title = text(heading.innerText || heading.textContent || '', 500);
                if (!title) continue;
                let card = heading.closest('article, li, [data-testid="result"], .result, .snippet');
                if (!card) {
                  card = heading.parentElement;
                  // Widen to the enclosing block, but never into one that holds another result.
                  for (let depth = 0; depth < 3 && card?.parentElement
                       && (card.innerText || '').length < title.length + 60
                       && card.parentElement.querySelectorAll('h2, h3').length <= 1; depth += 1) {
                    card = card.parentElement;
                  }
                }
                const around = text(card?.innerText || '', 2000);
                const snippet = around.length <= 1800 ? text(around.split(title).join(' '), 1200) : '';
                seen.add(href);
                search_results.push({href: href.slice(0, 2048), title, snippet, rank: search_results.length + 1});
              }
            }
          }
          const headings = Array.from(document.querySelectorAll('h1,h2,h3')).slice(0, 50)
            .map(node => text(node.innerText || node.textContent || '', 500)).filter(Boolean);
          return {
            schema_version: 1,
            platform,
            kind,
            url: location.href,
            title: text(document.title, 500),
            canonical_url: document.querySelector('link[rel="canonical"]')?.href || null,
            meta,
            jsonld,
            embedded_state,
            headings,
            links,
            search_results,
            visible_text: text(document.body?.innerText || '', 120000),
          };
        }
        """,
            {"platform": platform, "kind": kind},
        )
    except PlaywrightError:
        payload = await _extract_structured_page_protocol(page, platform, kind)
    payload = payload if isinstance(payload, dict) else {}
    if kind == "social_profile":
        try:
            payload["profile_objects"] = await collect_profile_objects(page) if platform not in {"whatsapp", "discord"} else []
            if platform in {"whatsapp", "discord"}:
                payload["visible_profile"] = await visible_contact_profile(page, platform)
                # Conversation text is not profile evidence.
                payload["visible_text"] = None
                payload["links"] = []
                payload["embedded_state"] = {}
                payload["jsonld"] = []
                payload["profile_objects"] = []
        except PlaywrightError:
            payload["profile_extraction_state"] = "unavailable"
    return payload


async def capture_current_page(job: PendingJob) -> CaptureResult:
    if _context is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "browser is not ready")
    validate_public_target(job.page.url)
    observed_at = datetime.now(UTC)
    challenge_type = await detect_challenge(job.page)
    page_title = (await job.page.title())[:500] or None
    if challenge_type:
        job.challenges.append({"type": challenge_type, "observed_at": observed_at.isoformat()})
        screenshot_name = f"challenge-{len(job.challenges)}.png"
    else:
        screenshot_name = "page.png"
    screenshot_path = job.artifact_dir / screenshot_name
    try:
        await job.page.screenshot(path=str(screenshot_path), full_page=True)
    except PlaywrightError:
        await job.page.screenshot(path=str(screenshot_path), full_page=False)
    screenshot_sha256 = sha256_file(screenshot_path)

    html_path: Path | None = None
    html_sha256: str | None = None
    extracted: dict[str, Any] | None = None
    extraction_kind: ExtractionKind | None = None
    extraction_platform: str | None = None
    authenticated_session: bool | None = None
    policy = platform_policy(job.page.url)
    if challenge_type is None:
        encoded = (await job.page.content()).encode("utf-8")
        if len(encoded) <= MAX_HTML_BYTES:
            html_path = job.artifact_dir / "page.html"
            html_path.write_bytes(encoded)
            html_sha256 = hashlib.sha256(encoded).hexdigest()
        if policy is not None:
            extraction_platform, extraction_kind, _ = policy
            if extraction_platform in {"instagram", "facebook", "tiktok"}:
                active_sessions = authenticated_session_names(await _context.cookies())
                authenticated_session = extraction_platform in active_sessions
            extracted = await extract_structured_page(
                job.page, extraction_platform, extraction_kind
            )
            extraction_path = job.artifact_dir / "extracted.json"
            extraction_path.write_text(
                json.dumps(extracted, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )

    run_status: Literal["completed", "manual_action_required"] = (
        "manual_action_required" if challenge_type else "completed"
    )
    root = relative_root(job)
    manifest = {
        "schema_version": 1,
        "job_id": job.request.job_id,
        "investigation_id": job.request.investigation_id,
        "collector_id": job.request.collector_id,
        "status": run_status,
        "requested_url": job.request.url,
        "final_url": job.page.url,
        "observed_at": observed_at.isoformat(),
        "page_title": page_title,
        "challenge_type": challenge_type,
        "challenge_history": job.challenges,
        "browser_version": _context.browser.version,
        "browser_engine": _active_browser_engine,
        "authenticated_session": authenticated_session,
        "artifacts": {
            "screenshot": {"path": f"{root}/{screenshot_name}", "sha256": screenshot_sha256},
            "html": (
                {"path": f"{root}/page.html", "sha256": html_sha256}
                if html_path is not None
                else None
            ),
            "extracted": ({"path": f"{root}/extracted.json"} if extracted is not None else None),
        },
    }
    manifest_path = job.artifact_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    manifest_sha256 = sha256_file(manifest_path)
    return CaptureResult(
        job_id=job.request.job_id,
        status=run_status,
        requested_url=job.request.url,
        final_url=job.page.url,
        observed_at=observed_at,
        page_title=page_title,
        challenge_type=challenge_type,
        screenshot_reference=f"{root}/{screenshot_name}",
        html_reference=f"{root}/page.html" if html_path is not None else None,
        manifest_reference=f"{root}/manifest.json",
        screenshot_sha256=screenshot_sha256,
        html_sha256=html_sha256,
        manifest_sha256=manifest_sha256,
        browser_version=_context.browser.version,
        browser_engine=_active_browser_engine,
        extraction_kind=extraction_kind,
        extraction_platform=extraction_platform,
        authenticated_session=authenticated_session,
        extracted=extracted,
    )


def _async_playwright_factory() -> Any:
    """Select the Playwright driver entrypoint, honouring STEALTH_BROWSER_ENABLED.

    Patchright (github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) is a drop-in-API-compatible
    fork of Playwright's async client with CDP-level patches that reduce the chance Google/Bing
    flag the session as automated - it never touches a challenge once one is shown, only how
    often one appears. The driver is chosen once, at process startup; toggling the setting
    requires restarting this service, not a per-request decision. See THIRD_PARTY_NOTICES.md.
    """
    if not STEALTH_BROWSER_ENABLED:
        return async_playwright
    from patchright.async_api import (  # type: ignore[import-not-found]
        async_playwright as stealth_async_playwright,
    )

    return stealth_async_playwright


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _playwright, _context, _pending_job, _profile_lock_fd
    if len(AGENT_TOKEN) < 32:
        raise RuntimeError("BROWSER_AGENT_TOKEN must contain at least 32 characters")
    PROFILE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    EVIDENCE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    _profile_lock_fd = acquire_profile_lock()
    try:
        if _active_browser_engine == "chromium":
            _playwright = await _async_playwright_factory()().start()
        await activate_profile(DEFAULT_PROFILE_NAME)
        yield
    finally:
        if _pending_job is not None:
            try:
                await _pending_job.page.close()
            except PlaywrightError:
                pass
            _pending_job = None
        await _close_active_context()
        if _playwright is not None:
            try:
                await _playwright.stop()
            except PlaywrightError:
                pass
        _context = None
        _playwright = None
        if _profile_lock_fd is not None:
            release_profile_lock(_profile_lock_fd)
            _profile_lock_fd = None


app = FastAPI(
    title="OSINT Browser Agent",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.get("/healthz")
async def health(_: None = Depends(require_token)) -> dict[str, object]:
    authenticated_sessions: tuple[str, ...] = ()
    if _context is not None:
        authenticated_sessions = authenticated_session_names(await _context.cookies())
    return {
        "status": "ok" if _context is not None else "starting",
        "profile_ready": _context is not None,
        "allowed_hosts": list(ALLOWED_HOSTS),
        "browser_version": _context.browser.version if _context is not None and _context.browser is not None else None,
        "browser_engine": _active_browser_engine,
        "stealth_enabled": STEALTH_BROWSER_ENABLED if _active_browser_engine == "chromium" else True,
        "active_job": _job_lock.locked() or _pending_job is not None,
        "pending_job_id": _pending_job.request.job_id if _pending_job is not None else None,
        "authenticated_sessions": list(authenticated_sessions),
        "active_profile": _active_profile_name,
        "profiles": list(list_profile_names()),
    }


@app.post("/v1/engine/switch")
async def switch_engine(
    payload: EngineSwitchRequest, _: None = Depends(require_token)
) -> dict[str, object]:
    try:
        await switch_browser_engine(payload.engine)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {
        "status": "active",
        "browser_engine": _active_browser_engine,
        "active_profile": _active_profile_name,
    }


@app.post("/v1/profiles/switch")
async def switch_profile(
    payload: ProfileSwitchRequest, _: None = Depends(require_token)
) -> dict[str, object]:
    try:
        await activate_profile(payload.name)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"status": "active", "active_profile": _active_profile_name, "profiles": list(list_profile_names())}


@app.post("/v1/profiles/{profile_name}/backup")
async def backup_profile(
    profile_name: str,
    x_corvus_backup_password: str = Header(default=""),
    _: None = Depends(require_token),
) -> Response:
    global _context
    try:
        canonical = validate_profile_name(profile_name)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if _job_lock.locked() or _pending_job is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "cannot back up a browser identity during capture")
    was_active = canonical == _active_profile_name and _context is not None
    try:
        if was_active and _context is not None:
            await _context.close()
            _context = None
        content = await asyncio.to_thread(
            encrypt_profile_backup, canonical, x_corvus_backup_password
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    finally:
        if was_active:
            await activate_profile(canonical)
    return Response(
        content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="corvus-browser-{canonical}.cbb"',
            "Cache-Control": "no-store",
            "X-Content-SHA256": hashlib.sha256(content).hexdigest(),
        },
    )


@app.put("/v1/profiles/{profile_name}/restore")
async def restore_profile(
    profile_name: str,
    request: FastAPIRequest,
    x_corvus_backup_password: str = Header(default=""),
    _: None = Depends(require_token),
) -> dict[str, object]:
    global _context
    try:
        canonical = validate_profile_name(profile_name)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if _job_lock.locked() or _pending_job is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "cannot restore a browser identity during capture")
    content = await request.body()
    if len(content) > MAX_BACKUP_BYTES + 16 * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "browser profile backup is too large")
    was_active = canonical == _active_profile_name and _context is not None
    try:
        payload = await asyncio.to_thread(
            decrypt_profile_backup, canonical, x_corvus_backup_password, content
        )
        if was_active and _context is not None:
            await _context.close()
            _context = None
        await asyncio.to_thread(restore_profile_archive, canonical, payload)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    finally:
        if was_active:
            await activate_profile(canonical)
    return {
        "status": "restored",
        "profile": canonical,
        "active_profile": _active_profile_name,
        "profiles": list(list_profile_names()),
    }


@app.post("/v1/sessions/export", response_model=SessionExportResult)
async def export_session(
    payload: SessionExportRequest, _: None = Depends(require_token)
) -> SessionExportResult:
    if _context is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "browser is not ready")
    scope_hosts = SESSION_EXPORT_HOSTS[payload.collector_id]
    raw_cookies = await _context.cookies()
    cookies = filter_session_cookies(raw_cookies, scope_hosts)
    active_sessions = authenticated_session_names(cookies)
    authenticated = (
        "instagram" in active_sessions
        if payload.collector_id == "instaloader"
        else bool(active_sessions)
    )
    user_agent, accept_language = await browser_session_identity(_context)
    return SessionExportResult(
        collector_id=payload.collector_id,
        exported_at=datetime.now(UTC),
        scope_hosts=scope_hosts,
        authenticated=authenticated,
        user_agent=user_agent,
        accept_language=accept_language,
        cookies=cookies,
    )


@app.delete("/v1/evidence/{investigation_id}", dependencies=[Depends(require_token)])
async def delete_investigation_evidence(investigation_id: str) -> dict[str, int | str]:
    try:
        canonical_id = str(UUID(investigation_id))
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid investigation id"
        ) from exc
    if canonical_id != investigation_id.lower():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid investigation id")
    if _pending_job and _pending_job.request.investigation_id == investigation_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "investigation capture is still active")
    root = EVIDENCE_DIR.resolve()
    destination = (root / investigation_id).resolve()
    if root not in destination.parents:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid evidence path")
    removed_files = 0
    if destination.exists():
        removed_files = sum(1 for item in destination.rglob("*") if item.is_file())
        shutil.rmtree(destination)
    return {
        "status": "deleted",
        "investigation_id": investigation_id,
        "removed_files": removed_files,
    }


@app.post("/v1/captures", response_model=CaptureResult)
async def capture(payload: CaptureRequest, _auth: None = Depends(require_token)) -> CaptureResult:
    global _pending_job
    target = validate_public_target(payload.url)
    if _context is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "browser is not ready")
    if _job_lock.locked() or _pending_job is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "browser is already processing a job")
    policy = platform_policy(target)
    if policy is not None:
        platform, _kind, requires_auth = policy
        if requires_auth:
            active = authenticated_session_names(await _context.cookies())
            if platform not in active:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"an authenticated {platform} browser session is required",
                )
    async with _job_lock:
        artifact_dir = safe_artifact_dir(payload.investigation_id, payload.job_id)
        is_visible_search = payload.collector_id in {
            "browser_google_search",
            "browser_bing_search",
            "browser_mojeek_search",
        }
        page = (
            _context.pages[0]
            if is_visible_search and _context.pages
            else await _context.new_page()
        )
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        job = PendingJob(request=payload, page=page, artifact_dir=artifact_dir)
        try:
            if is_visible_search:
                await page.bring_to_front()
                env = dict(os.environ)
                env["DISPLAY"] = ":99"
                for command in (
                    ("key", "--clearmodifiers", "ctrl+l"),
                    ("type", "--clearmodifiers", "--delay", "1", target),
                    ("key", "--clearmodifiers", "Return"),
                ):
                    process = await asyncio.create_subprocess_exec(  # noqa: S603
                        "/usr/bin/xdotool",
                        *command,
                        env=env,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                        raise HTTPException(
                            status.HTTP_503_SERVICE_UNAVAILABLE,
                            "visible browser macro timed out",
                        ) from None
                    if process.returncode != 0:
                        raise HTTPException(
                            status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"visible browser macro failed: {stderr.decode(errors='replace')[:200]}",
                        )
                await page.wait_for_timeout(1500)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
                except PlaywrightError:
                    pass
            else:
                await page.goto(target, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)
            await wait_for_automatic_browser_check(page)
            result = await capture_current_page(job)
            if result.status == "manual_action_required":
                _pending_job = job
            elif not is_visible_search:
                await page.close()
            return result
        except Exception:
            if not is_visible_search:
                await page.close()
            raise


@app.post("/v1/captures/abandon")
async def abandon(payload: ResumeRequest, _: None = Depends(require_token)) -> dict[str, str]:
    global _pending_job
    if _job_lock.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, "browser is already processing a job")
    if (
        _pending_job is None
        or _pending_job.request.job_id != payload.job_id
        or _pending_job.request.investigation_id != payload.investigation_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pending browser job was not found")
    job = _pending_job
    visible_search = job.request.collector_id in {
        "browser_google_search",
        "browser_bing_search",
        "browser_mojeek_search",
    }
    try:
        if visible_search:
            await job.page.goto("about:blank", wait_until="domcontentloaded")
        else:
            await job.page.close()
    except PlaywrightError:
        pass
    _pending_job = None
    return {
        "status": "abandoned",
        "job_id": payload.job_id,
        "investigation_id": payload.investigation_id,
    }


@app.post("/v1/captures/resume", response_model=CaptureResult)
async def resume(payload: ResumeRequest, _: None = Depends(require_token)) -> CaptureResult:
    global _pending_job
    if _job_lock.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, "browser is already processing a job")
    if (
        _pending_job is None
        or _pending_job.request.job_id != payload.job_id
        or _pending_job.request.investigation_id != payload.investigation_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pending browser job was not found")
    async with _job_lock:
        job = _pending_job
        result = await capture_current_page(job)
        if result.status == "completed":
            if job.request.collector_id not in {
                "browser_google_search",
                "browser_bing_search",
                "browser_mojeek_search",
            }:
                await job.page.close()
            _pending_job = None
        return result
