import base64
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import PlainTextResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from app.api.case_comparison_routes import router as case_comparison_router
from app.api.external_api_settings_routes import router as external_api_settings_router
from app.api.routes import router, templates
from app.api.steam_settings_routes import router as steam_settings_router
from app.api.urlscan_settings_routes import router as urlscan_settings_router
from app.core.config import get_settings
from app.core.security import password_matches
from app.db.session import SessionLocal, engine
from app.models.db import Base
from app.services.evidence_quality import reconcile_legacy_weak_claims, reconcile_platform_metadata
from app.services.external_api_settings import (
    get_companies_house_api_key,
    get_openphish_feed_url,
    set_companies_house_adapter_enabled,
    set_openphish_adapter_enabled,
)
from app.services.jobs import job_manager
from app.services.steam_settings import get_steam_api_key, set_steam_adapter_enabled
from app.services.urlscan_settings import get_urlscan_api_key, set_urlscan_adapter_enabled


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        defaults = {
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data: blob:; connect-src 'self'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Cache-Control": "no-store",
        }
        for name, value in defaults.items():
            if name not in response.headers:
                response.headers[name] = value
        return response


class LocalAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings = get_settings()
        if not settings.local_auth_enabled or request.url.path in {"/healthz", "/readyz"}:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        try:
            scheme, encoded = header.split(" ", 1)
            username, password = base64.b64decode(encoded).decode().split(":", 1)
        except (ValueError, UnicodeDecodeError):
            scheme, username, password = "", "", ""
        valid = (
            scheme.lower() == "basic"
            and hmac.compare_digest(username, settings.local_auth_username)
            and password_matches(password, settings.local_auth_password_hash)
        )
        if not valid:
            return PlainTextResponse(
                "authentication required",
                401,
                headers={"WWW-Authenticate": 'Basic realm="Corvus"'},
            )
        return await call_next(request)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        reconcile_legacy_weak_claims(db)
        reconcile_platform_metadata(db)
        set_steam_adapter_enabled(get_steam_api_key(db) is not None)
        set_urlscan_adapter_enabled(get_urlscan_api_key(db) is not None)
        set_companies_house_adapter_enabled(get_companies_house_api_key(db) is not None)
        set_openphish_adapter_enabled(get_openphish_feed_url(db) is not None)
    await job_manager.recover_stale()
    yield
    await job_manager.shutdown()


settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(LocalAuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="strict",
    https_only=settings.secure_cookies,
    max_age=28800,
)


@app.exception_handler(HTTPException)
async def browser_http_exception(request: Request, exc: HTTPException) -> Response:
    if "text/html" not in request.headers.get("accept", ""):
        return await http_exception_handler(request, exc)
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "request": request,
            "status_code": exc.status_code,
            "message": str(exc.detail),
        },
        status_code=exc.status_code,
    )


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(router)
app.include_router(case_comparison_router)
app.include_router(external_api_settings_router)
app.include_router(steam_settings_router)
app.include_router(urlscan_settings_router)
