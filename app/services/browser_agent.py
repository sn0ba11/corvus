from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from app.core.config import BUILT_IN_BROWSER_ALLOWED_HOSTS, Settings


class BrowserAgentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BrowserAgentStatus:
    available: bool
    enabled: bool
    profile_ready: bool
    active_job: bool
    pending_job_id: str | None
    browser_version: str | None
    allowed_hosts: tuple[str, ...]
    authenticated_sessions: tuple[str, ...]
    browser_engine: Literal["chromium", "firefox"] = "chromium"
    stealth_enabled: bool = False
    active_profile: str = "sock-default"
    profiles: tuple[str, ...] = ("sock-default",)
    error: str | None = None


@dataclass(frozen=True)
class BrowserSessionCookie:
    name: str
    value: str
    domain: str
    path: str
    expires: float
    secure: bool
    http_only: bool

    def runner_payload(self) -> dict[str, str | float | bool]:
        return {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path,
            "expires": self.expires,
            "secure": self.secure,
            "http_only": self.http_only,
        }


@dataclass(frozen=True)
class BrowserSessionBundle:
    collector_id: str
    exported_at: datetime
    scope_hosts: tuple[str, ...]
    authenticated: bool
    user_agent: str
    accept_language: str
    cookies: tuple[BrowserSessionCookie, ...]


@dataclass(frozen=True)
class BrowserCaptureResult:
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
    extraction_kind: Literal["social_profile", "location_history", "search_results"] | None = None
    extraction_platform: str | None = None
    authenticated_session: bool | None = None
    extracted: dict[str, Any] | None = None


def configured_allowed_hosts(settings: Settings) -> tuple[str, ...]:
    # The built-in hosts always apply; a configured value can only add to them.
    configured = (
        f"{BUILT_IN_BROWSER_ALLOWED_HOSTS},{settings.browser_allowed_hosts},"
        f"{settings.browser_maigret_session_hosts}"
    )
    return tuple(
        dict.fromkeys(
            item.strip().lower().lstrip(".") for item in configured.split(",") if item.strip()
        )
    )


def host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    normalised = host.lower().rstrip(".")
    return any(normalised == item or normalised.endswith(f".{item}") for item in allowed_hosts)


def validate_browser_target(
    raw_url: str,
    allowed_hosts: tuple[str, ...],
    *,
    resolver: Any = socket.getaddrinfo,
) -> str:
    parsed = urlsplit(raw_url.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise BrowserAgentError("invalid_target", "target must be a public HTTPS URL")
    if not host_allowed(parsed.hostname, allowed_hosts):
        raise BrowserAgentError("target_not_allowed", "target host is not allowlisted")
    try:
        answers = resolver(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BrowserAgentError("target_unresolved", "target host did not resolve") from exc
    addresses = {answer[4][0].split("%", 1)[0] for answer in answers}
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise BrowserAgentError("unsafe_target", "target resolved to a non-public address")
    return raw_url.strip()


def resolve_evidence_path(reference: str, evidence_root: Path) -> Path:
    if not reference or Path(reference).is_absolute():
        raise BrowserAgentError("invalid_evidence_reference", "invalid browser evidence reference")
    resolved_root = evidence_root.resolve()
    candidate = (resolved_root / reference).resolve()
    if resolved_root not in candidate.parents:
        raise BrowserAgentError("invalid_evidence_reference", "invalid browser evidence reference")
    return candidate


class BrowserAgentClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.transport = transport

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.browser_agent_token}"}

    async def status(self) -> BrowserAgentStatus:
        allowed_hosts = configured_allowed_hosts(self.settings)
        if not self.settings.browser_enabled:
            return BrowserAgentStatus(
                available=False,
                enabled=False,
                profile_ready=False,
                active_job=False,
                pending_job_id=None,
                browser_version=None,
                allowed_hosts=allowed_hosts,
                authenticated_sessions=(),
            )
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=5.0,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    f"{self.settings.browser_agent_url.rstrip('/')}/healthz", headers=self.headers
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return BrowserAgentStatus(
                available=False,
                enabled=True,
                profile_ready=False,
                active_job=False,
                pending_job_id=None,
                browser_version=None,
                allowed_hosts=allowed_hosts,
                authenticated_sessions=(),
                error=str(exc)[:300],
            )
        return BrowserAgentStatus(
            available=payload.get("status") == "ok",
            enabled=True,
            profile_ready=bool(payload.get("profile_ready")),
            active_job=bool(payload.get("active_job")),
            pending_job_id=str(payload.get("pending_job_id") or "") or None,
            browser_version=str(payload.get("browser_version") or "") or None,
            allowed_hosts=tuple(str(item) for item in payload.get("allowed_hosts", allowed_hosts)),
            authenticated_sessions=tuple(
                str(item) for item in payload.get("authenticated_sessions", []) if item
            ),
            browser_engine=(
                "firefox" if str(payload.get("browser_engine") or "chromium") == "firefox" else "chromium"
            ),
            stealth_enabled=bool(payload.get("stealth_enabled")),
            active_profile=str(payload.get("active_profile") or "sock-default"),
            profiles=tuple(str(item) for item in payload.get("profiles", ["sock-default"]) if item),
        )

    async def switch_engine(self, engine: Literal["chromium", "firefox"]) -> str:
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=60.0,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    f"{self.settings.browser_agent_url.rstrip('/')}/v1/engine/switch",
                    headers=self.headers,
                    json={"engine": engine},
                )
                if response.status_code in {409, 422}:
                    detail = response.json().get("detail", "browser engine switch rejected")
                    raise BrowserAgentError("engine_switch_rejected", str(detail)[:300])
                response.raise_for_status()
                body = response.json()
            active = str(body.get("browser_engine") or "")
            if active not in {"chromium", "firefox"}:
                raise ValueError("missing active browser engine")
            return active
        except BrowserAgentError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise BrowserAgentError("engine_switch_failed", "browser engine switch failed") from exc

    async def switch_profile(self, profile_name: str) -> str:
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=30.0,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    f"{self.settings.browser_agent_url.rstrip('/')}/v1/profiles/switch",
                    headers=self.headers,
                    json={"name": profile_name},
                )
                if response.status_code in {409, 422}:
                    detail = response.json().get("detail", "browser identity switch rejected")
                    raise BrowserAgentError("profile_switch_rejected", str(detail)[:300])
                response.raise_for_status()
                body = response.json()
            active = str(body.get("active_profile") or "")
            if not active:
                raise ValueError("missing active browser profile")
            return active
        except BrowserAgentError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise BrowserAgentError("profile_switch_failed", "browser identity switch failed") from exc

    async def backup_profile(self, profile_name: str, password: str) -> tuple[bytes, str]:
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=120.0,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    f"{self.settings.browser_agent_url.rstrip('/')}/v1/profiles/{profile_name}/backup",
                    headers={**self.headers, "X-Corvus-Backup-Password": password},
                )
                if response.status_code in {409, 422}:
                    detail = response.json().get("detail", "browser identity backup rejected")
                    raise BrowserAgentError("profile_backup_rejected", str(detail)[:300])
                response.raise_for_status()
            content = response.content
            digest = str(response.headers.get("X-Content-SHA256") or "")
            if not content or len(digest) != 64:
                raise ValueError("invalid browser identity backup response")
            return content, digest
        except BrowserAgentError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise BrowserAgentError("profile_backup_failed", "browser identity backup failed") from exc

    async def restore_profile(self, profile_name: str, password: str, content: bytes) -> str:
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=120.0,
                follow_redirects=False,
            ) as client:
                response = await client.put(
                    f"{self.settings.browser_agent_url.rstrip('/')}/v1/profiles/{profile_name}/restore",
                    headers={
                        **self.headers,
                        "X-Corvus-Backup-Password": password,
                        "Content-Type": "application/octet-stream",
                    },
                    content=content,
                )
                if response.status_code in {409, 413, 422}:
                    detail = response.json().get("detail", "browser identity restore rejected")
                    raise BrowserAgentError("profile_restore_rejected", str(detail)[:300])
                response.raise_for_status()
                body = response.json()
            restored = str(body.get("profile") or "")
            if not restored:
                raise ValueError("missing restored browser profile")
            return restored
        except BrowserAgentError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise BrowserAgentError("profile_restore_failed", "browser identity restore failed") from exc

    async def export_session(self, collector_id: str) -> BrowserSessionBundle:
        if not self.settings.browser_enabled:
            raise BrowserAgentError("browser_disabled", "managed browser support is disabled")
        if collector_id not in {"instaloader", "gallery_dl", "maigret"}:
            raise BrowserAgentError(
                "session_not_supported", "collector does not accept browser sessions"
            )
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=10.0,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    f"{self.settings.browser_agent_url.rstrip('/')}/v1/sessions/export",
                    headers=self.headers,
                    json={"collector_id": collector_id},
                )
                if response.status_code == 422:
                    raise BrowserAgentError(
                        "session_not_supported", "browser agent rejected the session scope"
                    )
                response.raise_for_status()
                body = response.json()
            raw_cookies = body.get("cookies", [])
            authenticated = body.get("authenticated")
            user_agent = " ".join(str(body.get("user_agent") or "").split())
            accept_language = " ".join(str(body.get("accept_language") or "").split())
            if (
                body.get("collector_id") != collector_id
                or not isinstance(raw_cookies, list)
                or len(raw_cookies) > 100
                or not isinstance(authenticated, bool)
                or not user_agent
                or len(user_agent) > 512
                or not accept_language
                or len(accept_language) > 256
            ):
                raise BrowserAgentError(
                    "session_export_failed", "managed browser returned an invalid session bundle"
                )
            cookies = tuple(
                BrowserSessionCookie(
                    name=str(item["name"]),
                    value=str(item["value"]),
                    domain=str(item["domain"]),
                    path=str(item.get("path") or "/"),
                    expires=float(item.get("expires", -1)),
                    secure=bool(item.get("secure")),
                    http_only=bool(item.get("http_only")),
                )
                for item in raw_cookies
                if isinstance(item, dict)
            )
            return BrowserSessionBundle(
                collector_id=str(body["collector_id"]),
                exported_at=datetime.fromisoformat(body["exported_at"]),
                scope_hosts=tuple(str(host) for host in body.get("scope_hosts", [])),
                authenticated=authenticated,
                user_agent=user_agent,
                accept_language=accept_language,
                cookies=cookies,
            )
        except BrowserAgentError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise BrowserAgentError(
                "browser_unavailable", "managed browser is unavailable"
            ) from exc
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise BrowserAgentError(
                "session_export_failed", "managed browser session export failed"
            ) from exc

    async def delete_investigation_evidence(self, investigation_id: str) -> int:
        if not self.settings.browser_enabled:
            return 0
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=15.0,
                follow_redirects=False,
            ) as client:
                response = await client.delete(
                    f"{self.settings.browser_agent_url.rstrip('/')}/v1/evidence/{investigation_id}",
                    headers=self.headers,
                )
                if response.status_code == 409:
                    raise BrowserAgentError(
                        "browser_busy", "managed browser evidence is still being captured"
                    )
                if response.status_code == 422:
                    raise BrowserAgentError(
                        "evidence_delete_rejected", "browser agent rejected the investigation id"
                    )
                response.raise_for_status()
                body = response.json()
            if body.get("status") != "deleted" or body.get("investigation_id") != investigation_id:
                raise BrowserAgentError(
                    "evidence_delete_failed", "managed browser returned an invalid deletion result"
                )
            removed_files = body.get("removed_files", 0)
            if not isinstance(removed_files, int) or removed_files < 0:
                raise BrowserAgentError(
                    "evidence_delete_failed", "managed browser returned an invalid deletion count"
                )
            return removed_files
        except BrowserAgentError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise BrowserAgentError(
                "browser_unavailable", "managed browser is unavailable"
            ) from exc
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise BrowserAgentError(
                "evidence_delete_failed", "managed browser evidence deletion failed"
            ) from exc

    async def _capture_request(self, path: str, payload: dict[str, str]) -> BrowserCaptureResult:
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.settings.browser_request_timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    f"{self.settings.browser_agent_url.rstrip('/')}{path}",
                    headers=self.headers,
                    json=payload,
                )
                if response.status_code == 409:
                    detail = ""
                    try:
                        detail = str(response.json().get("detail") or "")
                    except (ValueError, AttributeError):
                        pass
                    if "authenticated" in detail.casefold() and "session" in detail.casefold():
                        raise BrowserAgentError("session_required", detail[:300])
                    raise BrowserAgentError(
                        "browser_busy", "managed browser is processing another job"
                    )
                if response.status_code == 404:
                    raise BrowserAgentError(
                        "pending_job_missing", "pending browser job was not found"
                    )
                if response.status_code == 422:
                    raise BrowserAgentError("target_rejected", "browser agent rejected the target")
                response.raise_for_status()
                body = response.json()
        except BrowserAgentError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise BrowserAgentError(
                "browser_unavailable", "managed browser is unavailable"
            ) from exc
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise BrowserAgentError("browser_failed", "managed browser capture failed") from exc
        return BrowserCaptureResult(
            status=body["status"],
            requested_url=body["requested_url"],
            final_url=body["final_url"],
            observed_at=datetime.fromisoformat(body["observed_at"]),
            page_title=body.get("page_title"),
            challenge_type=body.get("challenge_type"),
            screenshot_reference=body["screenshot_reference"],
            html_reference=body.get("html_reference"),
            manifest_reference=body["manifest_reference"],
            screenshot_sha256=body["screenshot_sha256"],
            html_sha256=body.get("html_sha256"),
            manifest_sha256=body["manifest_sha256"],
            browser_version=body["browser_version"],
            browser_engine=("firefox" if body.get("browser_engine") == "firefox" else "chromium"),
            extraction_kind=body.get("extraction_kind"),
            extraction_platform=body.get("extraction_platform"),
            authenticated_session=(
                bool(body["authenticated_session"])
                if isinstance(body.get("authenticated_session"), bool)
                else None
            ),
            extracted=body.get("extracted") if isinstance(body.get("extracted"), dict) else None,
        )

    async def capture(
        self,
        *,
        job_id: str,
        investigation_id: str,
        url: str,
        collector_id: str = "generic_capture",
    ) -> BrowserCaptureResult:
        if not self.settings.browser_enabled:
            raise BrowserAgentError("browser_disabled", "managed browser support is disabled")
        target = validate_browser_target(url, configured_allowed_hosts(self.settings))
        return await self._capture_request(
            "/v1/captures",
            {
                "job_id": job_id,
                "investigation_id": investigation_id,
                "url": target,
                "collector_id": collector_id,
            },
        )

    async def abandon(self, *, job_id: str, investigation_id: str) -> None:
        if not self.settings.browser_enabled:
            raise BrowserAgentError("browser_disabled", "managed browser support is disabled")
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=15.0,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    f"{self.settings.browser_agent_url.rstrip('/')}/v1/captures/abandon",
                    headers=self.headers,
                    json={"job_id": job_id, "investigation_id": investigation_id},
                )
                if response.status_code == 404:
                    raise BrowserAgentError("pending_job_missing", "pending browser job was not found")
                if response.status_code == 409:
                    raise BrowserAgentError("browser_busy", "managed browser is processing another job")
                if response.status_code == 422:
                    detail = "browser agent rejected the pending job"
                    try:
                        detail = str(response.json().get("detail") or detail)
                    except (ValueError, AttributeError):
                        pass
                    raise BrowserAgentError("target_rejected", detail[:300])
                response.raise_for_status()
                body = response.json()
            if body.get("status") != "abandoned" or body.get("job_id") != job_id:
                raise ValueError("invalid browser abandon response")
        except BrowserAgentError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise BrowserAgentError("browser_unavailable", "managed browser is unavailable") from exc
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise BrowserAgentError("browser_failed", "managed browser abandon failed") from exc

    async def resume(self, *, job_id: str, investigation_id: str) -> BrowserCaptureResult:
        if not self.settings.browser_enabled:
            raise BrowserAgentError("browser_disabled", "managed browser support is disabled")
        return await self._capture_request(
            "/v1/captures/resume",
            {"job_id": job_id, "investigation_id": investigation_id},
        )
