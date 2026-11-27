import asyncio
import hashlib
import ipaddress
import json
import math
import os
import random
import re
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import FormData, UploadFile

from app.adapters import REGISTRY
from app.adapters.yt_dlp_metadata import YtDlpMetadataAdapter
from app.core.config import get_settings
from app.core.security import make_csrf_token, redact, verify_csrf
from app.core.upstreams import commit_for
from app.db.session import SessionLocal, get_db
from app.models.db import (
    ApplicationSetting,
    AuditEvent,
    BrowserRun,
    Finding,
    Investigation,
    LocationObservation,
    SearchTemplate,
    ToolRun,
)
from app.models.schemas import (
    LocationEvidenceType,
    LocationObservationInput,
    QueryType,
    SearchRequest,
)
from app.services import capture_run, case_identifiers, dork_run
from app.services.account_correlation import (
    AccountEvidence,
    correlate_accounts,
    extract_domains,
    extract_urls,
)
from app.services.auto_archiver_handoff import (
    AUTO_ARCHIVER_COMMIT,
    AUTO_ARCHIVER_VERSION,
    AutoArchiverCandidate,
    build_auto_archiver_bundle,
)
from app.services.browser_agent import (
    BrowserAgentClient,
    BrowserAgentError,
    BrowserCaptureResult,
    configured_allowed_hosts,
    resolve_evidence_path,
    validate_browser_target,
)
from app.services.collector_retry import (
    RetryConflict,
    can_retry,
    queue_deep_username_search,
    queue_retry,
    retry_targets,
)
from app.services.discord_intelligence import (
    MAX_EXPORT_BYTES as DISCORD_MAX_EXPORT_BYTES,
)
from app.services.discord_intelligence import (
    MAX_SQLITE_BYTES as DISCORD_MAX_SQLITE_BYTES,
)
from app.services.discord_intelligence import (
    DiscordImportError,
    parse_discord_chat_export,
    parse_discord_sqlite_export,
)
from app.services.discord_intelligence import (
    message_metadata as discord_message_metadata,
)
from app.services.egress_policy import (
    EgressPayloadKind,
    EgressPolicyError,
    validate_external_egress,
)
from app.services.evidence_bundle import BundleOptions, EvidenceBundleError, build_evidence_bundle
from app.services.evidence_clusters import (
    build_evidence_clusters,
    cluster_setting_key,
    update_cluster_note,
    update_pair_override,
)
from app.services.exports import csv_export, html_export, json_export, render_html_report
from app.services.external_api_settings import (
    abuseipdb_configured,
    companies_house_configured,
    get_abuseipdb_api_key,
    openphish_configured,
)
from app.services.face_matching import FaceMatcher, FaceMatchingError, correlate_faces
from app.services.graph_annotations import (
    add_manual_relationship,
    apply_graph_annotations,
    graph_annotation_setting_key,
    normalise_graph_annotations,
    remove_manual_relationship,
    update_node_note,
)
from app.services.handle_candidates import generate_handle_candidates
from app.services.historical_urls import build_historical_url_view
from app.services.identity_pivots import discover_display_name_pivots
from app.services.image_cache import ImageCache, ImageCacheError
from app.services.image_correlation import (
    ImageCorrelationError,
    ImageFingerprint,
    correlate_fingerprints,
    perceptual_dhash,
)
from app.services.infrastructure_enrichment import (
    InfrastructureEnrichmentError,
    enrich_infrastructure,
)
from app.services.investigation_accounts import build_investigation_account_view
from app.services.investigation_deletion import remove_investigation_raw_artifacts
from app.services.investigation_graph import (
    build_investigation_graph,
    graph_layout_setting_key,
)
from app.services.jobs import job_manager
from app.services.local_artifact_analysis import analyze_retained_artifacts
from app.services.local_image_evidence import LocalImageEvidenceError, load_local_image
from app.services.location_collection import (
    LocationCollectionError,
    PublicLocationCollector,
    policy_for_url,
)
from app.services.location_intelligence import (
    add_location_observation,
    build_location_clusters,
    seed_declared_locations,
)
from app.services.managed_browser_extraction import (
    collector_for_url as managed_browser_collector_for_url,
)
from app.services.managed_browser_extraction import (
    import_location_history as import_managed_location_history,
)
from app.services.managed_browser_extraction import (
    import_managed_search_results,
    search_results_from_capture,
    social_profile_was_read,
)
from app.services.managed_browser_extraction import (
    import_social_profile as import_managed_social_profile,
)
from app.services.metasearch2_settings import (
    ENGINE_CATALOG as METASEARCH2_ENGINE_CATALOG,
)
from app.services.metasearch2_settings import (
    get_active_metasearch2_engines,
    reset_active_metasearch2_engines,
    save_active_metasearch2_engines,
)
from app.services.phone_intelligence import (
    PhoneNormalisationError,
    looks_like_phone,
    normalise_phone,
)
from app.services.phone_presence_settings import (
    PLATFORM_CATALOG as PHONE_PRESENCE_PLATFORM_CATALOG,
)
from app.services.phone_presence_settings import (
    get_active_phone_presence_platforms,
    reset_active_phone_presence_platforms,
    save_active_phone_presence_platforms,
)
from app.services.pivots import (
    discover_email_pivots,
    discover_username_pivots,
    find_email_pivot,
    find_username_pivot,
    queue_automatic_username_pivots,
    queue_email_pivot,
    queue_seeded_username_sweep,
    queue_username_pivot,
)
from app.services.platform_icons import platform_icon_svg
from app.services.presentation import dashboard_summary, investigation_summary
from app.services.profile_enrichment import ProfileEnricher, ProfileEnrichmentError
from app.services.raw_json import RawJsonError, list_raw_json_artifacts, load_raw_json_artifact
from app.services.reverse_image import ReverseImageSearchError, reverse_image_target
from app.services.runtime_settings import (
    get_runtime_options,
    reset_runtime_options,
    save_runtime_options,
)
from app.services.search_imports import import_search_result_items, import_search_results
from app.services.search_queries import (
    DOCUMENT_TYPES,
    ENGINE_URLS,
    SEARCH_PLATFORMS,
    SearchQueryOptions,
    default_search_options,
    find_search_query,
    generate_google_dorks,
    generate_search_queries,
    name_variants_for_search,
    sign_search_query,
    validate_search_options,
    validate_template_definition,
    verify_search_query_token,
)
from app.services.search_searxng_capture import search_via_searxng
from app.services.searxng_settings import (
    ENGINE_CATALOG as SEARXNG_ENGINE_CATALOG,
)
from app.services.searxng_settings import (
    get_active_searxng_engines,
    reset_active_searxng_engines,
    save_active_searxng_engines,
)
from app.services.site_intelligence_view import build_site_intelligence_view
from app.services.telegram_intelligence import (
    MAX_EXPORT_BYTES,
    TelegramImportError,
    message_metadata,
    parse_telegram_export,
)
from app.services.tiktok_evidence import (
    TikTokEvidenceError,
    finalize_acquisition,
    validate_tiktok_post,
)
from app.services.tiktok_evidence import (
    evidence_zip as build_tiktok_evidence_zip,
)
from app.services.tool_run_logs import ToolRunLogError, load_tool_run_log
from app.services.tool_settings import (
    MAX_TOOL_TIMEOUT_SECONDS,
    MIN_TOOL_TIMEOUT_SECONDS,
    built_in_tool_timeouts,
    get_tool_timeouts,
    reset_tool_timeouts,
    save_tool_timeouts,
    validate_tool_timeout,
)
from app.services.toolkit_recommendations import (
    find_toolkit_recommendation,
    toolkit_recommendations,
)
from app.services.update_build_range import load_update_build_range
from app.services.update_changelog import load_update_changelog
from app.services.urlscan_settings import urlscan_api_key_configured
from app.services.vies import VIESError, check_vat_number
from app.services.wayback import (
    WaybackCapture,
    WaybackError,
    archived_asset_url,
    build_identity_evolution,
    compare_archived_pages,
    discover_captures,
    normalise_target,
    save_page_now,
)
from app.services.whatsmyname import WhatsMyNameError, cached_dataset, refresh_dataset

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["platform_icon_svg"] = platform_icon_svg
USERNAME_RE = re.compile(r"^[\w.\-]{1,64}$", re.UNICODE)
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$"
)
EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,253}$")
NAME_PART = r"(?:[^\W\d_](?:[^\W\d_]|[.'’\-](?=[^\W\d_]))*\.?)"
NAME_RE = re.compile(rf"^(?=.{{3,200}}$){NAME_PART}(?:\s+{NAME_PART})+$", re.UNICODE)


def detect_query_type(value: str) -> QueryType | None:
    value = value.strip()
    if looks_like_phone(value):
        return QueryType.PHONE
    if EMAIL_RE.fullmatch(value):
        return QueryType.EMAIL
    if DOMAIN_RE.fullmatch(value):
        return QueryType.DOMAIN
    if USERNAME_RE.fullmatch(value):
        return QueryType.USERNAME
    if NAME_RE.fullmatch(value):
        return QueryType.NAME
    return None


def _parse_additional_identifiers(raw: object) -> list[case_identifiers.IdentifierSeed]:
    try:
        return case_identifiers.parse_additional_identifiers(
            str(raw or ""), detect_query_type, validate_typed_query
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


def validate_typed_query(value: str, query_type: QueryType) -> None:
    if query_type == QueryType.PHONE:
        try:
            normalise_phone(value)
        except PhoneNormalisationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        return
    pattern = {
        QueryType.USERNAME: USERNAME_RE,
        QueryType.EMAIL: EMAIL_RE,
        QueryType.DOMAIN: DOMAIN_RE,
        QueryType.NAME: NAME_RE,
    }[query_type]
    if not pattern.fullmatch(value):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"invalid {query_type} value")


def parse_optional_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid date or time") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_optional_float(value: object) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid number") from exc


def parse_optional_int(value: object) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid whole number") from exc


def csrf(request: Request, supplied: str | None) -> None:
    if not verify_csrf(request.session.get("csrf"), supplied):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid CSRF token")


def context(request: Request, **values: object) -> dict[str, object]:
    if "csrf" not in request.session:
        request.session["csrf"] = make_csrf_token()
    settings = get_settings()
    return {
        "request": request,
        "csrf": request.session["csrf"],
        "managed_browser_enabled": settings.browser_enabled,
        "managed_browser_desktop_url": settings.browser_desktop_url,
        "app_version": settings.version,
        "app_build_commit": settings.build_commit.strip(),
        **values,
    }


def validated_shortcut_target(target_url: str) -> str:
    if "\r" in target_url or "\n" in target_url:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "shortcut URL is invalid")
    parsed = urlsplit(target_url)
    hostname = parsed.hostname
    allowed_host = hostname in {"localhost", "127.0.0.1", "::1"}
    if hostname and not allowed_host:
        try:
            address = ipaddress.ip_address(hostname)
            allowed_host = address.is_private
        except ValueError:
            allowed_host = False
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or not allowed_host
    ):
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "shortcut URL must target loopback or a private LAN address",
        )
    return target_url


def windows_shortcut_response(target_url: str, filename: str) -> Response:
    target = validated_shortcut_target(target_url)
    body = f"[InternetShortcut]\r\nURL={target}\r\n"
    return Response(
        body,
        media_type="application/internet-shortcut",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def macos_shortcut_response(target_url: str, filename: str) -> Response:
    target = escape(validated_shortcut_target(target_url), quote=False)
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict><key>URL</key>'
        f"<string>{target}</string></dict></plist>\n"
    )
    return Response(
        body,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def purge_investigation_artifacts(investigation: Investigation) -> dict[str, int]:
    if investigation.status in {"queued", "running"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "cancel the active investigation before deleting it",
        )
    settings = get_settings()
    browser_files = 0
    if investigation.browser_runs:
        try:
            browser_files = await BrowserAgentClient(settings).delete_investigation_evidence(
                investigation.id
            )
        except BrowserAgentError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"browser evidence could not be deleted: {exc}",
            ) from exc
    try:
        raw_files = remove_investigation_raw_artifacts(settings.data_dir, investigation.id)
    except OSError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "raw collector evidence could not be deleted",
        ) from exc
    return {"raw_files": raw_files, "browser_files": browser_files}


def load_investigation(db: Session, investigation_id: str) -> Investigation:
    item = db.scalar(
        select(Investigation)
        .options(
            selectinload(Investigation.runs),
            selectinload(Investigation.findings),
            selectinload(Investigation.location_observations),
            selectinload(Investigation.browser_runs),
        )
        .where(Investigation.id == investigation_id)
    )
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "investigation not found")
    return item


@router.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
def readiness() -> dict[str, object]:
    return {"status": "ready"}


def _manual_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "section"


def _render_operator_manual(markdown_text: str) -> tuple[str, list[dict[str, str]]]:
    parser = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": True})
    tokens = parser.parse(markdown_text)
    toc: list[dict[str, str]] = []
    seen: dict[str, int] = {}
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.tag not in {"h2", "h3"}:
            continue
        if index + 1 >= len(tokens) or tokens[index + 1].type != "inline":
            continue
        title = tokens[index + 1].content.strip()
        base = _manual_slug(title)
        count = seen.get(base, 0)
        seen[base] = count + 1
        anchor = base if count == 0 else f"{base}-{count + 1}"
        token.attrSet("id", anchor)
        toc.append({"title": title, "anchor": anchor, "level": token.tag})
    return parser.renderer.render(tokens, parser.options, {}), toc


@router.get("/manual", response_class=HTMLResponse)
def operator_manual(request: Request) -> Response:
    manual_path = Path(__file__).resolve().parents[2] / "docs" / "MANUAL.md"
    if not manual_path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Corvus operator manual is not installed")
    manual_html, manual_toc = _render_operator_manual(manual_path.read_text(encoding="utf-8"))
    return templates.TemplateResponse(
        request,
        "manual.html",
        context(request, manual_html=manual_html, manual_toc=manual_toc),
    )


@router.get("/manual/download")
def download_operator_manual() -> Response:
    manual_path = Path(__file__).resolve().parents[2] / "docs" / "MANUAL.md"
    if not manual_path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Corvus operator manual is not installed")
    return Response(
        manual_path.read_text(encoding="utf-8"),
        media_type="text/markdown",
        headers={"Content-Disposition": 'attachment; filename="Corvus-Operator-Manual.md"'},
    )


def update_changelog_values(request: Request) -> dict[str, object]:
    settings = get_settings()
    build_range = load_update_build_range(settings.update_build_range_path)
    return context(
        request,
        build_range=build_range,
        update_changelog=load_update_changelog(settings.update_changelog_path),
    )


@router.get("/maintenance/update", response_class=HTMLResponse)
def update_launcher_page(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "update_launcher.html",
        update_changelog_values(request),
    )


@router.get("/maintenance/whats-new", response_class=HTMLResponse)
def whats_new_page(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "whats_new.html",
        update_changelog_values(request),
    )


@router.get("/maintenance/changelog")
def full_changelog() -> Response:
    changelog_path = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
    if not changelog_path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Corvus changelog is not installed")
    return Response(
        changelog_path.read_text(encoding="utf-8"),
        media_type="text/markdown",
        headers={"Content-Disposition": 'inline; filename="Corvus-CHANGELOG.md"'},
    )


@router.get("/maintenance/update-launcher/{platform}")
def update_launcher_download(platform: str) -> Response:
    if platform == "windows":
        content = """@echo off\r\nsetlocal\r\nset \"UPDATER=\"\r\nfor /f \"usebackq delims=\" %%I in (`powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command \"$c=(Get-ItemProperty -LiteralPath 'HKCU:\\Software\\Classes\\corvus-update\\shell\\open\\command' -ErrorAction SilentlyContinue).'(default)'; if($c -match '-File \\\"([^\\\"]+Update-Corvus(?:GUI)?\\.ps1)\\\"'){ $p=$matches[1]; if($p -like '*Update-CorvusGUI.ps1'){ $p=Join-Path (Split-Path -Parent $p) 'Update-Corvus.ps1' }; if(Test-Path -LiteralPath $p -PathType Leaf){$p} }\"`) do set \"UPDATER=%%I\"\r\nif not defined UPDATER for /f \"usebackq delims=\" %%I in (`powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command \"$a=(Get-ScheduledTask -TaskName 'Corvus Daily Update' -ErrorAction SilentlyContinue).Actions | Select-Object -First 1; if($a -and $a.Arguments -match '-File \\\"([^\\\"]+Update-Corvus\\.ps1)\\\"'){ $p=$matches[1]; if(Test-Path -LiteralPath $p -PathType Leaf){$p} }\"`) do set \"UPDATER=%%I\"\r\nif not defined UPDATER if exist \"%LOCALAPPDATA%\\Programs\\Corvus\\app\\Update-Corvus.ps1\" set \"UPDATER=%LOCALAPPDATA%\\Programs\\Corvus\\app\\Update-Corvus.ps1\"\r\nif not defined UPDATER (\r\n  echo Corvus updater could not be discovered from the registered update protocol, scheduled update task, or standard installation.\r\n  pause\r\n  exit /b 1\r\n)\r\npowershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File \"%UPDATER%\" -OnlyIfChanged\r\nif errorlevel 1 pause\r\n"""
        filename = "Check-Corvus-Updates.cmd"
        media_type = "application/x-msdos-program"
    elif platform == "macos":
        content = """#!/bin/zsh
set -euo pipefail
UPDATER="$HOME/Library/Application Support/OSINT Workbench/app/installer/macos/update-osint-workbench.sh"
if [[ ! -x "$UPDATER" ]]; then
  print -u2 "Corvus updater was not found at: $UPDATER"
  exit 1
fi
exec "$UPDATER"
"""
        filename = "Check-Corvus-Updates.command"
        media_type = "text/x-shellscript"
    else:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "update launcher not found")
    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)) -> Response:
    investigations = list(
        db.scalars(
            select(Investigation)
            .options(
                selectinload(Investigation.runs),
                selectinload(Investigation.findings),
                selectinload(Investigation.location_observations),
            )
            .order_by(Investigation.created_at.desc())
            .limit(50)
        )
    )
    configured_timeouts = get_tool_timeouts(db)
    tools = [
        {
            "id": key,
            "name": cls.display_name,
            "types": [x.value for x in cls.supported_query_types],
            "recovery": cls.recovery_endpoint,
            "default_selected": cls.default_selected,
            "timeout": configured_timeouts[key],
        }
        for key, cls in REGISTRY.items()
    ]
    investigation_summaries = {item.id: investigation_summary(item) for item in investigations}
    deleted_value = request.query_params.get("deleted", "")
    deleted_count = int(deleted_value) if deleted_value.isdigit() else 0
    return templates.TemplateResponse(
        request,
        "index.html",
        context(
            request,
            investigations=investigations,
            investigation_summaries=investigation_summaries,
            dashboard=dashboard_summary(investigations),
            tools=tools,
            min_tool_timeout=MIN_TOOL_TIMEOUT_SECONDS,
            max_tool_timeout=MAX_TOOL_TIMEOUT_SECONDS,
            deleted_count=deleted_count,
        ),
    )


@router.get("/settings", response_class=HTMLResponse)
async def application_settings(request: Request, db: Session = Depends(get_db)) -> Response:
    configured_timeouts = get_tool_timeouts(db)
    built_in_timeouts = built_in_tool_timeouts()
    settings = get_settings()
    cached_wmn = cached_dataset(settings.data_dir)
    wmn_summary = cached_wmn[1] if cached_wmn else None
    active_searxng_engines = get_active_searxng_engines(db)
    active_metasearch2_engines = get_active_metasearch2_engines(db)
    active_phone_presence_platforms = get_active_phone_presence_platforms(db)
    runtime_options = get_runtime_options(db)
    configured_searxng_engines: set[str] = set()
    searxng_status = "unavailable"
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            response = await client.get(f"{settings.searxng_url.rstrip('/')}/config")
            response.raise_for_status()
            payload = response.json()
        raw_engines = payload.get("engines", []) if isinstance(payload, dict) else []
        if isinstance(raw_engines, list):
            for item in raw_engines:
                if isinstance(item, dict) and item.get("name"):
                    configured_searxng_engines.add(str(item["name"]).casefold())
        searxng_status = "ready"
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    tools = [
        {
            "id": tool_id,
            "name": adapter.display_name,
            "types": [query_type.value for query_type in adapter.supported_query_types],
            "timeout": configured_timeouts[tool_id],
            "built_in_timeout": built_in_timeouts[tool_id],
        }
        for tool_id, adapter in REGISTRY.items()
    ]
    return templates.TemplateResponse(
        request,
        "settings.html",
        context(
            request,
            tools=tools,
            min_timeout=MIN_TOOL_TIMEOUT_SECONDS,
            max_timeout=MAX_TOOL_TIMEOUT_SECONDS,
            saved=request.query_params.get("saved") == "1",
            reset=request.query_params.get("reset") == "1",
            wmn_summary=wmn_summary,
            wmn_refreshed=request.query_params.get("wmn_refreshed") == "1",
            wmn_added=int(request.query_params.get("wmn_added", "0") or 0),
            wmn_removed=int(request.query_params.get("wmn_removed", "0") or 0),
            wmn_changed=int(request.query_params.get("wmn_changed", "0") or 0),
            searxng_engines=[
                {
                    "id": engine.engine_id,
                    "name": engine.name,
                    "description": engine.description,
                    "active": engine.engine_id in active_searxng_engines,
                    "available": (
                        engine.engine_id in configured_searxng_engines
                        if searxng_status == "ready"
                        else None
                    ),
                }
                for engine in SEARXNG_ENGINE_CATALOG
            ],
            metasearch2_engines=[
                {
                    "id": engine.engine_id,
                    "name": engine.name,
                    "description": engine.description,
                    "active": engine.engine_id in active_metasearch2_engines,
                }
                for engine in METASEARCH2_ENGINE_CATALOG
            ],
            phone_presence_platforms=[
                {
                    "id": platform.platform_id,
                    "name": platform.name,
                    "description": platform.description,
                    "risk_note": platform.risk_note,
                    "active": platform.platform_id in active_phone_presence_platforms,
                }
                for platform in PHONE_PRESENCE_PLATFORM_CATALOG
            ],
            runtime_options=runtime_options,
            tor_configured=bool(settings.tor_proxy),
            searxng_status=searxng_status,
            searxng_saved=request.query_params.get("searxng_saved") == "1",
            searxng_reset=request.query_params.get("searxng_reset") == "1",
            metasearch2_saved=request.query_params.get("metasearch2_saved") == "1",
            metasearch2_reset=request.query_params.get("metasearch2_reset") == "1",
            phone_presence_saved=request.query_params.get("phone_presence_saved") == "1",
            phone_presence_reset=request.query_params.get("phone_presence_reset") == "1",
            runtime_saved=request.query_params.get("runtime_saved") == "1",
            runtime_reset=request.query_params.get("runtime_reset") == "1",
            urlscan_configured=urlscan_api_key_configured(db),
            companies_house_configured=companies_house_configured(db),
            abuseipdb_configured=abuseipdb_configured(db),
            openphish_configured=openphish_configured(db),
        ),
    )


@router.post("/settings/tool-timeouts")
async def update_tool_timeout_settings(request: Request, db: Session = Depends(get_db)) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    action = str(form.get("action", "save"))
    if action == "reset":
        values = reset_tool_timeouts(db)
        event_type = "tool_timeout_settings_reset"
        destination = "/settings?reset=1"
    elif action == "save":
        try:
            values = save_tool_timeouts(
                db,
                {tool_id: form.get(f"timeout_{tool_id}") for tool_id in REGISTRY},
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        event_type = "tool_timeout_settings_updated"
        destination = "/settings?saved=1"
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid settings action")

    db.add(
        AuditEvent(
            investigation_id=None,
            event_type=event_type,
            details={"timeouts": values},
        )
    )
    db.commit()
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/settings/searxng-engines")
async def update_searxng_engine_settings(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    action = str(form.get("action", "save"))
    if action == "reset":
        selected = reset_active_searxng_engines(db)
        event_type = "searxng_engine_settings_reset"
        destination = "/settings?searxng_reset=1"
    elif action == "save":
        try:
            selected = save_active_searxng_engines(db, list(form.getlist("engine")))
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        event_type = "searxng_engine_settings_updated"
        destination = "/settings?searxng_saved=1"
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid settings action")
    db.add(
        AuditEvent(
            investigation_id=None,
            event_type=event_type,
            details={"engines": list(selected)},
        )
    )
    db.commit()
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/settings/phone-presence-platforms")
async def update_phone_presence_platform_settings(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    action = str(form.get("action", "save"))
    if action == "reset":
        selected = reset_active_phone_presence_platforms(db)
        event_type = "phone_presence_platform_settings_reset"
        destination = "/settings?phone_presence_reset=1"
    elif action == "save":
        selected = save_active_phone_presence_platforms(db, list(form.getlist("platform")))
        event_type = "phone_presence_platform_settings_updated"
        destination = "/settings?phone_presence_saved=1"
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid settings action")
    db.add(
        AuditEvent(
            investigation_id=None,
            event_type=event_type,
            details={"platforms": list(selected)},
        )
    )
    db.commit()
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/settings/metasearch2-engines")
async def update_metasearch2_engine_settings(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    action = str(form.get("action", "save"))
    if action == "reset":
        selected = reset_active_metasearch2_engines(db)
        event_type = "metasearch2_engine_settings_reset"
        destination = "/settings?metasearch2_reset=1"
    elif action == "save":
        try:
            selected = save_active_metasearch2_engines(db, list(form.getlist("engine")))
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        event_type = "metasearch2_engine_settings_updated"
        destination = "/settings?metasearch2_saved=1"
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid settings action")
    db.add(AuditEvent(investigation_id=None, event_type=event_type, details={"engines": list(selected)}))
    db.commit()
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/settings/runtime-options")
async def update_runtime_option_settings(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    action = str(form.get("action", "save"))
    if action == "reset":
        selected = reset_runtime_options(db)
        event_type = "runtime_settings_reset"
        destination = "/settings?runtime_reset=1"
    elif action == "save":
        selected = save_runtime_options(
            db,
            {
                "browser_share_sessions": "browser_share_sessions" in form,
                "automatic_wayback": "automatic_wayback" in form,
                "automatic_dork_run": "automatic_dork_run" in form,
                "phone_presence_via_tor": "phone_presence_via_tor" in form,
            },
        )
        event_type = "runtime_settings_updated"
        destination = "/settings?runtime_saved=1"
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid settings action")
    db.add(AuditEvent(investigation_id=None, event_type=event_type, details=selected))
    db.commit()
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/settings/whatsmyname/refresh")
async def refresh_whatsmyname_coverage(request: Request, db: Session = Depends(get_db)) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    try:
        comparison = await refresh_dataset(get_settings().data_dir)
    except (WhatsMyNameError, httpx.HTTPError, OSError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    details = {
        "revision": comparison.active.revision,
        "sha256": comparison.active.sha256,
        "sites": comparison.active.site_count,
        "categories": comparison.active.category_count,
        "added": len(comparison.added_sites),
        "removed": len(comparison.removed_sites),
        "changed": len(comparison.changed_sites),
    }
    db.add(
        AuditEvent(
            investigation_id=None,
            event_type="whatsmyname_dataset_refreshed",
            details=details,
        )
    )
    db.commit()
    query = urlencode(
        {
            "wmn_refreshed": "1",
            "wmn_added": details["added"],
            "wmn_removed": details["removed"],
            "wmn_changed": details["changed"],
        }
    )
    return RedirectResponse(f"/settings?{query}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/shortcuts/osint-workbench.url")
def download_workbench_shortcut() -> Response:
    settings = get_settings()
    return windows_shortcut_response(
        f"http://127.0.0.1:{settings.port}/",
        "Corvus.url",
    )


@router.get("/shortcuts/osint-workbench.webloc")
def download_macos_workbench_shortcut() -> Response:
    settings = get_settings()
    return macos_shortcut_response(
        f"http://127.0.0.1:{settings.port}/",
        "Corvus.webloc",
    )


@router.get("/api/detect")
def detect(q: str) -> dict[str, str | None]:
    detected = detect_query_type(q[:321])
    return {"query_type": detected.value if detected else None}


@router.post("/investigations")
async def create_investigation(request: Request, db: Session = Depends(get_db)) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    raw_query = str(form.get("query", "")).strip()
    detected_type = detect_query_type(raw_query)
    manual_type = str(form.get("query_type_override", "")) == "on"
    supplied_type: QueryType | None
    try:
        supplied_type = QueryType(str(form.get("query_type", "")))
    except ValueError:
        if manual_type:
            raise HTTPException(422, "select a valid query type") from None
        supplied_type = detected_type
    query_type = supplied_type if manual_type else detected_type or supplied_type
    if query_type is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "could not determine whether the search value is a username, email address, domain or full name",
        )
    compatible_tools = [
        tool_id
        for tool_id, adapter in REGISTRY.items()
        if query_type in adapter.supported_query_types
    ]
    default_tools = [
        tool_id
        for tool_id in compatible_tools
        if tool_id != "metasearch2" and REGISTRY[tool_id].default_selected
    ]
    submitted_tools = [str(x) for x in form.getlist("tools")]
    extended_username_search = (
        query_type == QueryType.USERNAME
        and str(form.get("extended_username_search", "")) == "on"
    )
    if supplied_type != query_type:
        tools = default_tools
    else:
        tools = [tool for tool in submitted_tools if tool in compatible_tools]
        if not tools:
            tools = default_tools
    # Naminter's full WMN sweep is intentionally a deep-search collector. Keep it
    # off the latency-sensitive first pass, but add it automatically when the
    # examiner explicitly requests an extended username search.
    if extended_username_search and "naminter" in compatible_tools and "naminter" not in tools:
        tools.append("naminter")
    try:
        configured_timeouts = get_tool_timeouts(db)
        timeouts = {
            tool: validate_tool_timeout(
                tool,
                form.get(f"timeout_{tool}", configured_timeouts[tool]),
            )
            for tool in tools
        }
        payload = SearchRequest(
            query=raw_query,
            query_type=query_type,
            title=str(form.get("title") or "") or None,
            case_reference=str(form.get("case_reference") or "") or None,
            notes=str(form.get("notes") or "") or None,
            tags=[x.strip() for x in str(form.get("tags") or "").split(",") if x.strip()],
            tools=tools,
            timeouts=timeouts,
            proxy=str(form.get("proxy") or "") or None,
            auto_follow_username_pivots=(
                query_type in {QueryType.EMAIL, QueryType.USERNAME}
                and (
                    supplied_type != query_type
                    or str(form.get("auto_follow_username_pivots", "")) == "on"
                )
            ),
            extended_username_search=extended_username_search,
        )
    except (ValidationError, ValueError, KeyError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    validate_typed_query(payload.query, payload.query_type)
    additional_seeds = _parse_additional_identifiers(form.get("additional_identifiers"))
    if payload.proxy not in {None, "http_proxy", "https_proxy", "socks_proxy", "tor_proxy"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid proxy selection")
    investigation = Investigation(
        title=payload.title or f"{payload.query_type.title()} investigation",
        query=payload.query,
        query_type=payload.query_type,
        case_reference=payload.case_reference,
        notes=payload.notes,
        tags=payload.tags,
        selected_tools=tools,
        proxy_name=payload.proxy,
        auto_follow_username_pivots=payload.auto_follow_username_pivots,
        extended_username_search=payload.extended_username_search,
    )
    db.add(investigation)
    db.flush()
    for tool in tools:
        adapter = REGISTRY[tool]
        db.add(
            ToolRun(
                investigation_id=investigation.id,
                tool_id=tool,
                display_name=adapter.display_name,
                timeout_seconds=timeouts[tool],
                tool_commit=commit_for(tool),
            )
        )
    db.flush()
    additional_runs = case_identifiers.queue_additional_identifiers(
        db, investigation, additional_seeds
    )
    investigation.selected_tools = list(
        dict.fromkeys([*tools, *(run.tool_id for run in additional_runs)])
    )
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="investigation_created",
            details={
                "query_type": payload.query_type,
                "tools": tools,
                "auto_follow_username_pivots": payload.auto_follow_username_pivots,
                "extended_username_search": payload.extended_username_search,
                "search_led": payload.query_type == QueryType.NAME,
            },
        )
    )
    db.commit()
    job_manager.start(investigation.id)
    if (
        get_settings().browser_enabled
        and get_runtime_options(db)["automatic_dork_run"]
        and dork_run.dork_seeds(investigation)
    ):
        waiter = asyncio.create_task(_automatic_dork_run_after_collection(investigation.id))
        automatic_dork_run_waiters.add(waiter)
        waiter.add_done_callback(automatic_dork_run_waiters.discard)
    destination = f"/investigations/{investigation.id}"
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/investigations/{investigation_id}/raw-json", response_class=HTMLResponse)
def investigation_raw_json(
    investigation_id: str,
    request: Request,
    run_id: str | None = None,
    artifact: str | None = None,
    download: bool = False,
    db: Session = Depends(get_db),
) -> Response:
    investigation = load_investigation(db, investigation_id)
    settings = get_settings()
    all_artifacts = list_raw_json_artifacts(settings.data_dir, investigation)
    selected_run = None
    artifacts = all_artifacts
    if run_id:
        selected_run = db.get(ToolRun, run_id)
        if selected_run is None or selected_run.investigation_id != investigation.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "tool run not found")
        artifacts = [item for item in all_artifacts if item.run_id == run_id]

    selected = None
    json_text = None
    error = None
    if run_id and artifact:
        if selected_run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "tool run not found")
        try:
            raw, json_text, _payload = load_raw_json_artifact(
                settings.data_dir, investigation, selected_run, artifact
            )
        except RawJsonError as exc:
            error = str(exc)
        else:
            selected = next(
                (item for item in artifacts if item.relative_path == artifact),
                None,
            )
            if download:
                filename = re.sub(r"[^A-Za-z0-9._-]", "_", Path(artifact).name) or "artifact.json"
                return Response(
                    raw,
                    media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'},
                )
    elif artifacts:
        preferred = next(
            (item for item in artifacts if item.category != "collector configuration"),
            artifacts[0],
        )
        query = urlencode({"run_id": preferred.run_id, "artifact": preferred.relative_path})
        return RedirectResponse(
            f"/investigations/{investigation.id}/raw-json?{query}",
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        )
    elif selected_run is not None:
        limitation = selected_run.error_message or "The collector did not produce a JSON artifact."
        error = (
            f"No JSON artifact was retained for {selected_run.display_name}. "
            f"Run status: {selected_run.status.replace('_', ' ')}. {limitation}"
        )
    return templates.TemplateResponse(
        request,
        "raw_json.html",
        context(
            request,
            investigation=investigation,
            artifacts=artifacts,
            all_artifact_count=len(all_artifacts),
            selected_run=selected_run,
            selected=selected,
            json_text=json_text,
            error=error,
        ),
    )


@router.get(
    "/investigations/{investigation_id}/runs/{run_id}/log",
    response_class=HTMLResponse,
)
def investigation_tool_run_log(
    investigation_id: str,
    run_id: str,
    request: Request,
    download: bool = False,
    db: Session = Depends(get_db),
) -> Response:
    investigation = load_investigation(db, investigation_id)
    run = db.get(ToolRun, run_id)
    if run is None or run.investigation_id != investigation.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tool run not found")
    log = None
    error = None
    try:
        log = load_tool_run_log(get_settings().data_dir, investigation, run)
    except ToolRunLogError as exc:
        error = str(exc)
    if download and log is not None:
        filename = f"{re.sub(r'[^A-Za-z0-9._-]', '_', run.tool_id)}-{run.id[:8]}-execution.log"
        return Response(
            log.content,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return templates.TemplateResponse(
        request,
        "tool_run_log.html",
        context(
            request,
            investigation=investigation,
            run=run,
            log=log,
            error=error,
        ),
    )


SEARCH_ENGINE_MANUAL_URLS = {
    "bing": "https://www.bing.com/search",
    "brave": "https://search.brave.com/search",
    "duckduckgo": "https://duckduckgo.com/",
    "google": "https://www.google.com/search",
    "mojeek": "https://www.mojeek.com/search",
    "qwant": "https://www.qwant.com/",
    "startpage": "https://www.startpage.com/sp/search",
}
SEARCH_ENGINE_HANDOFF_MARKERS = (
    "captcha",
    "challenge",
    "too many requests",
    "rate limit",
    "rate-limit",
    "access denied",
    "temporarily blocked",
)
SEARCH_ENGINE_WARMUP_QUERY = "time in new york"
BROWSER_SEARCH_ENGINES = ("duckduckgo", "startpage")


def _search_engine_url(engine: str, query: str) -> str:
    base = SEARCH_ENGINE_MANUAL_URLS.get(engine.casefold())
    if base is None:
        raise ValueError("unsupported manual search engine")
    if engine.casefold() == "startpage":
        return f"{base}?{urlencode({'query': query})}"
    if engine.casefold() == "qwant":
        return f"{base}?{urlencode({'q': query, 't': 'web'})}"
    if engine.casefold() == "bing":
        return f"{base}?{urlencode({'q': query, 'cc': 'ie'})}"
    return f"{base}?{urlencode({'q': query})}"


def _searxng_handoff_candidates(
    investigation: Investigation, run: ToolRun
) -> list[dict[str, str]]:
    if run.tool_id != "searxng_name_search":
        return []
    settings = get_settings()
    artifact = next(
        (
            item
            for item in list_raw_json_artifacts(settings.data_dir, investigation)
            if item.run_id == run.id and item.filename == "searxng-name-search.json"
        ),
        None,
    )
    if artifact is None:
        return []
    try:
        _raw, _pretty, payload = load_raw_json_artifact(
            settings.data_dir, investigation, run, artifact.relative_path
        )
    except RawJsonError:
        return []
    failures = payload.get("unresponsive_engines", []) if isinstance(payload, dict) else []
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    if not isinstance(failures, list):
        return []
    for item in failures:
        if not isinstance(item, dict):
            continue
        engine = str(item.get("engine") or "").casefold()
        error = str(item.get("error") or "")
        query_id = str(item.get("query_id") or "")
        query = str(item.get("query") or "")
        if engine not in SEARCH_ENGINE_MANUAL_URLS or not query_id or not query:
            continue
        if not any(marker in error.casefold() for marker in SEARCH_ENGINE_HANDOFF_MARKERS):
            continue
        key = (engine, query_id, query)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "engine": engine,
                "query_id": query_id,
                "query": query,
                "error": error[:300],
                "run_id": run.id,
            }
        )
    return candidates


@router.get("/investigations/{investigation_id}", response_class=HTMLResponse)
def investigation_detail(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    investigation = load_investigation(db, investigation_id)
    followed_seed_handles = {
        (run.query_value or "").casefold()
        for run in investigation.runs
        if run.query_type == QueryType.USERNAME.value and run.query_value
    }
    cluster_setting = db.get(ApplicationSetting, cluster_setting_key(investigation.id))
    cluster_controls = cluster_setting.value if cluster_setting is not None else None
    handle_candidates = (
        [
            {
                "username": candidate.username,
                "source_name": candidate.source_name,
                "provenance": candidate.provenance,
                "followed": candidate.username.casefold() in followed_seed_handles,
            }
            for candidate in generate_handle_candidates(investigation.query)
        ]
        if investigation.query_type == QueryType.NAME.value
        else []
    )
    searxng_manual_actions = [
        candidate
        for run in investigation.runs
        for candidate in _searxng_handoff_candidates(investigation, run)
    ]
    retry_attempts = retry_targets(db, investigation.id)
    return templates.TemplateResponse(
        request,
        "investigation.html",
        context(
            request,
            investigation=investigation,
            searxng_manual_actions=searxng_manual_actions,
            summary=investigation_summary(investigation),
            account_view=build_investigation_account_view(investigation),
            historical_url_view=build_historical_url_view(investigation),
            site_intelligence_view=build_site_intelligence_view(investigation),
            validation_started=request.query_params.get("validation") == "started",
            retryable_run_ids={run.id for run in investigation.runs if can_retry(run)} - retry_attempts.keys(),
            retry_attempts=retry_attempts,
            retry_started=request.query_params.get("retry") == "queued",
            validation_already_running=request.query_params.get("validation") == "running",
            username_pivots=discover_username_pivots(investigation),
            email_pivots=discover_email_pivots(investigation),
            display_name_pivots=discover_display_name_pivots(investigation),
            handle_candidates=handle_candidates,
            toolkit_recommendations=toolkit_recommendations(investigation),
            additional_identifiers=case_identifiers.case_seeds(investigation)[1:],
            max_additional_identifiers=case_identifiers.MAX_ADDITIONAL_IDENTIFIERS,
            evidence_clusters=build_evidence_clusters(investigation, cluster_controls),
            abuseipdb_configured=abuseipdb_configured(db),
        ),
    )


@router.post("/investigations/{investigation_id}/runs/{run_id}/retry")
async def retry_collector_run(
    investigation_id: str, run_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    original = next((run for run in investigation.runs if run.id == run_id), None)
    if original is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "collector run not found in this case")
    if investigation.id in job_manager.tasks:
        raise HTTPException(status.HTTP_409_CONFLICT, "wait for the current collection to finish")
    try:
        timeout = validate_tool_timeout(original.tool_id, form.get("timeout", original.timeout_seconds))
        queue_retry(db, investigation, original, timeout)
    except RetryConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    db.commit()
    job_manager.start(investigation.id)
    return RedirectResponse(f"/investigations/{investigation.id}?retry=queued#overview", status_code=303)


@router.post("/investigations/{investigation_id}/deeper-username-search")
async def deepen_username_investigation(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    if investigation.id in job_manager.tasks:
        raise HTTPException(409, "wait for the current collection to finish")
    try:
        queue_deep_username_search(db, investigation)
    except RetryConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    job_manager.start(investigation.id)
    return RedirectResponse(f"/investigations/{investigation.id}#overview", status_code=303)


async def browser_page_values(
    investigation: Investigation,
) -> dict[str, object]:
    settings = get_settings()
    browser_status = await BrowserAgentClient(settings).status()
    return {
        "investigation": investigation,
        "browser_status": browser_status,
        "browser_runs": sorted(
            investigation.browser_runs, key=lambda item: item.created_at, reverse=True
        ),
        "browser_desktop_url": settings.browser_desktop_url,
        "allowed_hosts": configured_allowed_hosts(settings),
    }


def investigation_graph_positions(db: Session, investigation_id: str) -> dict[str, object]:
    setting = db.get(ApplicationSetting, graph_layout_setting_key(investigation_id))
    if setting is None or not isinstance(setting.value, dict):
        return {}
    positions = setting.value.get("positions", {})
    return positions if isinstance(positions, dict) else {}


def investigation_graph_annotations(db: Session, investigation_id: str) -> dict[str, Any]:
    setting = db.get(ApplicationSetting, graph_annotation_setting_key(investigation_id))
    return setting.value if setting is not None and isinstance(setting.value, dict) else {}


def _save_graph_annotations(db: Session, investigation_id: str, value: dict[str, Any]) -> None:
    key = graph_annotation_setting_key(investigation_id)
    setting = db.get(ApplicationSetting, key)
    if setting is None:
        db.add(ApplicationSetting(key=key, value=value))
    else:
        setting.value = value
        setting.updated_at = datetime.now(UTC)


@router.get("/investigations/{investigation_id}/graph", response_class=HTMLResponse)
def investigation_graph_page(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    investigation = load_investigation(db, investigation_id)
    graph = apply_graph_annotations(
        build_investigation_graph(
            investigation,
            saved_positions=investigation_graph_positions(db, investigation_id),
        ),
        investigation_graph_annotations(db, investigation_id),
    )
    annotation_nodes = sorted(
        (
            {"id": str(node["data"]["id"]), "label": str(node["data"].get("label") or node["data"]["id"])}
            for node in graph["nodes"]
            if isinstance(node, dict) and isinstance(node.get("data"), dict)
        ),
        key=lambda item: item["label"].casefold(),
    )
    annotations = normalise_graph_annotations(
        investigation_graph_annotations(db, investigation_id),
        {item["id"] for item in annotation_nodes},
    )
    return templates.TemplateResponse(
        request,
        "graph.html",
        context(
            request,
            investigation=investigation,
            graph_summary=graph["summary"],
            graph_annotation_nodes=annotation_nodes,
            graph_annotations=annotations,
        ),
    )


@router.get("/investigations/{investigation_id}/graph/data")
def investigation_graph_data(
    investigation_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    investigation = load_investigation(db, investigation_id)
    return apply_graph_annotations(
        build_investigation_graph(
            investigation,
            saved_positions=investigation_graph_positions(db, investigation_id),
        ),
        investigation_graph_annotations(db, investigation_id),
    )


@router.post("/investigations/{investigation_id}/graph/annotations/note")
async def save_investigation_graph_note(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    base_graph = build_investigation_graph(investigation)
    valid_ids = {
        str(node.get("data", {}).get("id"))
        for node in base_graph["nodes"]
        if isinstance(node, dict) and isinstance(node.get("data"), dict)
    }
    try:
        value = update_node_note(
            investigation_graph_annotations(db, investigation_id),
            valid_ids,
            str(form.get("node_id", "")),
            str(form.get("note", "")),
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    _save_graph_annotations(db, investigation_id, value)
    db.add(AuditEvent(investigation_id=investigation_id, event_type="investigation_graph_note_updated", details={"node_id": str(form.get("node_id", "")), "has_note": bool(str(form.get("note", "")).strip())}))
    db.commit()
    return RedirectResponse(f"/investigations/{investigation_id}/graph", status_code=303)


@router.post("/investigations/{investigation_id}/graph/annotations/relationship")
async def save_investigation_graph_relationship(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    base_graph = build_investigation_graph(investigation)
    valid_ids = {
        str(node.get("data", {}).get("id"))
        for node in base_graph["nodes"]
        if isinstance(node, dict) and isinstance(node.get("data"), dict)
    }
    try:
        value = add_manual_relationship(
            investigation_graph_annotations(db, investigation_id),
            valid_ids,
            str(form.get("source", "")),
            str(form.get("target", "")),
            str(form.get("label", "")),
            str(form.get("note", "")),
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    _save_graph_annotations(db, investigation_id, value)
    db.add(AuditEvent(investigation_id=investigation_id, event_type="investigation_graph_relationship_added", details={"source": str(form.get("source", "")), "target": str(form.get("target", "")), "label": str(form.get("label", ""))[:120]}))
    db.commit()
    return RedirectResponse(f"/investigations/{investigation_id}/graph", status_code=303)


@router.post("/investigations/{investigation_id}/graph/annotations/relationship/{relationship_id}/delete")
async def delete_investigation_graph_relationship(
    investigation_id: str,
    relationship_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    base_graph = build_investigation_graph(investigation)
    valid_ids = {
        str(node.get("data", {}).get("id"))
        for node in base_graph["nodes"]
        if isinstance(node, dict) and isinstance(node.get("data"), dict)
    }
    value = remove_manual_relationship(
        investigation_graph_annotations(db, investigation_id), valid_ids, relationship_id
    )
    _save_graph_annotations(db, investigation_id, value)
    db.add(AuditEvent(investigation_id=investigation_id, event_type="investigation_graph_relationship_removed", details={"relationship_id": relationship_id[:64]}))
    db.commit()
    return RedirectResponse(f"/investigations/{investigation_id}/graph", status_code=303)


def _graph_collect_target(query_type_value: str, value: str) -> tuple[QueryType, str]:
    try:
        query_type = QueryType(query_type_value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unsupported graph collection type") from exc
    if query_type not in {QueryType.USERNAME, QueryType.EMAIL, QueryType.DOMAIN}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unsupported graph collection type")
    normalized = value.strip().removeprefix("@") if query_type == QueryType.USERNAME else value.strip()
    validate_typed_query(normalized, query_type)
    return query_type, normalized


@router.get("/investigations/{investigation_id}/graph/collect", response_class=HTMLResponse)
def graph_collect_confirmation(
    investigation_id: str,
    request: Request,
    query_type: str,
    value: str,
    db: Session = Depends(get_db),
) -> Response:
    investigation = load_investigation(db, investigation_id)
    parsed_type, normalized = _graph_collect_target(query_type, value)
    configured_timeouts = get_tool_timeouts(db)
    compatible = [
        {
            "id": tool_id,
            "name": adapter.display_name,
            "timeout": configured_timeouts[tool_id],
            "default_selected": adapter.default_selected,
        }
        for tool_id, adapter in REGISTRY.items()
        if parsed_type in adapter.supported_query_types
    ]
    return templates.TemplateResponse(
        request,
        "graph_collect_confirm.html",
        context(
            request,
            investigation=investigation,
            query_type=parsed_type.value,
            value=normalized,
            tools=compatible,
        ),
    )


@router.post("/investigations/{investigation_id}/graph/collect")
async def graph_collect_identifier(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    if investigation.status in {"queued", "running"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "wait for the active investigation run to finish")
    parsed_type, normalized = _graph_collect_target(
        str(form.get("query_type", "")), str(form.get("value", ""))
    )
    selected = [str(item) for item in form.getlist("tools")]
    compatible = [
        tool_id
        for tool_id in selected
        if tool_id in REGISTRY and parsed_type in REGISTRY[tool_id].supported_query_types
    ]
    if not compatible:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "select at least one compatible collector")
    configured_timeouts = get_tool_timeouts(db)
    existing = {
        (run.tool_id, (run.query_type or investigation.query_type), (run.query_value or investigation.query).casefold())
        for run in investigation.runs
    }
    queued: list[ToolRun] = []
    for tool_id in compatible:
        identity = (tool_id, parsed_type.value, normalized.casefold())
        if identity in existing:
            continue
        adapter = REGISTRY[tool_id]
        run = ToolRun(
            investigation_id=investigation.id,
            tool_id=tool_id,
            display_name=adapter.display_name,
            status="queued",
            timeout_seconds=configured_timeouts[tool_id],
            tool_commit=commit_for(tool_id),
            query_value=normalized,
            query_type=parsed_type.value,
            pivot_depth=1,
            pivot_automatic=False,
        )
        db.add(run)
        queued.append(run)
    if queued:
        investigation.status = "queued"
        investigation.updated_at = datetime.now(UTC)
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="graph_identifier_collection_queued",
            details={
                "query_type": parsed_type.value,
                "query_value": normalized,
                "requested_tools": compatible,
                "queued_tools": [run.tool_id for run in queued],
                "duplicate_tools_skipped": sorted(set(compatible) - {run.tool_id for run in queued}),
            },
        )
    )
    db.commit()
    if queued:
        job_manager.start(investigation.id)
    return RedirectResponse(f"/investigations/{investigation.id}#activity", status_code=303)


@router.post("/investigations/{investigation_id}/graph/layout")
async def save_investigation_graph_layout(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> dict[str, object]:
    investigation = load_investigation(db, investigation_id)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid graph layout payload"
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid graph layout payload")
    csrf(request, str(body.get("csrf") or ""))
    key = graph_layout_setting_key(investigation_id)
    if body.get("action") == "reset":
        existing = db.get(ApplicationSetting, key)
        if existing is not None:
            db.delete(existing)
        db.add(
            AuditEvent(
                investigation_id=investigation_id,
                event_type="investigation_graph_layout_reset",
                details={},
            )
        )
        db.commit()
        return {"status": "reset", "saved_position_count": 0}

    supplied = body.get("positions")
    if not isinstance(supplied, dict) or len(supplied) > 750:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid graph positions")
    graph = build_investigation_graph(investigation)
    allowed_ids = {
        str(node.get("data", {}).get("id"))
        for node in graph["nodes"]
        if isinstance(node, dict) and isinstance(node.get("data"), dict)
    }
    positions: dict[str, dict[str, float]] = {}
    for node_id, value in supplied.items():
        if (
            not isinstance(node_id, str)
            or node_id not in allowed_ids
            or not isinstance(value, dict)
        ):
            continue
        x = value.get("x")
        y = value.get("y")
        if not isinstance(x, int | float) or not isinstance(y, int | float):
            continue
        x_value, y_value = float(x), float(y)
        if not math.isfinite(x_value) or not math.isfinite(y_value):
            continue
        if not (-100000 <= x_value <= 100000 and -100000 <= y_value <= 100000):
            continue
        positions[node_id] = {"x": round(x_value, 2), "y": round(y_value, 2)}

    setting = db.get(ApplicationSetting, key)
    value = {"positions": positions, "saved_at": datetime.now(UTC).isoformat()}
    if setting is None:
        db.add(ApplicationSetting(key=key, value=value))
    else:
        setting.value = value
        setting.updated_at = datetime.now(UTC)
    db.add(
        AuditEvent(
            investigation_id=investigation_id,
            event_type="investigation_graph_layout_saved",
            details={"saved_position_count": len(positions)},
        )
    )
    db.commit()
    return {"status": "saved", "saved_position_count": len(positions)}


@router.get("/investigations/{investigation_id}/browser", response_class=HTMLResponse)
async def managed_browser_page(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    investigation = load_investigation(db, investigation_id)
    values = await browser_page_values(investigation)
    browser_status = values["browser_status"]
    pending_job_id = getattr(browser_status, "pending_job_id", None)
    handoffs: dict[str, dict[str, Any]] = {}
    for run in investigation.browser_runs:
        setting = db.get(ApplicationSetting, f"searxng_handoff:{run.id}")
        if setting is not None and isinstance(setting.value, dict):
            handoffs[run.id] = dict(setting.value)
    values["browser_handoffs"] = handoffs
    values["capture_run"] = _capture_run_view(db, investigation)
    values["capture_candidates"] = capture_run.capture_candidates(investigation)
    values["capture_run_defaults"] = {
        "max_pages": capture_run.DEFAULT_MAX_PAGES,
        "max_pages_limit": capture_run.MAX_PAGES_LIMIT,
        "delay_min": capture_run.DEFAULT_DELAY_MIN,
        "delay_max": capture_run.DEFAULT_DELAY_MAX,
    }
    values["dork_run_active"] = investigation.id in dork_run_tasks
    values["searxng_handoff"] = handoffs.get(str(pending_job_id)) if pending_job_id else None
    return templates.TemplateResponse(
        request,
        "browser.html",
        context(request, **values),
    )


@router.post("/investigations/{investigation_id}/browser/engine")
async def switch_browser_engine_route(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    engine = str(form.get("engine") or "").strip().casefold()
    if engine not in {"chromium", "firefox"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid browser engine")
    try:
        active = await BrowserAgentClient(get_settings()).switch_engine(engine)  # type: ignore[arg-type]
    except BrowserAgentError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="browser_engine_switched",
            details={"engine": active},
        )
    )
    db.commit()
    return RedirectResponse(
        f"/investigations/{investigation.id}/browser", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/investigations/{investigation_id}/browser/profile")
async def switch_browser_profile(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    profile_name = str(form.get("profile_name") or "").strip()
    try:
        active = await BrowserAgentClient(get_settings()).switch_profile(profile_name)
    except BrowserAgentError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="browser_profile_switched",
            details={"profile": active},
        )
    )
    db.commit()
    return RedirectResponse(
        f"/investigations/{investigation.id}/browser", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/investigations/{investigation_id}/browser/profile/backup")
async def backup_browser_profile(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    profile_name = str(form.get("profile_name") or "").strip()
    password = str(form.get("password") or "")
    try:
        content, digest = await BrowserAgentClient(get_settings()).backup_profile(
            profile_name, password
        )
    except BrowserAgentError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="browser_profile_backed_up",
            details={"profile": profile_name, "sha256": digest, "size_bytes": len(content)},
        )
    )
    db.commit()
    return Response(
        content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="corvus-browser-{profile_name}.cbb"',
            "Cache-Control": "no-store",
            "X-Content-SHA256": digest,
        },
    )


@router.post("/investigations/{investigation_id}/browser/profile/restore")
async def restore_browser_profile(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    profile_name = str(form.get("profile_name") or "").strip()
    password = str(form.get("password") or "")
    upload = form.get("backup")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "encrypted browser backup is required")
    content = await upload.read(528 * 1024 * 1024)
    if not isinstance(content, bytes) or not content:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "encrypted browser backup is empty")
    try:
        restored = await BrowserAgentClient(get_settings()).restore_profile(
            profile_name, password, content
        )
    except BrowserAgentError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="browser_profile_restored",
            details={
                "profile": restored,
                "backup_sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            },
        )
    )
    db.commit()
    return RedirectResponse(
        f"/investigations/{investigation.id}/browser", status_code=status.HTTP_303_SEE_OTHER
    )


def apply_browser_capture_result(
    db: Session,
    investigation: Investigation,
    browser_run: BrowserRun,
    result: BrowserCaptureResult,
    *,
    resumed: bool,
) -> int:
    settings = get_settings()
    for reference in (
        result.screenshot_reference,
        result.html_reference,
        result.manifest_reference,
    ):
        if reference:
            resolve_evidence_path(reference, settings.browser_evidence_dir)
    browser_run.status = result.status
    browser_run.final_url = result.final_url
    browser_run.page_title = result.page_title
    browser_run.challenge_type = result.challenge_type
    browser_run.browser_version = f"{result.browser_engine.title()} {result.browser_version}"
    browser_run.observed_at = result.observed_at
    browser_run.screenshot_reference = result.screenshot_reference
    browser_run.screenshot_sha256 = result.screenshot_sha256
    browser_run.html_reference = result.html_reference
    browser_run.html_sha256 = result.html_sha256
    browser_run.manifest_reference = result.manifest_reference
    browser_run.manifest_sha256 = result.manifest_sha256
    browser_run.finished_at = datetime.now(UTC) if result.status == "completed" else None
    imported_findings = 0
    imported_locations = 0
    if result.status == "completed" and result.extraction_kind == "social_profile":
        imported_findings = import_managed_social_profile(
            db, investigation, result, browser_run.collector_id
        )
    elif result.status == "completed" and result.extraction_kind == "location_history":
        imported_locations = import_managed_location_history(
            db, investigation, result, settings.browser_evidence_dir
        )
    elif result.status == "completed" and result.extraction_kind == "search_results":
        imported_findings = import_managed_search_results(db, investigation, result)
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type=(
                "browser_manual_action_required"
                if result.status == "manual_action_required"
                else "browser_capture_completed"
            ),
            details={
                "browser_run_id": browser_run.id,
                "collector_id": browser_run.collector_id,
                "resumed": resumed,
                "challenge_type": result.challenge_type,
                "screenshot_sha256": result.screenshot_sha256,
                "html_sha256": result.html_sha256,
                "manifest_sha256": result.manifest_sha256,
                "extraction_kind": result.extraction_kind,
                "extraction_platform": result.extraction_platform,
                "authenticated_session": result.authenticated_session,
                "imported_findings": imported_findings,
                "imported_locations": imported_locations,
            },
        )
    )
    return imported_findings


def fail_browser_run(
    db: Session,
    investigation: Investigation,
    browser_run: BrowserRun,
    exc: BrowserAgentError,
) -> None:
    browser_run.status = "failed"
    browser_run.error_code = exc.code
    browser_run.error_message = str(exc)
    browser_run.finished_at = datetime.now(UTC)
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="browser_capture_failed",
            details={"browser_run_id": browser_run.id, "error_code": exc.code},
        )
    )


def _browser_search_targets(investigation: Investigation) -> tuple[str, ...]:
    if investigation.query_type not in {QueryType.USERNAME.value, QueryType.NAME.value}:
        return ()
    value = investigation.query.strip().lstrip("@")
    if not value:
        return ()
    exact = f'"{value}"'
    queries = [exact]
    if investigation.query_type == QueryType.USERNAME.value:
        queries.append(f'{exact} site:instagram.com')
    # DuckDuckGo and Startpage completed these searches in live checks on 2026-11-13; Bing and
    # Google, used here before, are the engines that most often answer with a challenge.
    return tuple(
        _search_engine_url(engine, query)
        for query in queries
        for engine in BROWSER_SEARCH_ENGINES
    )


async def _run_searxng_handoff_original(
    db: Session,
    investigation: Investigation,
    setting: ApplicationSetting,
) -> BrowserRun:
    handoff_value = dict(setting.value) if isinstance(setting.value, dict) else {}
    original_url = str(handoff_value.get("original_url") or "")
    if not original_url:
        raise HTTPException(status.HTTP_409_CONFLICT, "manual search handoff lost its original query")
    browser_run = BrowserRun(
        investigation_id=investigation.id,
        requested_url=original_url,
        collector_id=managed_browser_collector_for_url(original_url),
        status="running",
        started_at=datetime.now(UTC),
    )
    db.add(browser_run)
    db.flush()
    db.commit()
    try:
        result = await BrowserAgentClient(get_settings()).capture(
            job_id=browser_run.id,
            investigation_id=investigation.id,
            url=original_url,
            collector_id=browser_run.collector_id,
        )
        apply_browser_capture_result(db, investigation, browser_run, result, resumed=False)
    except BrowserAgentError as exc:
        fail_browser_run(db, investigation, browser_run, exc)
    # The exact search can itself be challenged. Move the handoff state to the
    # new run instead of deleting it, so Resume completes that search rather than
    # accidentally launching it a second time. Failed/cancelled exact searches
    # also retain enough state for an explicit analyst retry from Corvus.
    db.delete(setting)
    if browser_run.status == "manual_action_required":
        db.add(
            ApplicationSetting(
                key=f"searxng_handoff:{browser_run.id}",
                value={**handoff_value, "phase": "original"},
            )
        )
    elif browser_run.status != "completed":
        db.add(
            ApplicationSetting(
                key=f"searxng_handoff:{browser_run.id}",
                value={**handoff_value, "phase": "retry_available"},
            )
        )
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="searxng_manual_handoff_original_retried",
            details={
                "engine": handoff_value.get("engine"),
                "query_id": handoff_value.get("query_id"),
                "browser_run_id": browser_run.id,
                "result": browser_run.status,
            },
        )
    )
    db.commit()
    return browser_run


@router.post("/investigations/{investigation_id}/searxng-handoff/{run_id}/start")
async def start_searxng_manual_handoff(
    investigation_id: str,
    run_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    investigation = load_investigation(db, investigation_id)
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    engine = str(form.get("engine") or "").casefold()
    query_id = str(form.get("query_id") or "")
    run = db.get(ToolRun, run_id)
    if run is None or run.investigation_id != investigation.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SearXNG run not found")
    candidate = next(
        (
            item
            for item in _searxng_handoff_candidates(investigation, run)
            if item["engine"] == engine and item["query_id"] == query_id
        ),
        None,
    )
    if candidate is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "search-engine challenge is no longer pending")
    warmup_url = _search_engine_url(engine, SEARCH_ENGINE_WARMUP_QUERY)
    original_url = _search_engine_url(engine, candidate["query"])
    browser_run = BrowserRun(
        investigation_id=investigation.id,
        requested_url=warmup_url,
        collector_id=managed_browser_collector_for_url(warmup_url),
        status="running",
        started_at=datetime.now(UTC),
    )
    db.add(browser_run)
    db.flush()
    db.add(
        ApplicationSetting(
            key=f"searxng_handoff:{browser_run.id}",
            value={
                "engine": engine,
                "query_id": query_id,
                "query": candidate["query"],
                "original_url": original_url,
                "source_run_id": run.id,
                "phase": "warmup",
            },
        )
    )
    db.commit()
    try:
        result = await BrowserAgentClient(get_settings()).capture(
            job_id=browser_run.id,
            investigation_id=investigation.id,
            url=warmup_url,
            collector_id=browser_run.collector_id,
        )
        apply_browser_capture_result(db, investigation, browser_run, result, resumed=False)
    except BrowserAgentError as exc:
        fail_browser_run(db, investigation, browser_run, exc)
    db.commit()
    focus_run = browser_run
    if browser_run.status == "completed":
        setting = db.get(ApplicationSetting, f"searxng_handoff:{browser_run.id}")
        if setting is not None:
            focus_run = await _run_searxng_handoff_original(db, investigation, setting)
    return RedirectResponse(
        f"/investigations/{investigation.id}/browser#capture-{focus_run.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/searxng-handoff/{browser_run_id}/retry")
async def retry_searxng_manual_handoff_original(
    investigation_id: str,
    browser_run_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    investigation = load_investigation(db, investigation_id)
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    source_run = db.scalar(
        select(BrowserRun).where(
            BrowserRun.id == browser_run_id,
            BrowserRun.investigation_id == investigation.id,
        )
    )
    if source_run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "browser search run not found")
    setting = db.get(ApplicationSetting, f"searxng_handoff:{source_run.id}")
    if setting is None or not isinstance(setting.value, dict):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "original blocked search is no longer retained")
    if source_run.status == "manual_action_required":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the held capture is still active; resume or abandon it before retrying",
        )
    retried = await _run_searxng_handoff_original(db, investigation, setting)
    return RedirectResponse(
        f"/investigations/{investigation.id}/browser#capture-{retried.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/browser-search")
async def run_browser_search_pack(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    investigation = load_investigation(db, investigation_id)
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    settings = get_settings()
    if not settings.browser_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "managed browser support is disabled")
    targets = _browser_search_targets(investigation)
    if not targets:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "sock-browser search currently supports username and full-name investigations",
        )
    completed = 0
    manual_action_required = False
    focus_run_id: str | None = None
    for target in targets:
        browser_run = BrowserRun(
            investigation_id=investigation.id,
            requested_url=target,
            collector_id=managed_browser_collector_for_url(target),
            status="running",
            started_at=datetime.now(UTC),
        )
        db.add(browser_run)
        db.flush()
        focus_run_id = browser_run.id
        db.commit()
        try:
            result = await BrowserAgentClient(settings).capture(
                job_id=browser_run.id,
                investigation_id=investigation.id,
                url=target,
                collector_id=browser_run.collector_id,
            )
            apply_browser_capture_result(db, investigation, browser_run, result, resumed=False)
            db.commit()
            if result.status != "completed":
                manual_action_required = True
                break
            completed += 1
        except BrowserAgentError as exc:
            fail_browser_run(db, investigation, browser_run, exc)
            db.commit()
            break
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="browser_search_pack_completed",
            details={
                "requested": len(targets),
                "completed": completed,
                "manual_action_required": manual_action_required,
            },
        )
    )
    db.commit()
    destination = (
        f"/investigations/{investigation.id}/browser#capture-{focus_run_id}"
        if manual_action_required and focus_run_id
        else f"/investigations/{investigation.id}"
    )
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/investigations/{investigation_id}/browser/captures")
async def create_browser_capture(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    investigation = load_investigation(db, investigation_id)
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    settings = get_settings()
    if not settings.browser_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "managed browser support is disabled")
    target = str(form.get("url", "")).strip()
    if len(target) > 2048:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "browser target is too long")
    try:
        target = validate_browser_target(target, configured_allowed_hosts(settings))
    except BrowserAgentError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    browser_run = BrowserRun(
        investigation_id=investigation.id,
        requested_url=target,
        collector_id=managed_browser_collector_for_url(target),
        status="running",
        started_at=datetime.now(UTC),
    )
    db.add(browser_run)
    db.flush()
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="browser_capture_started",
            details={"browser_run_id": browser_run.id, "collector_id": browser_run.collector_id},
        )
    )
    db.commit()
    try:
        result = await BrowserAgentClient(settings).capture(
            job_id=browser_run.id,
            investigation_id=investigation.id,
            url=target,
            collector_id=browser_run.collector_id,
        )
        apply_browser_capture_result(db, investigation, browser_run, result, resumed=False)
    except BrowserAgentError as exc:
        fail_browser_run(db, investigation, browser_run, exc)
    db.commit()
    return RedirectResponse(
        f"/investigations/{investigation.id}/browser#capture-{browser_run.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/browser/captures/{browser_run_id}/retry")
async def retry_browser_capture(
    investigation_id: str,
    browser_run_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    investigation = load_investigation(db, investigation_id)
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    source_run = db.scalar(
        select(BrowserRun).where(
            BrowserRun.id == browser_run_id,
            BrowserRun.investigation_id == investigation.id,
        )
    )
    if source_run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "browser capture not found")
    if source_run.status == "manual_action_required":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the held capture is still active; resume or abandon it before retrying",
        )
    settings = get_settings()
    try:
        target = validate_browser_target(
            source_run.requested_url, configured_allowed_hosts(settings)
        )
    except BrowserAgentError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    retried = BrowserRun(
        investigation_id=investigation.id,
        requested_url=target,
        collector_id=managed_browser_collector_for_url(target),
        status="running",
        started_at=datetime.now(UTC),
    )
    db.add(retried)
    db.flush()
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="browser_capture_retried",
            details={"source_browser_run_id": source_run.id, "browser_run_id": retried.id},
        )
    )
    db.commit()
    try:
        result = await BrowserAgentClient(settings).capture(
            job_id=retried.id,
            investigation_id=investigation.id,
            url=target,
            collector_id=retried.collector_id,
        )
        apply_browser_capture_result(db, investigation, retried, result, resumed=False)
    except BrowserAgentError as exc:
        fail_browser_run(db, investigation, retried, exc)
    db.commit()
    return RedirectResponse(
        f"/investigations/{investigation.id}/browser#capture-{retried.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/browser/captures/{browser_run_id}/resume")
async def resume_browser_capture(
    investigation_id: str,
    browser_run_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    investigation = load_investigation(db, investigation_id)
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    browser_run = db.scalar(
        select(BrowserRun).where(
            BrowserRun.id == browser_run_id,
            BrowserRun.investigation_id == investigation.id,
        )
    )
    if browser_run is None or browser_run.status != "manual_action_required":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pending browser capture not found")
    handoff = db.get(ApplicationSetting, f"searxng_handoff:{browser_run.id}")
    handoff_phase = (
        str(handoff.value.get("phase") or "warmup")
        if handoff is not None and isinstance(handoff.value, dict)
        else None
    )
    try:
        result = await BrowserAgentClient(get_settings()).resume(
            job_id=browser_run.id,
            investigation_id=investigation.id,
        )
        apply_browser_capture_result(db, investigation, browser_run, result, resumed=True)
    except BrowserAgentError as exc:
        fail_browser_run(db, investigation, browser_run, exc)
    db.commit()
    if browser_run.status == "completed" and handoff is not None:
        if handoff_phase == "warmup":
            await _run_searxng_handoff_original(db, investigation, handoff)
        else:
            # This held page was already the exact original search. Completing
            # it imported the results through apply_browser_capture_result(); do
            # not launch the same query a second time.
            db.delete(handoff)
            db.add(
                AuditEvent(
                    investigation_id=investigation.id,
                    event_type="searxng_manual_handoff_completed",
                    details={"browser_run_id": browser_run.id, "phase": handoff_phase},
                )
            )
            db.commit()
    return RedirectResponse(
        f"/investigations/{investigation.id}/browser#capture-{browser_run.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/browser/captures/{browser_run_id}/abandon")
async def abandon_browser_capture(
    investigation_id: str,
    browser_run_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    investigation = load_investigation(db, investigation_id)
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    browser_run = db.scalar(
        select(BrowserRun).where(
            BrowserRun.id == browser_run_id,
            BrowserRun.investigation_id == investigation.id,
        )
    )
    if browser_run is None or browser_run.status != "manual_action_required":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pending browser capture not found")
    try:
        await BrowserAgentClient(get_settings()).abandon(
            job_id=browser_run.id,
            investigation_id=investigation.id,
        )
    except BrowserAgentError as exc:
        if exc.code != "pending_job_missing":
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    browser_run.status = "cancelled"
    browser_run.finished_at = datetime.now(UTC)
    browser_run.error_message = "analyst abandoned pending browser challenge"
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="browser_capture_abandoned",
            details={
                "browser_run_id": browser_run.id,
                "collector_id": browser_run.collector_id,
                "challenge_type": browser_run.challenge_type,
            },
        )
    )
    db.commit()
    return RedirectResponse(
        f"/investigations/{investigation.id}/browser", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/investigations/{investigation_id}/browser/captures/{browser_run_id}/screenshot")
def browser_capture_screenshot(
    investigation_id: str,
    browser_run_id: str,
    db: Session = Depends(get_db),
) -> Response:
    browser_run = db.scalar(
        select(BrowserRun).where(
            BrowserRun.id == browser_run_id,
            BrowserRun.investigation_id == investigation_id,
        )
    )
    if not browser_run or not browser_run.screenshot_reference:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "browser screenshot not found")
    try:
        path = resolve_evidence_path(
            browser_run.screenshot_reference, get_settings().browser_evidence_dir
        )
    except BrowserAgentError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "browser screenshot not found") from exc
    if path.suffix.lower() != ".png" or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "browser screenshot not found")
    return FileResponse(path, media_type="image/png", filename=f"browser-{browser_run.id}.png")


def location_page_values(
    db: Session,
    investigation: Investigation,
    *,
    collection_result: dict[str, object] | None = None,
    collection_error: str | None = None,
) -> dict[str, object]:
    observations = list(
        db.scalars(
            select(LocationObservation)
            .where(LocationObservation.investigation_id == investigation.id)
            .order_by(LocationObservation.created_at.desc())
        )
    )
    candidates = sorted(
        {
            finding.canonical_url
            for finding in investigation.findings
            if finding.canonical_url
            and finding.status in {"found", "likely"}
            and policy_for_url(finding.canonical_url) is not None
        }
    )
    return {
        "investigation": investigation,
        "observations": observations,
        "clusters": build_location_clusters(observations),
        "evidence_types": list(LocationEvidenceType),
        "collection_candidates": candidates,
        "collection_result": collection_result,
        "collection_error": collection_error,
    }


@router.post("/investigations/{investigation_id}/analyze-retained-artifacts")
async def analyze_case_retained_artifacts(
    investigation_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    root = get_settings().data_dir / "raw" / investigation.id
    analyses = analyze_retained_artifacts(root)
    now = datetime.now(UTC)
    run = ToolRun(
        investigation_id=investigation.id,
        tool_id="local_artifact_analysis",
        display_name="Retained Artifact Analysis",
        status="completed",
        tool_version="1",
        timeout_seconds=30,
        query_value=investigation.query,
        query_type=investigation.query_type,
        started_at=now,
        finished_at=now,
        duration_ms=0,
        raw_output_reference=str(root),
    )
    db.add(run)
    db.flush()
    existing_ids = {
        value
        for value in db.scalars(
            select(Finding.source_record_id).where(
                Finding.investigation_id == investigation.id,
                Finding.source_tool == "local_artifact_analysis",
            )
        )
        if value
    }
    added = 0
    gps_added = 0
    for artifact in analyses:
        artifact_ref = f"{artifact.sha256}:file"
        if artifact_ref not in existing_ids:
            db.add(
                Finding(
                    investigation_id=investigation.id,
                    tool_run_id=run.id,
                    query_type=investigation.query_type,
                    queried_value=investigation.query,
                    finding_type="retained_file_metadata",
                    platform="Local retained artifact",
                    value=artifact.relative_path,
                    status="found",
                    confidence=1.0,
                    title=artifact.relative_path,
                    description=f"{artifact.media_type} · {artifact.size_bytes} bytes",
                    extra={
                        "file_sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                        "media_type": artifact.media_type,
                        "metadata": artifact.metadata,
                    },
                    source_tool="local_artifact_analysis",
                    source_tools=["local_artifact_analysis"],
                    source_record_id=artifact_ref,
                    observed_at=now,
                    raw_output_reference=artifact.relative_path,
                )
            )
            existing_ids.add(artifact_ref)
            added += 1
        for email in artifact.emails:
            record_id = f"{artifact.sha256}:email:{email.casefold()}"
            if record_id in existing_ids:
                continue
            db.add(
                Finding(
                    investigation_id=investigation.id,
                    tool_run_id=run.id,
                    query_type=investigation.query_type,
                    queried_value=investigation.query,
                    finding_type="email_candidate",
                    platform="Local retained artifact",
                    value=email,
                    status="likely",
                    confidence=0.7,
                    title=email,
                    description=f"Extracted locally from {artifact.relative_path}; association requires analyst review.",
                    extra={
                        "file_sha256": artifact.sha256,
                        "source_path": artifact.relative_path,
                        "extraction": "local_email_regex",
                    },
                    source_tool="local_artifact_analysis",
                    source_tools=["local_artifact_analysis"],
                    source_record_id=record_id,
                    observed_at=now,
                    raw_output_reference=artifact.relative_path,
                )
            )
            existing_ids.add(record_id)
            added += 1
        for company in artifact.companies:
            record_id = f"{artifact.sha256}:company:{hashlib.sha256(company.casefold().encode()).hexdigest()[:20]}"
            if record_id in existing_ids:
                continue
            db.add(
                Finding(
                    investigation_id=investigation.id,
                    tool_run_id=run.id,
                    query_type=investigation.query_type,
                    queried_value=investigation.query,
                    finding_type="company_candidate",
                    platform="Local retained artifact",
                    value=company,
                    status="unknown",
                    confidence=0.5,
                    title=company,
                    description=f"Organisation-name candidate extracted locally from {artifact.relative_path}; confirm before registry pivoting.",
                    extra={
                        "file_sha256": artifact.sha256,
                        "source_path": artifact.relative_path,
                        "extraction": "local_company_pattern",
                    },
                    source_tool="local_artifact_analysis",
                    source_tools=["local_artifact_analysis"],
                    source_record_id=record_id,
                    observed_at=now,
                    raw_output_reference=artifact.relative_path,
                )
            )
            existing_ids.add(record_id)
            added += 1
        latitude = artifact.metadata.get("gps_latitude")
        longitude = artifact.metadata.get("gps_longitude")
        if isinstance(latitude, int | float) and isinstance(longitude, int | float):
            add_location_observation(
                db,
                investigation,
                LocationObservationInput(
                    place_name=f"Photo GPS · {artifact.relative_path}",
                    latitude=float(latitude),
                    longitude=float(longitude),
                    precision_radius_m=25,
                    evidence_type=LocationEvidenceType.PHOTO,
                    source_platform="Local retained artifact",
                    confidence=0.8,
                    raw_evidence={
                        "file_sha256": artifact.sha256,
                        "source_path": artifact.relative_path,
                        "metadata": artifact.metadata,
                    },
                ),
                observed_at=now,
            )
            gps_added += 1
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="retained_artifact_analysis_completed",
            details={
                "files_analyzed": len(analyses),
                "findings_added": added,
                "gps_observations_processed": gps_added,
                "network_egress": False,
            },
        )
    )
    db.commit()
    return RedirectResponse(
        f"/investigations/{investigation.id}#findings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/infrastructure-enrichment")
async def enrich_case_infrastructure(
    investigation_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    provider = str(form.get("provider", "")).strip().casefold()
    resource = str(form.get("resource", "")).strip()
    try:
        result = await enrich_infrastructure(
            provider,
            resource,
            abuseipdb_api_key=get_abuseipdb_api_key(db),
        )
    except InfrastructureEnrichmentError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "infrastructure registry lookup failed") from exc
    now = datetime.now(UTC)
    tool_id = {"ripe": "ripe_stat", "arin": "arin_rdap", "abuseipdb": "abuseipdb"}.get(provider)
    if tool_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unsupported infrastructure provider")
    run = ToolRun(
        investigation_id=investigation.id,
        tool_id=tool_id,
        display_name=result.provider,
        status="completed",
        tool_version="public-api",
        timeout_seconds=15,
        query_value=result.resource,
        query_type=investigation.query_type,
        started_at=now,
        finished_at=now,
        duration_ms=0,
    )
    db.add(run)
    db.flush()
    record_id = f"{tool_id}:{result.resource.casefold()}"
    existing = db.scalar(
        select(Finding).where(
            Finding.investigation_id == investigation.id,
            Finding.source_record_id == record_id,
        )
    )
    if existing is None:
        db.add(
            Finding(
                investigation_id=investigation.id,
                tool_run_id=run.id,
                query_type=investigation.query_type,
                queried_value=investigation.query,
                finding_type=result.finding_type,
                platform=result.provider,
                value=result.resource,
                canonical_url=result.canonical_url,
                display_url=result.canonical_url,
                status="found",
                confidence=0.9,
                title=result.title,
                description=result.description,
                extra=result.metadata,
                source_tool=tool_id,
                source_tools=[tool_id],
                source_record_id=record_id,
                observed_at=now,
            )
        )
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="infrastructure_enrichment_completed",
            details={
                "provider": result.provider,
                "resource": result.resource,
                "source_tool": tool_id,
                "deduplicated": existing is not None,
            },
        )
    )
    db.commit()
    return RedirectResponse(
        f"/investigations/{investigation.id}#findings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/vies")
async def validate_case_vat_number(
    investigation_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    vat_number = str(form.get("vat_number", "")).strip()
    try:
        result = await check_vat_number(vat_number)
    except VIESError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    now = datetime.now(UTC)
    raw_dir = get_settings().data_dir / "raw" / investigation.id / "vies"
    raw_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw_path = raw_dir / f"{result.vat_number}.json"
    raw_path.write_text(json.dumps(result.raw, indent=2, sort_keys=True), encoding="utf-8")
    raw_path.chmod(0o600)
    raw_reference = str(raw_path.relative_to(get_settings().data_dir))
    run = ToolRun(
        investigation_id=investigation.id,
        tool_id="vies",
        display_name="EU VIES VAT Validation",
        status="completed",
        tool_version="rest-api",
        timeout_seconds=20,
        query_value=result.vat_number,
        query_type=investigation.query_type,
        started_at=now,
        finished_at=now,
        duration_ms=0,
        raw_output_reference=raw_reference,
    )
    db.add(run)
    db.flush()
    record_id = f"vies:{result.vat_number}"
    existing = db.scalar(select(Finding).where(Finding.investigation_id == investigation.id, Finding.source_record_id == record_id))
    values = {
        "tool_run_id": run.id,
        "query_type": investigation.query_type,
        "queried_value": investigation.query,
        "finding_type": "vat_validation",
        "platform": "EU VIES",
        "value": result.vat_number,
        "canonical_url": "https://ec.europa.eu/taxation_customs/vies/",
        "display_url": "https://ec.europa.eu/taxation_customs/vies/",
        "status": "found" if result.valid else "not_found",
        "confidence": 0.99,
        "title": f"VIES VAT {'valid' if result.valid else 'invalid'} · {result.vat_number}",
        "description": result.name or result.address,
        "extra": {
            "vat_number": result.vat_number,
            "country_code": result.country_code,
            "valid": result.valid,
            "request_date": result.request_date,
            "request_identifier": result.request_identifier,
            "registered_name": result.name,
            "registered_address": result.address,
            "trader_company_type": result.trader_company_type,
            "source_scope": "official_eu_vies_validation",
        },
        "source_tool": "vies",
        "source_tools": ["vies"],
        "source_record_id": record_id,
        "observed_at": now,
        "raw_output_reference": raw_reference,
    }
    if existing is None:
        db.add(Finding(investigation_id=investigation.id, **values))
    else:
        for key, value in values.items():
            setattr(existing, key, value)
    db.add(AuditEvent(investigation_id=investigation.id, event_type="vies_validation_completed", details={"vat_number": result.vat_number, "valid": result.valid}))
    db.commit()
    return RedirectResponse(f"/investigations/{investigation.id}#findings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/investigations/{investigation_id}/video-metadata")
async def collect_case_video_metadata(
    investigation_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    video_url = str(form.get("video_url", "")).strip()
    query_type = QueryType(investigation.query_type)
    adapter = YtDlpMetadataAdapter()
    output_dir = get_settings().data_dir / "raw" / investigation.id / "yt-dlp" / hashlib.sha256(video_url.encode()).hexdigest()[:20]
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        command = adapter.build_command(video_url, query_type, output_dir, adapter.default_timeout)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    started = datetime.now(UTC)
    result = await adapter.execute(command, output_dir, adapter.default_timeout, video_url)
    finished = datetime.now(UTC)
    if result.returncode != 0:
        detail = result.error_message or "yt-dlp metadata extraction failed"
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail)
    records = adapter.parse_results(result, output_dir)
    raw_path = output_dir / "yt-dlp-metadata.json"
    raw_reference = str(raw_path.relative_to(get_settings().data_dir)) if raw_path.is_file() else None
    findings = adapter.normalise_results(records, video_url, query_type, raw_reference)
    if not findings:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "yt-dlp returned no usable metadata")
    run = ToolRun(
        investigation_id=investigation.id,
        tool_id=adapter.tool_id,
        display_name=adapter.display_name,
        status="completed",
        tool_version="pinned-container",
        timeout_seconds=adapter.default_timeout,
        query_value=video_url,
        query_type=investigation.query_type,
        started_at=started,
        finished_at=finished,
        duration_ms=max(0, int((finished - started).total_seconds() * 1000)),
        raw_output_reference=raw_reference,
    )
    db.add(run)
    db.flush()
    for item in findings:
        existing = db.scalar(select(Finding).where(Finding.investigation_id == investigation.id, Finding.source_record_id == item.source_record_id))
        if existing is not None:
            existing.tool_run_id = run.id
            existing.value = item.value
            existing.canonical_url = item.canonical_url
            existing.display_url = item.display_url
            existing.title = item.title
            existing.description = item.description
            existing.extra = item.metadata
            existing.observed_at = item.observed_at
            existing.raw_output_reference = raw_reference
            continue
        db.add(Finding(
            investigation_id=investigation.id,
            tool_run_id=run.id,
            query_type=investigation.query_type,
            queried_value=investigation.query,
            finding_type=item.finding_type,
            platform=item.platform,
            value=item.value,
            canonical_url=item.canonical_url,
            display_url=item.display_url,
            status=item.status.value,
            confidence=item.confidence,
            title=item.title,
            description=item.description,
            extra=item.metadata,
            source_tool=adapter.tool_id,
            source_tools=[adapter.tool_id],
            source_record_id=item.source_record_id,
            observed_at=item.observed_at,
            raw_output_reference=raw_reference,
        ))
    db.add(AuditEvent(investigation_id=investigation.id, event_type="video_metadata_collected", details={"source_tool": adapter.tool_id, "url_host": urlsplit(video_url).hostname}))
    db.commit()
    return RedirectResponse(f"/investigations/{investigation.id}#findings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/investigations/{investigation_id}/tiktok/evidence")
async def acquire_case_tiktok_evidence(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    try:
        video_url = validate_tiktok_post(str(form.get("video_url", "")))
    except (ValueError, TikTokEvidenceError) as exc:
        raise HTTPException(422, str(exc)) from exc
    acquisition_id = str(uuid4())
    data_dir = get_settings().data_dir
    output_dir = data_dir / "raw" / investigation.id / "tiktok" / acquisition_id
    output_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    adapter = YtDlpMetadataAdapter()
    command = adapter.build_tiktok_acquisition_command(video_url)
    started = datetime.now(UTC)
    result = await adapter.execute(command, output_dir, 120, video_url)
    finished = datetime.now(UTC)
    if result.returncode != 0:
        raise HTTPException(502, result.error_message or "TikTok media acquisition failed; no account absence is inferred")
    try:
        health = await adapter.healthcheck()
        manifest = finalize_acquisition(
            output_dir, video_url, version=health.version or "unknown", acquired_at=finished
        )
    except (OSError, ValueError, TikTokEvidenceError) as exc:
        raise HTTPException(502, f"TikTok acquisition could not be verified: {exc}") from exc
    metadata = manifest["metadata"]
    raw_reference = str((output_dir / "acquisition-manifest.json").relative_to(data_dir))
    run = ToolRun(
        investigation_id=investigation.id, tool_id="yt_dlp_metadata",
        display_name="TikTok public video acquisition", status="completed",
        tool_version=manifest["collector_version"], timeout_seconds=120,
        query_value=video_url, query_type=investigation.query_type,
        started_at=started, finished_at=finished,
        duration_ms=max(0, int((finished - started).total_seconds() * 1000)),
        raw_output_reference=raw_reference,
    )
    db.add(run)
    db.flush()
    db.add(Finding(
        investigation_id=investigation.id, tool_run_id=run.id,
        query_type=investigation.query_type, queried_value=investigation.query,
        finding_type="tiktok_video_evidence", platform="TikTok",
        value=str(metadata["id"]), canonical_url=video_url, display_url=video_url,
        status="found", confidence=0.95,
        title=str(metadata.get("title") or metadata["id"])[:500],
        description=str(metadata.get("description") or "")[:2000] or None,
        extra={**metadata, "acquisition_id": acquisition_id, "media_sha256": manifest["files"][0]["sha256"],
               "media_size_bytes": manifest["files"][0]["size_bytes"],
               "acquired_at_utc": manifest["acquired_at_utc"]},
        source_tool="yt_dlp_metadata", source_tools=["yt_dlp_metadata"],
        source_record_id=f"tiktok_acquisition:{acquisition_id}",
        observed_at=finished, raw_output_reference=raw_reference,
    ))
    db.add(AuditEvent(
        investigation_id=investigation.id, event_type="tiktok_media_evidence_acquired",
        details={"acquisition_id": acquisition_id, "source_url": video_url,
                 "media_sha256": manifest["files"][0]["sha256"],
                 "media_size_bytes": manifest["files"][0]["size_bytes"]},
    ))
    db.commit()
    return RedirectResponse(f"/investigations/{investigation.id}#findings", status_code=303)


@router.get("/investigations/{investigation_id}/tiktok/evidence/{acquisition_id}.zip")
def download_case_tiktok_evidence(
    investigation_id: str, acquisition_id: str, db: Session = Depends(get_db)
) -> Response:
    investigation = load_investigation(db, investigation_id)
    if not re.fullmatch(r"[0-9a-f-]{36}", acquisition_id):
        raise HTTPException(404, "acquisition not found")
    recorded = db.scalar(select(Finding).where(
        Finding.investigation_id == investigation.id,
        Finding.source_record_id == f"tiktok_acquisition:{acquisition_id}",
    ))
    if recorded is None:
        raise HTTPException(404, "acquisition not found")
    output_dir = get_settings().data_dir / "raw" / investigation.id / "tiktok" / acquisition_id
    try:
        bundle = build_tiktok_evidence_zip(output_dir)
    except (OSError, TikTokEvidenceError) as exc:
        raise HTTPException(409, f"TikTok evidence integrity verification failed: {exc}") from exc
    db.add(AuditEvent(
        investigation_id=investigation.id, event_type="tiktok_evidence_bundle_downloaded",
        details={"acquisition_id": acquisition_id, "sha256": hashlib.sha256(bundle).hexdigest()},
    ))
    db.commit()
    return Response(
        bundle, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="corvus-tiktok-{acquisition_id}.zip"',
                 "X-Content-SHA256": hashlib.sha256(bundle).hexdigest()},
    )


@router.post("/investigations/{investigation_id}/telegram/import")
async def import_telegram_export(
    investigation_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    upload = form.get("telegram_export")
    if not isinstance(upload, UploadFile):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Telegram JSON export is required")
    raw = await upload.read(MAX_EXPORT_BYTES + 1)
    try:
        parsed = parse_telegram_export(raw)
    except TelegramImportError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    root = get_settings().data_dir / "raw" / investigation.id / "telegram-import"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = root / f"{parsed.sha256}.json"
    if not destination.exists():
        destination.write_bytes(raw)
        destination.chmod(0o600)
    now = datetime.now(UTC)
    run = ToolRun(
        investigation_id=investigation.id,
        tool_id="telegram_export_import",
        display_name="Telegram Desktop Export",
        status="completed",
        tool_version="desktop-json",
        timeout_seconds=30,
        query_value=parsed.chat_name,
        query_type=investigation.query_type,
        started_at=now,
        finished_at=now,
        duration_ms=0,
        raw_output_reference=str(destination.relative_to(get_settings().data_dir)),
    )
    db.add(run)
    db.flush()
    existing_ids = {
        value
        for value in db.scalars(
            select(Finding.source_record_id).where(
                Finding.investigation_id == investigation.id,
                Finding.source_tool == "telegram_export_import",
            )
        )
        if value
    }
    chat_record_id = f"{parsed.sha256}:chat"
    added = 0
    if chat_record_id not in existing_ids:
        db.add(
            Finding(
                investigation_id=investigation.id,
                tool_run_id=run.id,
                query_type=investigation.query_type,
                queried_value=investigation.query,
                finding_type="telegram_chat_export",
                platform="Telegram",
                value=parsed.chat_name,
                status="found",
                confidence=1.0,
                title=parsed.chat_name,
                description=(f"Telegram Desktop export · {parsed.chat_type or 'chat'} · {len(parsed.messages)} retained messages"),
                extra={
                    "telegram_entity_kind": "chat_export",
                    "telegram_chat_type": parsed.chat_type,
                    "telegram_chat_id": parsed.chat_id,
                    "telegram_export_sha256": parsed.sha256,
                    "telegram_message_count": len(parsed.messages),
                    "telegram_export_truncated": parsed.truncated,
                },
                source_tool="telegram_export_import",
                source_tools=["telegram_export_import"],
                source_record_id=chat_record_id,
                observed_at=now,
                raw_output_reference=str(destination.relative_to(get_settings().data_dir)),
            )
        )
        existing_ids.add(chat_record_id)
        added += 1
    for message in parsed.messages:
        record_id = f"{parsed.sha256}:message:{message.message_id}"
        if record_id in existing_ids:
            continue
        db.add(
            Finding(
                investigation_id=investigation.id,
                tool_run_id=run.id,
                query_type=investigation.query_type,
                queried_value=investigation.query,
                finding_type="telegram_message",
                platform="Telegram",
                value=message.sender_name or message.sender_id or f"Message {message.message_id}",
                status="found",
                confidence=1.0,
                title=f"Telegram message {message.message_id}",
                description=message.text[:2000] or None,
                extra={
                    **message_metadata(message),
                    "telegram_chat_name": parsed.chat_name,
                    "telegram_chat_id": parsed.chat_id,
                    "telegram_export_sha256": parsed.sha256,
                },
                source_tool="telegram_export_import",
                source_tools=["telegram_export_import"],
                source_record_id=record_id,
                observed_at=now,
                raw_output_reference=str(destination.relative_to(get_settings().data_dir)),
            )
        )
        existing_ids.add(record_id)
        added += 1
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="telegram_export_imported",
            details={
                "chat_name": parsed.chat_name,
                "chat_type": parsed.chat_type,
                "chat_id": parsed.chat_id,
                "export_sha256": parsed.sha256,
                "messages_parsed": len(parsed.messages),
                "truncated": parsed.truncated,
                "findings_added": added,
                "network_egress": False,
            },
        )
    )
    db.commit()
    return RedirectResponse(
        f"/investigations/{investigation.id}#findings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _store_discord_import(
    db: Session,
    investigation: Investigation,
    parsed: Any,
    *,
    tool_id: str,
    display_name: str,
    raw_reference: str,
    audit_event_type: str,
) -> int:
    """Persist one parsed Discord import (chat export or SQLite archive) as findings."""
    now = datetime.now(UTC)
    channel_label = parsed.channel_name or parsed.guild_name or "Discord import"
    run = ToolRun(
        investigation_id=investigation.id,
        tool_id=tool_id,
        display_name=display_name,
        status="completed",
        tool_version=parsed.source,
        timeout_seconds=30,
        query_value=channel_label,
        query_type=investigation.query_type,
        started_at=now,
        finished_at=now,
        duration_ms=0,
        raw_output_reference=raw_reference,
    )
    db.add(run)
    db.flush()
    existing_ids = {
        value
        for value in db.scalars(
            select(Finding.source_record_id).where(
                Finding.investigation_id == investigation.id,
                Finding.source_tool == tool_id,
            )
        )
        if value
    }
    chat_record_id = f"{parsed.sha256}:chat"
    added = 0
    if chat_record_id not in existing_ids:
        db.add(
            Finding(
                investigation_id=investigation.id,
                tool_run_id=run.id,
                query_type=investigation.query_type,
                queried_value=investigation.query,
                finding_type="discord_chat_export",
                platform="Discord",
                value=channel_label,
                status="found",
                confidence=1.0,
                title=channel_label,
                description=(
                    f"{display_name} · {parsed.channel_type or 'channel'} · "
                    f"{len(parsed.messages)} retained messages"
                ),
                extra={
                    "discord_entity_kind": "chat_export",
                    "discord_guild_id": parsed.guild_id,
                    "discord_guild_name": parsed.guild_name,
                    "discord_channel_id": parsed.channel_id,
                    "discord_channel_type": parsed.channel_type,
                    "discord_export_sha256": parsed.sha256,
                    "discord_message_count": len(parsed.messages),
                    "discord_export_truncated": parsed.truncated,
                    "discord_import_source": parsed.source,
                },
                source_tool=tool_id,
                source_tools=[tool_id],
                source_record_id=chat_record_id,
                observed_at=now,
                raw_output_reference=raw_reference,
            )
        )
        existing_ids.add(chat_record_id)
        added += 1
    seen_users: set[str] = set()
    for message in parsed.messages:
        if not message.author_id or message.author_id in seen_users:
            continue
        seen_users.add(message.author_id)
        user_record_id = f"{parsed.sha256}:user:{message.author_id}"
        if user_record_id in existing_ids:
            continue
        display_name = message.author_name or message.author_username or message.author_id
        db.add(
            Finding(
                investigation_id=investigation.id,
                tool_run_id=run.id,
                query_type="username",
                queried_value=message.author_username or message.author_id,
                finding_type="profile",
                platform="Discord",
                value=message.author_username or display_name,
                canonical_url=f"https://discord.com/users/{message.author_id}",
                status="found",
                confidence=1.0,
                title=display_name,
                description=(
                    f"Discord user observed in {parsed.guild_name or 'imported chat'}"
                    + (f" / #{parsed.channel_name}" if parsed.channel_name else "")
                ),
                extra={
                    "profile": {
                        "display_name": message.author_name or message.author_username,
                        "account_id": message.author_id,
                        "avatar_url": message.author_avatar_url,
                    },
                    "discord_entity_kind": "user",
                    "discord_user_id": message.author_id,
                    "discord_username": message.author_username,
                    "discord_display_name": message.author_name,
                    "discord_is_bot": message.author_is_bot,
                    "discord_guild_id": parsed.guild_id,
                    "discord_guild_name": parsed.guild_name,
                    "discord_channel_id": parsed.channel_id,
                    "discord_channel_name": parsed.channel_name,
                    "discord_export_sha256": parsed.sha256,
                    "discord_import_source": parsed.source,
                },
                source_tool=tool_id,
                source_tools=[tool_id],
                source_record_id=user_record_id,
                observed_at=now,
                raw_output_reference=raw_reference,
            )
        )
        existing_ids.add(user_record_id)
        added += 1
    for message in parsed.messages:
        record_id = f"{parsed.sha256}:message:{message.message_id}"
        if record_id in existing_ids:
            continue
        db.add(
            Finding(
                investigation_id=investigation.id,
                tool_run_id=run.id,
                query_type=investigation.query_type,
                queried_value=investigation.query,
                finding_type="discord_message",
                platform="Discord",
                value=message.author_name or message.author_id or f"Message {message.message_id}",
                status="found",
                confidence=1.0,
                title=f"Discord message {message.message_id}",
                description=message.content[:2000] or None,
                extra={
                    **discord_message_metadata(message),
                    "discord_guild_id": parsed.guild_id,
                    "discord_guild_name": parsed.guild_name,
                    "discord_channel_id": parsed.channel_id,
                    "discord_channel_name": parsed.channel_name,
                    "discord_export_sha256": parsed.sha256,
                    "discord_import_source": parsed.source,
                },
                source_tool=tool_id,
                source_tools=[tool_id],
                source_record_id=record_id,
                observed_at=now,
                raw_output_reference=raw_reference,
            )
        )
        existing_ids.add(record_id)
        added += 1
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type=audit_event_type,
            details={
                "guild_name": parsed.guild_name,
                "channel_name": parsed.channel_name,
                "export_sha256": parsed.sha256,
                "messages_parsed": len(parsed.messages),
                "truncated": parsed.truncated,
                "findings_added": added,
                "network_egress": False,
            },
        )
    )
    return added


@router.post("/investigations/{investigation_id}/discord/import")
async def import_discord_export(
    investigation_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    upload = form.get("discord_export")
    if not isinstance(upload, UploadFile):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Discord JSON export is required")
    raw = await upload.read(DISCORD_MAX_EXPORT_BYTES + 1)
    try:
        parsed = parse_discord_chat_export(raw)
    except DiscordImportError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    root = get_settings().data_dir / "raw" / investigation.id / "discord-import"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = root / f"{parsed.sha256}.json"
    if not destination.exists():
        destination.write_bytes(raw)
        destination.chmod(0o600)
    added = _store_discord_import(
        db,
        investigation,
        parsed,
        tool_id="discord_export_import",
        display_name="Discord Chat Export",
        raw_reference=str(destination.relative_to(get_settings().data_dir)),
        audit_event_type="discord_export_imported",
    )
    _ = added
    db.commit()
    return RedirectResponse(
        f"/investigations/{investigation.id}#findings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/discord/import-sqlite")
async def import_discord_sqlite(
    investigation_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    upload = form.get("discord_archive")
    if not isinstance(upload, UploadFile):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Discord SQLite archive is required")
    raw = await upload.read(DISCORD_MAX_SQLITE_BYTES + 1)
    root = get_settings().data_dir / "raw" / investigation.id / "discord-import"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(dir=root, suffix=".sqlite-tmp")
    os.close(descriptor)
    tmp_path = Path(tmp_name)
    tmp_path.chmod(0o600)
    try:
        tmp_path.write_bytes(raw)
        parsed = parse_discord_sqlite_export(raw, tmp_path)
    except DiscordImportError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    destination = root / f"{parsed.sha256}.sqlite"
    if not destination.exists():
        tmp_path.rename(destination)
    else:
        tmp_path.unlink(missing_ok=True)
    added = _store_discord_import(
        db,
        investigation,
        parsed,
        tool_id="discord_sqlite_import",
        display_name="Discord SQLite Archive (discrawl)",
        raw_reference=str(destination.relative_to(get_settings().data_dir)),
        audit_event_type="discord_sqlite_imported",
    )
    _ = added
    db.commit()
    return RedirectResponse(
        f"/investigations/{investigation.id}#findings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/investigations/{investigation_id}/toolkit/{recommendation_key}")
def launch_toolkit_recommendation(
    investigation_id: str,
    recommendation_key: str,
    db: Session = Depends(get_db),
) -> Response:
    investigation = load_investigation(db, investigation_id)
    recommendation = find_toolkit_recommendation(investigation, recommendation_key)
    if recommendation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "toolkit recommendation not found")
    try:
        decision = validate_external_egress(
            recommendation.destination_url,
            payload_kind=EgressPayloadKind.URL_REFERENCE,
            allowed_hosts=frozenset({recommendation.destination_host}),
        )
    except EgressPolicyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="toolkit_recommendation_launched",
            details={
                "recommendation_key": recommendation.key,
                "label": recommendation.label,
                "category": recommendation.category,
                "destination_host": recommendation.destination_host,
                "payload_kind": decision.payload_kind.value,
                "sends_case_value": recommendation.sends_case_value,
                "finding_id": recommendation.finding_id,
                "search_value": recommendation.search_value,
                "jurisdiction": recommendation.jurisdiction,
                "provenance": recommendation.provenance,
            },
        )
    )
    db.commit()
    return RedirectResponse(
        recommendation.destination_url,
        status_code=status.HTTP_302_FOUND,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post("/investigations/{investigation_id}/pivots/name-handle")
async def follow_name_handle_seed(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    if investigation.query_type != QueryType.NAME.value:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "name-handle seeds apply only to name investigations",
        )
    if investigation.status in {"queued", "running"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "wait for the active investigation run to finish before starting a manual seed",
        )
    requested = str(form.get("username", "")).casefold()
    candidate = next(
        (
            item
            for item in generate_handle_candidates(investigation.query)
            if item.username.casefold() == requested
        ),
        None,
    )
    if candidate is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid generated handle seed")
    queued = queue_seeded_username_sweep(db, investigation, candidate)
    if queued:
        investigation.status = "queued"
        investigation.updated_at = datetime.now(UTC)
        db.commit()
        job_manager.start(investigation.id)
    return RedirectResponse(
        f"/investigations/{investigation_id}#analysis",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/pivots/username")
async def follow_username_pivot(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    if investigation.status in {"queued", "running"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "wait for the active investigation run to finish before starting a manual pivot",
        )
    username = str(form.get("username", ""))
    source_finding_id = str(form.get("source_finding_id", ""))
    candidate = find_username_pivot(investigation, username, source_finding_id)
    if candidate is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid username pivot")
    queued = queue_username_pivot(db, investigation, candidate, automatic=False)
    if queued:
        investigation.status = "queued"
        investigation.updated_at = datetime.now(UTC)
        db.commit()
        job_manager.start(investigation.id)
    return RedirectResponse(
        f"/investigations/{investigation_id}#overview",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/pivots/email")
async def follow_email_pivot(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    if investigation.status in {"queued", "running"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "wait for the active investigation run to finish before starting a manual pivot",
        )
    email = str(form.get("email", ""))
    source_finding_id = str(form.get("source_finding_id", ""))
    candidate = find_email_pivot(investigation, email, source_finding_id)
    if candidate is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid email pivot")
    queued = queue_email_pivot(db, investigation, candidate)
    if queued:
        investigation.status = "queued"
        investigation.updated_at = datetime.now(UTC)
        db.commit()
        job_manager.start(investigation.id)
    return RedirectResponse(
        f"/investigations/{investigation_id}#overview",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/pivots/automatic")
async def update_automatic_username_pivots(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    if investigation.query_type != QueryType.EMAIL.value:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "automatic username pivots apply only to email investigations",
        )
    enabled = str(form.get("enabled", "")) == "on"
    investigation.auto_follow_username_pivots = enabled
    investigation.updated_at = datetime.now(UTC)
    queued = (
        queue_automatic_username_pivots(db, investigation)
        if enabled and investigation.status not in {"queued", "running"}
        else []
    )
    if queued:
        investigation.status = "queued"
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="username_pivot_setting_changed",
            details={"enabled": enabled, "queued_run_count": len(queued)},
        )
    )
    db.commit()
    if queued:
        job_manager.start(investigation.id)
    return RedirectResponse(
        f"/investigations/{investigation_id}#analysis",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/investigations/{investigation_id}/locations", response_class=HTMLResponse)
def investigation_locations(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    investigation = load_investigation(db, investigation_id)
    seed_declared_locations(db, investigation)
    db.commit()
    return templates.TemplateResponse(
        request,
        "locations.html",
        context(request, **location_page_values(db, investigation)),
    )


@router.post("/investigations/{investigation_id}/locations")
async def add_investigation_location(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    try:
        payload = LocationObservationInput(
            place_name=str(form.get("place_name", "")),
            locality=str(form.get("locality") or "") or None,
            region=str(form.get("region") or "") or None,
            country=str(form.get("country") or "") or None,
            latitude=parse_optional_float(form.get("latitude")),
            longitude=parse_optional_float(form.get("longitude")),
            precision_radius_m=parse_optional_int(form.get("precision_radius_m")),
            evidence_type=LocationEvidenceType(str(form.get("evidence_type", ""))),
            occurred_at=parse_optional_datetime(form.get("occurred_at")),
            published_at=parse_optional_datetime(form.get("published_at")),
            source_platform=str(form.get("source_platform", "")),
            source_url=str(form.get("source_url") or "") or None,
            account_identifier=str(form.get("account_identifier") or "") or None,
            privacy_masked=str(form.get("privacy_masked", "")) == "on",
            confidence=parse_optional_float(form.get("confidence")),
            raw_evidence={"analyst_note": str(form.get("analyst_note") or "")[:2000]},
        )
        observation = add_location_observation(db, investigation, payload)
        db.flush()
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="location_observation_added",
            details={
                "observation_id": observation.id,
                "place_name": observation.place_name,
                "evidence_type": observation.evidence_type,
                "source_platform": observation.source_platform,
                "occurred_at": observation.occurred_at.isoformat()
                if observation.occurred_at
                else None,
                "privacy_masked": observation.privacy_masked,
            },
        )
    )
    db.commit()
    return RedirectResponse(f"/investigations/{investigation_id}/locations", status_code=303)


@router.post("/investigations/{investigation_id}/locations/collect", response_class=HTMLResponse)
async def collect_investigation_locations(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    requested_url = str(form.get("source_url") or "").strip()[:2048]
    respect_robots = str(form.get("respect_robots", "")) == "on"
    targets: list[tuple[str | None, str, str | None]] = []
    if requested_url:
        targets.append((None, requested_url, None))
    else:
        for finding in investigation.findings:
            if (
                finding.canonical_url
                and finding.status in {"found", "likely"}
                and policy_for_url(finding.canonical_url) is not None
            ):
                profile = finding.extra.get("profile", {})
                account_identifier = (
                    str(profile.get("account_id"))
                    if isinstance(profile, dict) and profile.get("account_id") is not None
                    else finding.value
                )
                targets.append((finding.id, finding.canonical_url, account_identifier))
            if len(targets) >= 25:
                break
    if not targets:
        return templates.TemplateResponse(
            request,
            "locations.html",
            context(
                request,
                **location_page_values(
                    db,
                    investigation,
                    collection_error="No supported review or fitness URLs were available to collect.",
                ),
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    settings = get_settings()
    collector = PublicLocationCollector(
        max_bytes=min(2 * 1024 * 1024, settings.profile_enrichment_max_bytes),
        minimum_interval_seconds=settings.profile_enrichment_interval_seconds,
        respect_robots=respect_robots,
    )
    pages_succeeded = 0
    pages_failed = 0
    observations_processed = 0
    failures: list[dict[str, str]] = []
    for finding_id, target_url, default_account_identifier in targets:
        try:
            collected = await collector.collect(target_url)
        except LocationCollectionError as exc:
            pages_failed += 1
            failures.append({"url": redact(target_url), "error": str(exc)})
            continue
        pages_succeeded += 1
        for item in collected:
            payload = item.payload
            if default_account_identifier and not payload.account_identifier:
                payload = payload.model_copy(
                    update={"account_identifier": default_account_identifier}
                )
            add_location_observation(
                db,
                investigation,
                payload,
                finding_id=finding_id,
            )
            observations_processed += 1

    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="location_public_collection",
            details={
                "targets": len(targets),
                "pages_succeeded": pages_succeeded,
                "pages_failed": pages_failed,
                "observations_processed": observations_processed,
                "robots_policy": "respected" if respect_robots else "ignored_by_analyst",
                "failures": failures[:25],
            },
        )
    )
    db.commit()
    collection_result: dict[str, object] = {
        "targets": len(targets),
        "pages_succeeded": pages_succeeded,
        "pages_failed": pages_failed,
        "observations_processed": observations_processed,
        "robots_policy": "respected" if respect_robots else "ignored by analyst",
    }
    return templates.TemplateResponse(
        request,
        "locations.html",
        context(
            request,
            **location_page_values(
                db,
                investigation,
                collection_result=collection_result,
                collection_error=(
                    failures[0]["error"]
                    if requested_url and not pages_succeeded and failures
                    else None
                ),
            ),
        ),
        status_code=(
            status.HTTP_502_BAD_GATEWAY
            if requested_url and not pages_succeeded
            else status.HTTP_200_OK
        ),
    )


def _search_date(value: object) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("search date must use YYYY-MM-DD") from exc
    if not 1900 <= parsed.year <= datetime.now(UTC).year:
        raise ValueError("search date is outside the supported range")
    return parsed


def _search_options_from_form(form: FormData) -> SearchQueryOptions:
    aliases = tuple(
        line.strip() for line in str(form.get("name_aliases", "")).splitlines() if line.strip()
    )
    return validate_search_options(
        SearchQueryOptions(
            engines=tuple(str(value) for value in form.getlist("engines")),
            platform_ids=tuple(str(value) for value in form.getlist("platform_ids")),
            document_types=tuple(str(value) for value in form.getlist("document_types")),
            from_date=_search_date(form.get("from_date")),
            to_date=_search_date(form.get("to_date")),
            include_core=str(form.get("include_core", "")) == "on",
            include_name_variants=str(form.get("include_name_variants", "")) == "on",
            name_aliases=aliases,
        )
    )


def search_query_page_values(
    db: Session,
    investigation: Investigation,
    *,
    options: SearchQueryOptions | None = None,
    selected_template_ids: set[str] | None = None,
    error: str | None = None,
    notice: dict[str, object] | None = None,
) -> dict[str, object]:
    selected_options = validate_search_options(options or default_search_options())
    query_type = QueryType(investigation.query_type)
    saved_templates = list(db.scalars(select(SearchTemplate).order_by(SearchTemplate.name)))
    applicable_templates = [
        template for template in saved_templates if query_type.value in template.query_types
    ]
    selected_ids = (
        selected_template_ids
        if selected_template_ids is not None
        else {template.id for template in applicable_templates}
    )
    active_templates = [
        template for template in applicable_templates if template.id in selected_ids
    ]
    display_name_pivots = discover_display_name_pivots(investigation)
    name_variants = name_variants_for_search(
        investigation.query,
        query_type,
        selected_options,
    )
    generated = generate_search_queries(
        investigation.query,
        query_type,
        selected_options,
        active_templates,
        tuple(item.value for item in display_name_pivots),
    )
    settings = get_settings()
    query_cards = [
        {
            "item": item,
            "token": sign_search_query(settings.secret_key, investigation.id, item),
        }
        for item in generated
    ]
    return {
        "investigation": investigation,
        "queries": query_cards,
        "display_name_pivots": display_name_pivots,
        "name_variants": name_variants,
        "browser_target": "system",
        "search_options": selected_options,
        "search_platforms": SEARCH_PLATFORMS,
        "document_types": DOCUMENT_TYPES,
        "saved_templates": saved_templates,
        "selected_template_ids": selected_ids,
        "query_types": list(QueryType),
        "engines": tuple(ENGINE_URLS),
        "error": error,
        "notice": notice,
    }


@router.get("/investigations/{investigation_id}/google-dorks", response_class=HTMLResponse)
def investigation_google_dorks(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    investigation = load_investigation(db, investigation_id)
    display_name_pivots = discover_display_name_pivots(investigation)
    dorks = generate_google_dorks(
        investigation.query,
        QueryType(investigation.query_type),
        tuple(item.value for item in display_name_pivots),
    )
    return templates.TemplateResponse(
        request,
        "google_dorks.html",
        context(
            request,
            investigation=investigation,
            dorks=dorks,
            documented_count=sum(item.reliability == "documented" for item in dorks),
            best_effort_count=sum(item.reliability == "best_effort" for item in dorks),
            dork_run=_dork_run_view(db, investigation),
            dork_run_supported=bool(dork_run.dork_seeds(investigation)),
            dork_run_seeds=[value for value, _ in dork_run.dork_seeds(investigation)],
            dork_run_engines=dork_run.DORK_RUN_ENGINES,
            dork_run_default_engines=dork_run.DEFAULT_ENGINES,
            dork_run_defaults={
                "max_queries": dork_run.DEFAULT_MAX_QUERIES,
                "max_queries_limit": dork_run.MAX_QUERIES_LIMIT,
                "delay_min": dork_run.DEFAULT_DELAY_MIN,
                "delay_max": dork_run.DEFAULT_DELAY_MAX,
                "delay_low": dork_run.DELAY_BOUNDS[0],
                "delay_high": dork_run.DELAY_BOUNDS[1],
            },
        ),
    )


@router.post("/investigations/{investigation_id}/identifiers")
async def add_case_identifiers(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    if investigation.id in job_manager.tasks or investigation.status in {"queued", "running"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "wait for the current collection to finish"
        )
    seeds = _parse_additional_identifiers(form.get("additional_identifiers"))
    if not seeds:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "enter at least one identifier to add"
        )
    runs = case_identifiers.queue_additional_identifiers(db, investigation, seeds)
    if runs:
        investigation.selected_tools = list(
            dict.fromkeys([*investigation.selected_tools, *(run.tool_id for run in runs)])
        )
        investigation.status = "queued"
        db.commit()
        job_manager.start(investigation.id)
    return RedirectResponse(
        f"/investigations/{investigation.id}#overview", status_code=status.HTTP_303_SEE_OTHER
    )


dork_run_tasks: dict[str, asyncio.Task[None]] = {}
DORK_RUN_BUSY_POLLS = 6
DORK_RUN_BUSY_WAIT_SECONDS = 20.0


async def _dork_run_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _run_dork_item(investigation_id: str, index: int) -> bool:
    """Run one planned query. Returns False when the run should stop."""
    settings = get_settings()
    client = BrowserAgentClient(settings)
    with SessionLocal() as db:
        state = dork_run.load_state(db, investigation_id)
        if state is None or state["status"] != "running":
            return False
        item = state["items"][index]
        engine = dork_run.available_engine(state, item["engine"], item["query"])
        if engine is None and dork_run.available_engine(state, item["engine"]) is not None:
            # Only engines that ignore search restrictions are left; skip this query.
            item["status"] = "skipped"
            item["detail"] = "no remaining engine honours site or file-type restrictions"
            dork_run.save_state(db, investigation_id, state)
            db.commit()
            return True
        if engine is None:
            dork_run.finish_state(state, "stopped", "every selected engine showed a challenge")
            dork_run.save_state(db, investigation_id, state)
            db.commit()
            return False
        item["engine"] = engine
        item["status"] = "running"
        dork_run.save_state(db, investigation_id, state)
        db.commit()

    # The browser has one capture slot; wait for a held or running capture to clear.
    for _ in range(DORK_RUN_BUSY_POLLS):
        browser_status = await client.status()
        if browser_status.available and not browser_status.active_job:
            break
        await _dork_run_sleep(DORK_RUN_BUSY_WAIT_SECONDS)
    else:
        with SessionLocal() as db:
            state = dork_run.load_state(db, investigation_id)
            if state is not None and state["status"] == "running":
                dork_run.finish_state(
                    state, "stopped", "the sock browser was unavailable or held by another capture"
                )
                dork_run.save_state(db, investigation_id, state)
                db.commit()
        return False

    target = _search_engine_url(engine, item["query"])
    with SessionLocal() as db:
        investigation = load_investigation(db, investigation_id)
        state = dork_run.load_state(db, investigation_id)
        if state is None or state["status"] != "running":
            return False
        item = state["items"][index]
        browser_run = BrowserRun(
            investigation_id=investigation.id,
            requested_url=target,
            collector_id=managed_browser_collector_for_url(target),
            status="running",
            started_at=datetime.now(UTC),
        )
        db.add(browser_run)
        db.flush()
        item["browser_run_id"] = browser_run.id
        dork_run.save_state(db, investigation_id, state)
        db.commit()
        challenged = False
        try:
            result = await client.capture(
                job_id=browser_run.id,
                investigation_id=investigation.id,
                url=target,
                collector_id=browser_run.collector_id,
            )
            imported = apply_browser_capture_result(
                db, investigation, browser_run, result, resumed=False
            )
            if result.status == "completed":
                extracted_items, _rejected = search_results_from_capture(result)
                item["imported"] = imported
                item["extracted"] = len(extracted_items)
                # A page with no result links is not a clean "no hits": the engine may have
                # served an unreadable or blocked page without flagging a challenge.
                item["status"] = "completed" if extracted_items else "no_results"
                if not extracted_items:
                    item["detail"] = "no result links could be read from the page"
            else:
                # Never solve or sit on a challenge: release the slot and drop this engine.
                challenged = True
                try:
                    await client.abandon(job_id=browser_run.id, investigation_id=investigation.id)
                except BrowserAgentError as exc:
                    if exc.code != "pending_job_missing":
                        raise
                browser_run.status = "cancelled"
                browser_run.finished_at = datetime.now(UTC)
                browser_run.error_message = (
                    "automated dork run released a challenged search; run it again manually to clear it"
                )
                db.add(
                    AuditEvent(
                        investigation_id=investigation.id,
                        event_type="browser_capture_abandoned",
                        details={
                            "browser_run_id": browser_run.id,
                            "collector_id": browser_run.collector_id,
                            "challenge_type": browser_run.challenge_type,
                            "automated_dork_run": True,
                        },
                    )
                )
        except BrowserAgentError as exc:
            fail_browser_run(db, investigation, browser_run, exc)
            item["status"] = "failed"
            item["detail"] = str(exc)[:200]
        # Re-read so a cancel issued during the capture is not overwritten.
        latest = dork_run.load_state(db, investigation_id)
        cancelled = latest is None or latest["status"] != "running"
        if latest is not None:
            state = latest
            # Two failures in a row point at the browser or its configuration, not at one
            # engine, so stop rather than fail every remaining search the same way.
            if (
                item["status"] == "failed"
                and state["items"][index].get("retried")
                and index > 0
                and state["items"][index - 1]["status"] == "failed"
                and not cancelled
            ):
                dork_run.finish_state(
                    state, "stopped", f"two searches failed in a row: {item['detail']}"
                )
                cancelled = True
            current = state["items"][index]
            current.update(
                {
                    key: item.get(key)
                    for key in ("engine", "status", "imported", "extracted", "browser_run_id", "detail")
                }
            )
            if challenged:
                if engine not in state["cooled_engines"]:
                    state["cooled_engines"] = [*state["cooled_engines"], engine]
                fallback = dork_run.available_engine(state, engine, current["query"])
                if fallback is not None and not current.get("retried") and not cancelled:
                    current.update({"status": "pending", "engine": fallback, "retried": True})
                    current["detail"] = f"{engine.title()} showed a challenge; moved to {fallback.title()}"
                else:
                    current["status"] = "challenged"
                    current["detail"] = f"{engine.title()} showed a challenge"
            if current["status"] == "failed" and not current.get("retried") and not cancelled:
                # A one-off failure such as a DNS hiccup gets one more try on another engine.
                fallback = dork_run.available_engine(
                    state, engine, current["query"], avoid=engine
                )
                if fallback is not None:
                    current["detail"] = f"{engine.title()} failed ({current['detail']}); retrying on {fallback.title()}"
                    current.update({"status": "pending", "engine": fallback, "retried": True})
            if cancelled and current["status"] in {"pending", "running"}:
                current["status"] = "skipped"
            dork_run.save_state(db, investigation_id, state)
        db.commit()
        return not cancelled


async def run_dork_queue(investigation_id: str) -> None:
    rng = random.SystemRandom()
    try:
        while True:
            with SessionLocal() as db:
                state = dork_run.load_state(db, investigation_id)
            if state is None or state["status"] != "running":
                return
            index = next(
                (i for i, item in enumerate(state["items"]) if item["status"] == "pending"), None
            )
            if index is None:
                break
            if not await _run_dork_item(investigation_id, index):
                return
            with SessionLocal() as db:
                state = dork_run.load_state(db, investigation_id)
            if state is None or not any(item["status"] == "pending" for item in state["items"]):
                break
            await _dork_run_sleep(rng.uniform(state["delay_min"], state["delay_max"]))
        with SessionLocal() as db:
            state = dork_run.load_state(db, investigation_id)
            if state is not None and state["status"] == "running":
                dork_run.finish_state(state, "completed")
                counts = dork_run.summarise(state)
                db.add(
                    AuditEvent(
                        investigation_id=investigation_id,
                        event_type="dork_run_completed",
                        details={
                            "queries": counts["total"],
                            "completed": counts.get("completed", 0),
                            "challenged": counts.get("challenged", 0),
                            "imported_findings": counts["imported"],
                            "cooled_engines": state["cooled_engines"],
                        },
                    )
                )
                dork_run.save_state(db, investigation_id, state)
                db.commit()
    except Exception as exc:  # noqa: BLE001 - a background run must record why it stopped
        with SessionLocal() as db:
            state = dork_run.load_state(db, investigation_id)
            if state is not None and state["status"] == "running":
                dork_run.finish_state(state, "stopped", f"run failed: {type(exc).__name__}")
                dork_run.save_state(db, investigation_id, state)
                db.commit()
        raise


capture_run_tasks: dict[str, asyncio.Task[None]] = {}


async def _run_capture_item(investigation_id: str, index: int) -> bool:
    """Capture one planned profile page. Returns False when the run should stop."""
    client = BrowserAgentClient(get_settings())
    with SessionLocal() as db:
        state = capture_run.load_state(db, investigation_id)
        if state is None or state["status"] != "running":
            return False
        state["items"][index]["status"] = "running"
        capture_run.save_state(db, investigation_id, state)
        db.commit()

    # The browser has one capture slot; wait for a held or running capture to clear.
    for _ in range(DORK_RUN_BUSY_POLLS):
        browser_status = await client.status()
        if browser_status.available and not browser_status.active_job:
            break
        await _dork_run_sleep(DORK_RUN_BUSY_WAIT_SECONDS)
    else:
        with SessionLocal() as db:
            state = capture_run.load_state(db, investigation_id)
            if state is not None and state["status"] == "running":
                capture_run.finish_state(
                    state, "stopped", "the sock browser was unavailable or held by another capture"
                )
                capture_run.save_state(db, investigation_id, state)
                db.commit()
        return False

    with SessionLocal() as db:
        investigation = load_investigation(db, investigation_id)
        state = capture_run.load_state(db, investigation_id)
        if state is None or state["status"] != "running":
            return False
        item = state["items"][index]
        target = item["url"]
        browser_run = BrowserRun(
            investigation_id=investigation.id,
            requested_url=target,
            collector_id=managed_browser_collector_for_url(target),
            status="running",
            started_at=datetime.now(UTC),
        )
        db.add(browser_run)
        db.flush()
        item["browser_run_id"] = browser_run.id
        capture_run.save_state(db, investigation_id, state)
        db.commit()
        outcome: dict[str, Any] = {"browser_run_id": browser_run.id}
        blocked = False
        platform_unreadable: str | None = None
        try:
            result = await client.capture(
                job_id=browser_run.id,
                investigation_id=investigation.id,
                url=target,
                collector_id=browser_run.collector_id,
            )
            imported = apply_browser_capture_result(
                db, investigation, browser_run, result, resumed=False
            )
            if result.status == "completed":
                # Enriching a finding that already exists imports nothing new, so ask whether
                # a profile was read rather than whether a finding was created.
                read = bool(imported) or social_profile_was_read(result)
                outcome.update({"imported": imported, "status": "completed" if read else "no_profile"})
                if not read:
                    # The page loaded but held no profile for this account: a login wall the
                    # agent did not flag, a removed account, or a page for someone else.
                    outcome["detail"] = "the page held no readable profile for this account"
            else:
                # Never sign in or solve a challenge: release the slot and leave this platform.
                blocked = True
                try:
                    await client.abandon(job_id=browser_run.id, investigation_id=investigation.id)
                except BrowserAgentError as exc:
                    if exc.code != "pending_job_missing":
                        raise
                browser_run.status = "cancelled"
                browser_run.finished_at = datetime.now(UTC)
                browser_run.error_message = (
                    "profile capture run released a page that asked for a sign-in or showed a "
                    "challenge; capture it manually to handle that"
                )
                db.add(
                    AuditEvent(
                        investigation_id=investigation.id,
                        event_type="browser_capture_abandoned",
                        details={
                            "browser_run_id": browser_run.id,
                            "collector_id": browser_run.collector_id,
                            "challenge_type": browser_run.challenge_type,
                            "profile_capture_run": True,
                        },
                    )
                )
                outcome.update({"status": "challenged", "detail": "sign-in or challenge shown"})
        except BrowserAgentError as exc:
            fail_browser_run(db, investigation, browser_run, exc)
            outcome.update({"status": "failed", "detail": str(exc)[:200]})
            if exc.code in {"session_required", "target_not_allowed"}:
                # The platform cannot be read at all in this setup (no signed-in session, or
                # its host is not in the browser's allowed list). That is not a browser
                # fault, and every other page on the platform would fail the same way.
                platform_unreadable = (
                    "needs a signed-in browser session; sign in through the sock browser first"
                    if exc.code == "session_required"
                    else "is not in the browser's allowed hosts (OSINT_BROWSER_ALLOWED_HOSTS)"
                )
                outcome.update({"status": "skipped", "detail": f"{item['platform']} {platform_unreadable}"})
        # Re-read so a cancel issued during the capture is not overwritten.
        latest = capture_run.load_state(db, investigation_id)
        cancelled = latest is None or latest["status"] != "running"
        if latest is not None:
            current = latest["items"][index]
            current.update(outcome)
            if blocked:
                capture_run.cool_platform(
                    latest, current["platform"],
                    f"{current['platform']} asked for a sign-in or showed a challenge earlier in this run",
                )
            if platform_unreadable:
                capture_run.cool_platform(
                    latest, current["platform"], f"{current['platform']} {platform_unreadable}"
                )
            if current["status"] == "no_profile" and capture_run.platform_keeps_returning_nothing(
                latest, current["platform"]
            ):
                capture_run.cool_platform(
                    latest, current["platform"],
                    f"{current['platform']} returned no readable profile twice in a row, which "
                    "usually means it wants a sign-in",
                )
            if (
                current["status"] == "failed"
                and index > 0
                and latest["items"][index - 1]["status"] == "failed"
                and not cancelled
            ):
                # Two failures in a row point at the browser, not at one page.
                capture_run.finish_state(
                    latest, "stopped", f"two captures failed in a row: {current['detail']}"
                )
                cancelled = True
            capture_run.save_state(db, investigation_id, latest)
        db.commit()
        return not cancelled


async def run_capture_queue(investigation_id: str) -> None:
    rng = random.SystemRandom()
    try:
        while True:
            with SessionLocal() as db:
                state = capture_run.load_state(db, investigation_id)
            if state is None or state["status"] != "running":
                return
            index = next(
                (i for i, item in enumerate(state["items"]) if item["status"] == "pending"), None
            )
            if index is None:
                break
            if not await _run_capture_item(investigation_id, index):
                return
            with SessionLocal() as db:
                state = capture_run.load_state(db, investigation_id)
            if state is None or not any(item["status"] == "pending" for item in state["items"]):
                break
            await _dork_run_sleep(rng.uniform(state["delay_min"], state["delay_max"]))
        with SessionLocal() as db:
            state = capture_run.load_state(db, investigation_id)
            if state is not None and state["status"] == "running":
                capture_run.finish_state(state, "completed")
                counts = capture_run.summarise(state)
                db.add(
                    AuditEvent(
                        investigation_id=investigation_id,
                        event_type="profile_capture_run_completed",
                        details={
                            "pages": counts["total"],
                            "completed": counts.get("completed", 0),
                            "no_profile": counts.get("no_profile", 0),
                            "challenged": counts.get("challenged", 0),
                            "imported_findings": counts["imported"],
                            "cooled_platforms": state["cooled_platforms"],
                        },
                    )
                )
                capture_run.save_state(db, investigation_id, state)
                db.commit()
    except Exception as exc:  # noqa: BLE001 - a background run must record why it stopped
        with SessionLocal() as db:
            state = capture_run.load_state(db, investigation_id)
            if state is not None and state["status"] == "running":
                capture_run.finish_state(state, "stopped", f"run failed: {type(exc).__name__}")
                capture_run.save_state(db, investigation_id, state)
                db.commit()
        raise


def _capture_run_view(db: Session, investigation: Investigation) -> dict[str, Any] | None:
    state = capture_run.load_state(db, investigation.id)
    if state is None:
        return None
    if investigation.id not in capture_run_tasks and (
        state["status"] == "running" or any(item["status"] == "running" for item in state["items"])
    ):
        # The application restarted mid-run; nothing is driving the queue any more.
        if state["status"] == "running":
            capture_run.finish_state(state, "stopped", "interrupted by an application restart")
        for item in state["items"]:
            if item["status"] == "running":
                item["status"] = "skipped"
        capture_run.save_state(db, investigation.id, state)
        db.commit()
    return {**state, "counts": capture_run.summarise(state)}


@router.post("/investigations/{investigation_id}/capture-run")
async def start_capture_run(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    investigation = load_investigation(db, investigation_id)
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    if not get_settings().browser_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "managed browser support is disabled")
    if investigation.id in capture_run_tasks:
        raise HTTPException(status.HTTP_409_CONFLICT, "a profile capture run is already active")
    if investigation.id in dork_run_tasks:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "an automated dork run is using the sock browser; wait for it or cancel it first",
        )
    try:
        delay_min, delay_max = capture_run.clamp_delays(
            int(str(form.get("delay_min") or capture_run.DEFAULT_DELAY_MIN)),
            int(str(form.get("delay_max") or capture_run.DEFAULT_DELAY_MAX)),
        )
        plan = capture_run.plan_capture_run(
            investigation, int(str(form.get("max_pages") or capture_run.DEFAULT_MAX_PAGES))
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    capture_run.save_state(db, investigation.id, capture_run.new_state(plan, delay_min, delay_max))
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="profile_capture_run_started",
            details={"pages": len(plan), "delay_min": delay_min, "delay_max": delay_max},
        )
    )
    db.commit()
    task = asyncio.create_task(run_capture_queue(investigation.id))
    capture_run_tasks[investigation.id] = task
    task.add_done_callback(lambda _: capture_run_tasks.pop(investigation.id, None))
    return RedirectResponse(
        f"/investigations/{investigation.id}/browser#capture-run",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/capture-run/cancel")
async def cancel_capture_run(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    investigation = load_investigation(db, investigation_id)
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    state = capture_run.load_state(db, investigation.id)
    if state is not None and state["status"] == "running":
        # The page in flight finishes; nothing further is opened.
        running = [item for item in state["items"] if item["status"] == "running"]
        capture_run.finish_state(state, "cancelled", "cancelled by the analyst")
        for item in running:
            item["status"] = "running"
            item["detail"] = None
        capture_run.save_state(db, investigation.id, state)
        db.add(
            AuditEvent(
                investigation_id=investigation.id, event_type="profile_capture_run_cancelled", details={}
            )
        )
        db.commit()
    return RedirectResponse(
        f"/investigations/{investigation.id}/browser#capture-run",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _discard_dork_run(db: Session, investigation_id: str) -> None:
    """Stop any active queue and remove its stored queries when a case is deleted."""
    task = dork_run_tasks.pop(investigation_id, None)
    if task is not None:
        task.cancel()
    setting = db.get(ApplicationSetting, dork_run.state_key(investigation_id))
    if setting is not None:
        db.delete(setting)
    capture_task = capture_run_tasks.pop(investigation_id, None)
    if capture_task is not None:
        capture_task.cancel()
    capture_setting = db.get(ApplicationSetting, capture_run.state_key(investigation_id))
    if capture_setting is not None:
        db.delete(capture_setting)


def _dork_run_view(db: Session, investigation: Investigation) -> dict[str, Any] | None:
    state = dork_run.load_state(db, investigation.id)
    if state is None:
        return None
    if state["status"] == "running" and investigation.id not in dork_run_tasks:
        # The application restarted mid-run; nothing is driving the queue any more.
        dork_run.finish_state(state, "stopped", "interrupted by an application restart")
        dork_run.save_state(db, investigation.id, state)
        db.commit()
    elif investigation.id not in dork_run_tasks and any(
        item["status"] == "running" for item in state["items"]
    ):
        for item in state["items"]:
            if item["status"] == "running":
                item["status"] = "skipped"
        dork_run.save_state(db, investigation.id, state)
        db.commit()
    return {**state, "counts": dork_run.summarise(state)}


def _launch_dork_run(
    db: Session,
    investigation: Investigation,
    engines: tuple[str, ...],
    delay_min: int,
    delay_max: int,
    max_queries: int,
    *,
    automatic: bool = False,
) -> int:
    """Plan a dork run, record it and start its background queue. Raises ValueError."""
    plan = dork_run.plan_dork_run(investigation, engines, max_queries)
    dork_run.save_state(
        db, investigation.id, dork_run.new_state(plan, engines, delay_min, delay_max)
    )
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="dork_run_started",
            details={
                "queries": len(plan),
                "engines": list(engines),
                "delay_min": delay_min,
                "delay_max": delay_max,
                "automatic": automatic,
            },
        )
    )
    db.commit()
    investigation_id = investigation.id
    task = asyncio.create_task(run_dork_queue(investigation_id))
    dork_run_tasks[investigation_id] = task
    task.add_done_callback(lambda _: dork_run_tasks.pop(investigation_id, None))
    return len(plan)


automatic_dork_run_waiters: set[asyncio.Task[None]] = set()
AUTOMATIC_DORK_RUN_POLL_SECONDS = 5.0
AUTOMATIC_DORK_RUN_MAX_WAIT_SECONDS = 2 * 60 * 60


async def _automatic_dork_run_after_collection(investigation_id: str) -> None:
    """Start the default dork run for a new case once its collectors have finished.

    It waits so the searches do not compete with collectors for the browser, and it gives up
    quietly when the case was cancelled, a run already exists, or the browser is not ready.
    """
    waited = 0.0
    while True:
        await _dork_run_sleep(AUTOMATIC_DORK_RUN_POLL_SECONDS)
        waited += AUTOMATIC_DORK_RUN_POLL_SECONDS
        with SessionLocal() as db:
            investigation = db.get(Investigation, investigation_id)
            if investigation is None or investigation.status == "cancelled":
                return
            if investigation.status in {"queued", "running"}:
                if waited >= AUTOMATIC_DORK_RUN_MAX_WAIT_SECONDS:
                    return
                continue
            settings = get_settings()
            skipped: str | None = None
            if not settings.browser_enabled or not get_runtime_options(db)["automatic_dork_run"]:
                return
            if investigation_id in dork_run_tasks or dork_run.load_state(db, investigation_id):
                return
            if investigation_id in capture_run_tasks:
                skipped = "a profile capture run was using the sock browser"
            else:
                try:
                    agent = await BrowserAgentClient(settings).status()
                except BrowserAgentError:
                    agent = None
                if agent is None or not agent.available or not agent.profile_ready:
                    skipped = "the sock browser was not available"
            if skipped is None:
                try:
                    _launch_dork_run(
                        db,
                        investigation,
                        dork_run.DEFAULT_ENGINES,
                        dork_run.DEFAULT_DELAY_MIN,
                        dork_run.DEFAULT_DELAY_MAX,
                        dork_run.DEFAULT_MAX_QUERIES,
                        automatic=True,
                    )
                except ValueError as exc:
                    skipped = str(exc)
            if skipped is not None:
                db.add(
                    AuditEvent(
                        investigation_id=investigation_id,
                        event_type="automatic_dork_run_skipped",
                        details={"reason": skipped[:300]},
                    )
                )
                db.commit()
            return


@router.post("/investigations/{investigation_id}/dork-run")
async def start_dork_run(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    investigation = load_investigation(db, investigation_id)
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    if not get_settings().browser_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "managed browser support is disabled")
    if investigation.id in dork_run_tasks:
        raise HTTPException(status.HTTP_409_CONFLICT, "an automated dork run is already active")
    if investigation.id in capture_run_tasks:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a profile capture run is using the sock browser; wait for it or cancel it first",
        )
    try:
        engines = dork_run.normalise_engines([str(item) for item in form.getlist("engines")])
        delay_min, delay_max = dork_run.clamp_delays(
            int(str(form.get("delay_min") or dork_run.DEFAULT_DELAY_MIN)),
            int(str(form.get("delay_max") or dork_run.DEFAULT_DELAY_MAX)),
        )
        _launch_dork_run(
            db,
            investigation,
            engines,
            delay_min,
            delay_max,
            int(str(form.get("max_queries") or dork_run.DEFAULT_MAX_QUERIES)),
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return RedirectResponse(
        f"/investigations/{investigation.id}/google-dorks#automated-run",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/dork-run/cancel")
async def cancel_dork_run(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    investigation = load_investigation(db, investigation_id)
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    state = dork_run.load_state(db, investigation.id)
    if state is not None and state["status"] == "running":
        # The query in flight finishes; nothing further is sent.
        running = [item for item in state["items"] if item["status"] == "running"]
        dork_run.finish_state(state, "cancelled", "cancelled by the analyst")
        for item in running:
            item["status"] = "running"
            item["detail"] = None
        dork_run.save_state(db, investigation.id, state)
        db.add(AuditEvent(investigation_id=investigation.id, event_type="dork_run_cancelled", details={}))
        db.commit()
    return RedirectResponse(
        f"/investigations/{investigation.id}/google-dorks#automated-run",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/investigations/{investigation_id}/search-queries", response_class=HTMLResponse)
def investigation_search_queries(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    investigation = load_investigation(db, investigation_id)
    notice = request.session.pop("search_workbench_notice", None)
    return templates.TemplateResponse(
        request,
        "search_queries.html",
        context(
            request,
            **search_query_page_values(
                db,
                investigation,
                notice=notice if isinstance(notice, dict) else None,
            ),
        ),
    )


@router.post(
    "/investigations/{investigation_id}/search-queries/generate", response_class=HTMLResponse
)
async def generate_search_query_pack(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    selected_template_ids = {str(value) for value in form.getlist("template_ids")}
    try:
        options = _search_options_from_form(form)
    except ValueError as exc:
        options = default_search_options()
        return templates.TemplateResponse(
            request,
            "search_queries.html",
            context(
                request,
                **search_query_page_values(
                    db,
                    investigation,
                    options=options,
                    selected_template_ids=selected_template_ids,
                    error=str(exc),
                ),
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    values = search_query_page_values(
        db,
        investigation,
        options=options,
        selected_template_ids=selected_template_ids,
    )
    query_cards = values["queries"]
    name_variants = values["name_variants"]
    if not isinstance(query_cards, list) or not isinstance(name_variants, tuple):
        raise RuntimeError("search query page produced an invalid query pack")
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="search_query_pack_generated",
            details={
                "engines": options.engines,
                "platform_ids": options.platform_ids,
                "document_types": options.document_types,
                "from_date": options.from_date.isoformat() if options.from_date else None,
                "to_date": options.to_date.isoformat() if options.to_date else None,
                "include_core": options.include_core,
                "include_name_variants": options.include_name_variants,
                "name_aliases": options.name_aliases,
                "name_variant_count": len(name_variants),
                "template_ids": sorted(selected_template_ids),
                "query_count": len(query_cards),
            },
        )
    )
    db.commit()
    return templates.TemplateResponse(
        request,
        "search_queries.html",
        context(request, **values),
    )


@router.post("/investigations/{investigation_id}/search-queries/templates")
async def create_search_template(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    try:
        name, category, query_template, engines, query_types = validate_template_definition(
            name=str(form.get("name", "")),
            category=str(form.get("category", "")),
            query_template=str(form.get("query_template", "")),
            engines=[str(value) for value in form.getlist("engines")],
            query_types=[str(value) for value in form.getlist("query_types")],
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    item = SearchTemplate(
        name=name,
        category=category,
        query_template=query_template,
        engines=engines,
        query_types=query_types,
    )
    db.add(item)
    db.flush()
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="search_template_created",
            details={"template_id": item.id, "name": item.name},
        )
    )
    db.commit()
    return RedirectResponse(
        f"/investigations/{investigation.id}/search-queries#templates",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/search-queries/templates/{template_id}")
async def update_search_template(
    investigation_id: str,
    template_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    item = db.get(SearchTemplate, template_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "search template not found")
    try:
        name, category, query_template, engines, query_types = validate_template_definition(
            name=str(form.get("name", "")),
            category=str(form.get("category", "")),
            query_template=str(form.get("query_template", "")),
            engines=[str(value) for value in form.getlist("engines")],
            query_types=[str(value) for value in form.getlist("query_types")],
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    item.name = name
    item.category = category
    item.query_template = query_template
    item.engines = engines
    item.query_types = query_types
    item.updated_at = datetime.now(UTC)
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="search_template_updated",
            details={"template_id": item.id, "name": item.name},
        )
    )
    db.commit()
    return RedirectResponse(
        f"/investigations/{investigation.id}/search-queries#templates",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/search-queries/templates/{template_id}/delete")
async def delete_search_template(
    investigation_id: str,
    template_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    item = db.get(SearchTemplate, template_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "search template not found")
    name = item.name
    db.delete(item)
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="search_template_deleted",
            details={"template_id": template_id, "name": name},
        )
    )
    db.commit()
    return RedirectResponse(
        f"/investigations/{investigation.id}/search-queries#templates",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/search-queries/launch")
async def launch_signed_search_query(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    try:
        item = verify_search_query_token(
            get_settings().secret_key,
            investigation.id,
            str(form.get("token", "")),
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="search_query_launched",
            details={
                "query_id": item.id,
                "engine": item.engine,
                "query": item.query,
                "browser_target": "system",
            },
        )
    )
    db.commit()
    return RedirectResponse(item.url, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/investigations/{investigation_id}/search-queries/import")
async def import_search_query_results(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    try:
        item = verify_search_query_token(
            get_settings().secret_key,
            investigation.id,
            str(form.get("token", "")),
        )
        summary = import_search_results(
            db,
            investigation,
            raw=str(form.get("results", "")),
            engine=item.engine,
            query=item.query,
            query_id=item.id,
            query_label=item.label,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    db.commit()
    request.session["search_workbench_notice"] = {
        "type": "success",
        "message": (
            f"Imported {summary.imported_count} new results; "
            f"updated {summary.duplicate_count} existing findings; "
            f"rejected {summary.rejected_count}."
        ),
    }
    return RedirectResponse(
        f"/investigations/{investigation.id}/search-queries#import-results",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/search-queries/searxng")
async def capture_search_query_via_searxng(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    settings = get_settings()
    try:
        item = verify_search_query_token(
            settings.secret_key,
            investigation.id,
            str(form.get("token", "")),
        )
        active_engines = get_active_searxng_engines(db)
        items, rejected = await search_via_searxng(
            item.query,
            base_url=settings.searxng_url,
            active_engines=active_engines,
        )
        summary = import_search_result_items(
            db,
            investigation,
            items,
            rejected,
            engine=item.engine,
            query=item.query,
            query_id=item.id,
            query_label=item.label,
            source_tool="search_searxng_capture",
            tool_display_name="Search result (SearXNG)",
            event_type="search_results_searxng",
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    db.commit()
    request.session["search_workbench_notice"] = {
        "type": "success",
        "message": (
            f"SearXNG returned {summary.submitted_count} result(s); "
            f"imported {summary.imported_count} new; "
            f"updated {summary.duplicate_count} existing findings; "
            f"rejected {summary.rejected_count}."
        ),
    }
    return RedirectResponse(
        f"/investigations/{investigation.id}/search-queries",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/search-queries/{query_id}/launch")
async def launch_search_query(
    investigation_id: str, query_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    item = find_search_query(investigation.query, QueryType(investigation.query_type), query_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "search query not found")
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="search_query_launched",
            details={
                "query_id": item.id,
                "engine": item.engine,
                "query": item.query,
                "browser_target": "system",
                "legacy_route": True,
            },
        )
    )
    db.commit()
    return RedirectResponse(item.url, status_code=status.HTTP_303_SEE_OTHER)


def automatic_wayback_page_results(investigation: Investigation) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for finding in investigation.findings:
        metadata = finding.extra.get("wayback", {})
        if not isinstance(metadata, dict) or not metadata.get("automatic_checked_at"):
            continue
        results.append(
            {
                "finding": finding,
                "metadata": metadata,
            }
        )
    results.sort(
        key=lambda item: (
            -int(item["metadata"].get("capture_count", 0)),
            str(item["finding"].platform or "").casefold(),
        )
    )
    return results


@router.get("/investigations/{investigation_id}/wayback", response_class=HTMLResponse)
def investigation_wayback(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    investigation = load_investigation(db, investigation_id)
    candidates = sorted(
        {
            finding.canonical_url
            for finding in investigation.findings
            if finding.canonical_url and finding.status in {"found", "likely"}
        }
    )
    default_target = (
        investigation.query if investigation.query_type == QueryType.DOMAIN.value else ""
    )
    return templates.TemplateResponse(
        request,
        "wayback.html",
        context(
            request,
            investigation=investigation,
            candidates=candidates,
            automatic_results=automatic_wayback_page_results(investigation),
            target=default_target,
            captures=[],
            error=None,
            save_result=None,
            save_error=None,
        ),
    )


@router.post("/investigations/{investigation_id}/wayback", response_class=HTMLResponse)
async def discover_wayback(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    candidates = sorted(
        {
            finding.canonical_url
            for finding in investigation.findings
            if finding.canonical_url and finding.status in {"found", "likely"}
        }
    )
    raw_target = str(form.get("target", ""))[:2048]
    wildcard_domain = str(form.get("wildcard_domain", "")) == "on"
    prefix_target = str(form.get("prefix_target", "")) == "on"
    try:
        target = normalise_target(
            raw_target, wildcard_domain=wildcard_domain, prefix_target=prefix_target
        )
        from_year_raw = str(form.get("from_year", "")).strip()
        to_year_raw = str(form.get("to_year", "")).strip()
        from_year = int(from_year_raw) if from_year_raw else None
        to_year = int(to_year_raw) if to_year_raw else None
        current_year = datetime.now(UTC).year
        for year in (from_year, to_year):
            if year is not None and not 1996 <= year <= current_year:
                raise ValueError(f"year must be between 1996 and {current_year}")
        if from_year is not None and to_year is not None and from_year > to_year:
            raise ValueError("from year cannot be later than to year")
        captures = await discover_captures(target, from_year=from_year, to_year=to_year, limit=200)
        db.add(
            AuditEvent(
                investigation_id=investigation.id,
                event_type="wayback_discovery_run",
                details={
                    "target": target,
                    "from_year": from_year,
                    "to_year": to_year,
                    "capture_count": len(captures),
                    "prefix_target": prefix_target,
                },
            )
        )
        db.commit()
        error = None
    except (ValueError, WaybackError) as exc:
        target = raw_target
        captures = []
        error = str(exc)
    return templates.TemplateResponse(
        request,
        "wayback.html",
        context(
            request,
            investigation=investigation,
            candidates=candidates,
            automatic_results=automatic_wayback_page_results(investigation),
            target=target,
            captures=captures,
            error=error,
            save_result=None,
            save_error=None,
        ),
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT if error else status.HTTP_200_OK,
    )


@router.post("/investigations/{investigation_id}/wayback/evolution", response_class=HTMLResponse)
async def wayback_identity_evolution(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    original_url = str(form.get("original_url", ""))[:2048]
    timestamps = [str(value)[:14] for value in form.getlist("timestamp")]
    captures = [
        WaybackCapture(
            timestamp=timestamp,
            original_url=original_url,
            status_code=200,
            mime_type="text/html",
            digest=None,
            length=None,
        )
        for timestamp in timestamps
    ]
    try:
        settings = get_settings()
        image_cache = ImageCache(
            settings.data_dir / "wayback-image-cache",
            max_bytes=settings.image_cache_max_bytes,
        )
        evolution = await build_identity_evolution(captures, image_cache=image_cache, limit=25)
    except (ValueError, WaybackError) as exc:
        return templates.TemplateResponse(
            request,
            "wayback_evolution.html",
            context(
                request,
                investigation=investigation,
                evolution=None,
                error=str(exc),
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="wayback_identity_evolution",
            details={
                "original_url": evolution.original_url,
                "identity_label": evolution.identity_label,
                "identity_first_seen": evolution.identity_first_seen.isoformat(),
                "first_seen": evolution.first_seen.isoformat(),
                "last_seen": evolution.last_seen.isoformat(),
                "available_captures": evolution.available_capture_count,
                "analysed_captures": evolution.analysed_capture_count,
                "change_count": len(evolution.changes),
                "change_types": sorted({change.change_type for change in evolution.changes}),
            },
        )
    )
    db.commit()
    return templates.TemplateResponse(
        request,
        "wayback_evolution.html",
        context(
            request,
            investigation=investigation,
            evolution=evolution,
            error=None,
        ),
    )


@router.post("/investigations/{investigation_id}/wayback/save", response_class=HTMLResponse)
async def save_wayback_page(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    candidates = sorted(
        {
            finding.canonical_url
            for finding in investigation.findings
            if finding.canonical_url and finding.status in {"found", "likely"}
        }
    )
    raw_target = str(form.get("target", ""))[:2048]
    try:
        result = await save_page_now(raw_target)
    except (ValueError, WaybackError) as exc:
        db.add(
            AuditEvent(
                investigation_id=investigation.id,
                event_type="wayback_save_page_now",
                details={
                    "target": redact(raw_target),
                    "outcome": "failed",
                    "error": str(exc),
                },
            )
        )
        db.commit()
        return templates.TemplateResponse(
            request,
            "wayback.html",
            context(
                request,
                investigation=investigation,
                candidates=candidates,
                automatic_results=automatic_wayback_page_results(investigation),
                target=redact(raw_target),
                captures=[],
                error=None,
                save_result=None,
                save_error=str(exc),
            ),
            status_code=(
                status.HTTP_422_UNPROCESSABLE_CONTENT
                if isinstance(exc, ValueError)
                else status.HTTP_502_BAD_GATEWAY
            ),
        )

    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="wayback_save_page_now",
            details={
                "target": result.target_url,
                "outcome": "submitted",
                "submitted_at": result.submitted_at.isoformat(),
                "status_code": result.status_code,
                "archive_url": result.archive_url,
                "job_id": result.job_id,
            },
        )
    )
    db.commit()
    return templates.TemplateResponse(
        request,
        "wayback.html",
        context(
            request,
            investigation=investigation,
            candidates=candidates,
            automatic_results=automatic_wayback_page_results(investigation),
            target=result.target_url,
            captures=[],
            error=None,
            save_result=result,
            save_error=None,
        ),
    )


@router.post("/investigations/{investigation_id}/wayback/compare", response_class=HTMLResponse)
async def compare_wayback_captures(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    original_url = str(form.get("original_url", ""))[:2048]
    first_timestamp = str(form.get("first_timestamp", ""))[:14]
    latest_timestamp = str(form.get("latest_timestamp", ""))[:14]
    try:
        settings = get_settings()
        image_cache = ImageCache(
            settings.data_dir / "wayback-image-cache",
            max_bytes=settings.image_cache_max_bytes,
        )
        comparison = await compare_archived_pages(
            original_url,
            first_timestamp,
            latest_timestamp,
            image_cache=image_cache,
        )
        db.add(
            AuditEvent(
                investigation_id=investigation.id,
                event_type="wayback_capture_comparison",
                details={
                    "original_url": original_url,
                    "first_timestamp": first_timestamp,
                    "latest_timestamp": latest_timestamp,
                    "first_sha256": comparison.first.content_sha256,
                    "latest_sha256": comparison.latest.content_sha256,
                    "avatar": (
                        {
                            "classification": comparison.avatar.classification,
                            "distance": comparison.avatar.distance,
                            "first_source_url": comparison.avatar.first_source_url,
                            "latest_source_url": comparison.avatar.latest_source_url,
                            "first_sha256": comparison.avatar.first_sha256,
                            "latest_sha256": comparison.avatar.latest_sha256,
                            "error": comparison.avatar.error,
                        }
                        if comparison.avatar
                        else None
                    ),
                },
            )
        )
        db.commit()
        error = None
    except (ValueError, WaybackError) as exc:
        comparison = None
        error = str(exc)
    return templates.TemplateResponse(
        request,
        "wayback_comparison.html",
        context(
            request,
            investigation=investigation,
            comparison=comparison,
            error=error,
        ),
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT if error else status.HTTP_200_OK,
    )


@router.get("/investigations/{investigation_id}/wayback/image")
async def wayback_image(
    investigation_id: str,
    timestamp: str,
    source_url: str,
    db: Session = Depends(get_db),
) -> Response:
    load_investigation(db, investigation_id)
    try:
        archive_url = archived_asset_url(timestamp, source_url)
        settings = get_settings()
        cache = ImageCache(
            settings.data_dir / "wayback-image-cache",
            max_bytes=settings.image_cache_max_bytes,
        )
        cached = await cache.fetch(archive_url)
    except (ValueError, ImageCacheError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "archived image unavailable") from exc
    return Response(
        cached.path.read_bytes(),
        media_type=cached.mime_type,
        headers={
            "Cache-Control": "private, max-age=86400",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/findings/{finding_id}/avatar")
async def finding_avatar(finding_id: str, db: Session = Depends(get_db)) -> Response:
    finding = db.get(Finding, finding_id)
    if not finding:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "finding not found")
    profile = finding.extra.get("profile", {})
    avatar_url = profile.get("avatar_url") if isinstance(profile, dict) else None
    if not isinstance(avatar_url, str) or not avatar_url:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "avatar unavailable")
    settings = get_settings()
    local_path = finding.extra.get("avatar_local_path")
    if isinstance(local_path, str) and local_path:
        try:
            local = load_local_image(
                local_path,
                data_dir=settings.data_dir,
                max_bytes=settings.image_cache_max_bytes,
            )
        except LocalImageEvidenceError as exc:
            finding.extra = {
                **finding.extra,
                "local_image_error": str(exc),
            }
            db.commit()
        else:
            finding.extra = {
                **finding.extra,
                "image_cache": local.metadata(avatar_url if isinstance(avatar_url, str) else None),
            }
            db.commit()
            return Response(
                local.path.read_bytes(),
                media_type=local.mime_type,
                headers={
                    "Cache-Control": "private, max-age=86400",
                    "Content-Security-Policy": "default-src 'none'; sandbox",
                    "X-Content-Type-Options": "nosniff",
                },
            )
    cache = ImageCache(settings.data_dir / "image-cache", max_bytes=settings.image_cache_max_bytes)
    try:
        cached = await cache.fetch(avatar_url)
    except ImageCacheError as exc:
        finding.extra = {
            **finding.extra,
            "image_cache": {"source_url": avatar_url, "error": str(exc)},
        }
        db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "avatar retrieval failed") from exc
    finding.extra = {**finding.extra, "image_cache": cached.metadata()}
    db.commit()
    return Response(
        cached.path.read_bytes(),
        media_type=cached.mime_type,
        headers={
            "Cache-Control": "private, max-age=86400",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _reverse_image_finding_target(
    finding_id: str, provider: str, db: Session
) -> tuple[Finding, str, Any]:
    finding = db.get(Finding, finding_id)
    if not finding:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "finding not found")
    profile = finding.extra.get("profile", {})
    avatar_url = profile.get("avatar_url") if isinstance(profile, dict) else None
    if not isinstance(avatar_url, str) or not avatar_url:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "avatar unavailable")
    try:
        target = reverse_image_target(provider, avatar_url)
    except ReverseImageSearchError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return finding, avatar_url, target


@router.get("/findings/{finding_id}/reverse-image/{provider}")
async def reverse_image_search_confirmation(
    finding_id: str,
    provider: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    finding, _avatar_url, target = _reverse_image_finding_target(finding_id, provider, db)
    return templates.TemplateResponse(
        request,
        "reverse_image_confirm.html",
        context(
            request,
            finding=finding,
            target=target,
        ),
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post("/findings/{finding_id}/reverse-image/{provider}")
async def reverse_image_search(
    finding_id: str,
    provider: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    if str(form.get("approved", "")) != "on":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "explicit per-image reverse-search approval is required",
        )
    finding, _avatar_url, target = _reverse_image_finding_target(finding_id, provider, db)
    destination_host = urlsplit(target.url).hostname or ""
    try:
        decision = validate_external_egress(
            target.url,
            payload_kind=EgressPayloadKind.MEDIA,
            media_approved=True,
            allowed_hosts=frozenset({destination_host}),
        )
    except EgressPolicyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    image_cache = finding.extra.get("image_cache", {})
    db.add(
        AuditEvent(
            investigation_id=finding.investigation_id,
            event_type="reverse_image_search_launched",
            details={
                "finding_id": finding.id,
                "provider": target.provider,
                "provider_label": target.label,
                "platform": finding.platform,
                "avatar_sha256": (
                    image_cache.get("sha256") if isinstance(image_cache, dict) else None
                ),
                "operator_confirmed": True,
                "payload_kind": decision.payload_kind.value,
                "destination_host": destination_host,
            },
        )
    )
    db.commit()
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    if target.method == "get":
        return RedirectResponse(target.url, status_code=status.HTTP_302_FOUND, headers=headers)

    fields = "".join(
        f'<input type="hidden" name="{escape(name, quote=True)}" '
        f'value="{escape(value, quote=True)}">'
        for name, value in target.fields.items()
    )
    body = (
        '<!doctype html><html><head><meta charset="utf-8"><title>Opening '
        f"{escape(target.label)}</title></head><body>"
        f'<form id="reverse-image-form" method="post" action="{escape(target.url, quote=True)}">'
        f'{fields}<button type="submit">Continue to {escape(target.label)}</button></form>'
        '<script>document.getElementById("reverse-image-form").submit();</script>'
        "</body></html>"
    )
    return HTMLResponse(
        body,
        headers={
            **headers,
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'unsafe-inline'; "
                "form-action https://www.bing.com; base-uri 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/investigations/{investigation_id}/enrich")
async def enrich_investigation(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    candidates = [
        (finding.id, finding.canonical_url)
        for finding in investigation.findings
        if finding.canonical_url and finding.status in {"found", "likely"}
    ][:25]
    settings = get_settings()
    respect_robots = str(form.get("respect_robots", "")) == "on"
    enricher = ProfileEnricher(
        max_bytes=settings.profile_enrichment_max_bytes,
        minimum_interval_seconds=settings.profile_enrichment_interval_seconds,
        respect_robots=respect_robots,
    )
    enriched = 0
    failed = 0
    for finding_id, url in candidates:
        assert url is not None
        finding = db.get(Finding, finding_id)
        if not finding:
            continue
        try:
            result = await enricher.fetch(url)
        except ProfileEnrichmentError as exc:
            finding.extra = {
                **finding.extra,
                "enrichment": {
                    "source_url": url,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "error": str(exc),
                    "robots_policy": "respected" if respect_robots else "ignored_by_analyst",
                },
            }
            failed += 1
            continue
        metadata = result.metadata()
        profile = dict(finding.extra.get("profile", {}))
        if result.avatar_url and not profile.get("avatar_url"):
            profile["avatar_url"] = result.avatar_url
        metadata["robots_policy"] = "respected" if respect_robots else "ignored_by_analyst"
        finding.extra = {**finding.extra, "profile": profile, "enrichment": metadata}
        if result.title and not finding.title:
            finding.title = result.title
        if result.description and not finding.description:
            finding.description = result.description
        enriched += 1
    db.add(
        AuditEvent(
            investigation_id=investigation_id,
            event_type="profile_enrichment_completed",
            details={
                "candidates": len(candidates),
                "enriched": enriched,
                "failed": failed,
                "robots_policy": "respected" if respect_robots else "ignored_by_analyst",
            },
        )
    )
    db.commit()
    return RedirectResponse(f"/investigations/{investigation_id}", status_code=303)


async def _retained_avatar_path(
    finding: Finding, *, settings: Any, cache: ImageCache
) -> tuple[Path, dict[str, Any]]:
    profile = finding.extra.get("profile", {})
    avatar_url = profile.get("avatar_url") if isinstance(profile, dict) else None
    if not isinstance(avatar_url, str) or not avatar_url:
        raise FaceMatchingError("avatar unavailable")

    local_path = finding.extra.get("avatar_local_path")
    if isinstance(local_path, str) and local_path:
        try:
            local = load_local_image(
                local_path,
                data_dir=settings.data_dir,
                max_bytes=settings.image_cache_max_bytes,
            )
        except LocalImageEvidenceError:
            pass
        else:
            return local.path, local.metadata(avatar_url)

    try:
        cached = await cache.fetch(avatar_url)
    except ImageCacheError as exc:
        raise FaceMatchingError(str(exc)) from exc
    return cached.path, cached.metadata()


@router.post("/investigations/{investigation_id}/correlate-images")
async def correlate_images(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    settings = get_settings()
    try:
        threshold = int(str(form.get("threshold", settings.image_correlation_threshold)))
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid image threshold"
        ) from exc
    if not 0 <= threshold <= 64:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid image threshold")

    candidates = []
    for finding in investigation.findings:
        profile = finding.extra.get("profile", {})
        avatar_url = profile.get("avatar_url") if isinstance(profile, dict) else None
        if isinstance(avatar_url, str) and avatar_url:
            candidates.append((finding.id, avatar_url))
    candidates = candidates[: settings.image_correlation_max_candidates]

    cache = ImageCache(settings.data_dir / "image-cache", max_bytes=settings.image_cache_max_bytes)
    fingerprints: dict[str, ImageFingerprint] = {}
    failures = 0
    for finding_id, avatar_url in candidates:
        loaded_finding = db.get(Finding, finding_id)
        if not loaded_finding:
            continue
        try:
            cached = await cache.fetch(avatar_url)
            fingerprint = ImageFingerprint(cached.sha256, perceptual_dhash(cached.path))
        except (ImageCacheError, ImageCorrelationError) as exc:
            loaded_finding.extra = {
                **loaded_finding.extra,
                "image_correlation": {"error": str(exc), "matches": []},
            }
            failures += 1
            continue
        previous = loaded_finding.extra.get("image_correlation", {})
        decisions = (
            {
                str(item.get("peer_id")): str(item.get("decision", "unreviewed"))
                for item in previous.get("matches", [])
                if isinstance(item, dict) and item.get("peer_id")
            }
            if isinstance(previous, dict)
            else {}
        )
        loaded_finding.extra = {
            **loaded_finding.extra,
            "image_cache": cached.metadata(),
            "image_correlation": {
                "sha256": fingerprint.sha256,
                "dhash": fingerprint.dhash,
                "threshold": threshold,
                "matches": [],
                "decisions": decisions,
            },
        }
        fingerprints[loaded_finding.id] = fingerprint

    matches = correlate_fingerprints(fingerprints, threshold=threshold)
    for match in matches:
        for finding_id, peer_id in (
            (match.left_id, match.right_id),
            (match.right_id, match.left_id),
        ):
            matched_finding = db.get(Finding, finding_id)
            if not matched_finding:
                continue
            correlation = dict(matched_finding.extra.get("image_correlation", {}))
            decisions = correlation.pop("decisions", {})
            decision = (
                decisions.get(peer_id, "unreviewed")
                if isinstance(decisions, dict)
                else "unreviewed"
            )
            entries = list(correlation.get("matches", []))
            entries.append(
                {
                    "peer_id": peer_id,
                    "match_type": match.match_type,
                    "distance": match.distance,
                    "decision": decision,
                }
            )
            correlation["matches"] = entries
            matched_finding.extra = {
                **matched_finding.extra,
                "image_correlation": correlation,
            }

    for finding_id in fingerprints:
        fingerprinted_finding = db.get(Finding, finding_id)
        if fingerprinted_finding:
            correlation = dict(fingerprinted_finding.extra.get("image_correlation", {}))
            correlation.pop("decisions", None)
            fingerprinted_finding.extra = {
                **fingerprinted_finding.extra,
                "image_correlation": correlation,
            }

    db.add(
        AuditEvent(
            investigation_id=investigation_id,
            event_type="image_correlation_completed",
            details={
                "candidates": len(candidates),
                "fingerprinted": len(fingerprints),
                "matches": len(matches),
                "failures": failures,
                "threshold": threshold,
            },
        )
    )
    db.commit()
    return RedirectResponse(f"/investigations/{investigation_id}", status_code=303)


@router.post("/investigations/{investigation_id}/match-faces")
async def match_faces_locally(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    settings = get_settings()
    try:
        threshold = float(str(form.get("threshold", settings.face_match_threshold)))
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid face similarity threshold"
        ) from exc
    if not 0.0 <= threshold <= 1.0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid face similarity threshold"
        )

    query_findings = []
    for finding in investigation.findings:
        profile = finding.extra.get("profile", {})
        if isinstance(profile, dict) and isinstance(profile.get("avatar_url"), str):
            query_findings.append(finding)
    query_findings = query_findings[: settings.face_match_max_candidates]
    query_ids = {finding.id for finding in query_findings}

    try:
        matcher = FaceMatcher(
            settings.face_detection_model_path,
            settings.face_recognition_model_path,
            detection_threshold=settings.face_detection_threshold,
        )
    except FaceMatchingError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    recent_findings = list(
        db.scalars(
            select(Finding)
            .where(Finding.status.in_(["found", "likely"]))
            .order_by(Finding.observed_at.desc())
            .limit(settings.face_match_max_candidates * 4)
        )
    )
    candidate_map = {finding.id: finding for finding in query_findings}
    for finding in recent_findings:
        image_cache = finding.extra.get("image_cache", {})
        retained = bool(
            finding.extra.get("avatar_local_path")
            or (isinstance(image_cache, dict) and image_cache.get("sha256"))
        )
        if retained and finding.id not in candidate_map:
            candidate_map[finding.id] = finding
        if len(candidate_map) >= settings.face_match_max_candidates:
            break

    cache = ImageCache(settings.data_dir / "image-cache", max_bytes=settings.image_cache_max_bytes)
    embeddings = {}
    failures = 0
    previous_decisions: dict[tuple[str, str], str] = {}
    for finding in query_findings:
        previous = finding.extra.get("face_matching", {})
        if isinstance(previous, dict):
            for match in previous.get("matches", []):
                if isinstance(match, dict) and match.get("peer_id"):
                    previous_decisions[(finding.id, str(match["peer_id"]))] = str(
                        match.get("decision", "unreviewed")
                    )

    for finding in candidate_map.values():
        try:
            path, image_metadata = await _retained_avatar_path(
                finding, settings=settings, cache=cache
            )
            embedding = await asyncio.to_thread(matcher.embed, path)
        except FaceMatchingError as exc:
            failures += 1
            if finding.id in query_ids:
                finding.extra = {
                    **finding.extra,
                    "face_matching": {
                        "model": "OpenCV YuNet + SFace",
                        "threshold": threshold,
                        "matches": [],
                        "error": str(exc),
                    },
                }
            continue
        embeddings[finding.id] = embedding
        if finding.id in query_ids:
            finding.extra = {
                **finding.extra,
                "image_cache": image_metadata,
                "face_matching": {
                    "model": "OpenCV YuNet + SFace",
                    "threshold": threshold,
                    "detection_confidence": embedding.detection_confidence,
                    "bounding_box": list(embedding.bounding_box),
                    "faces_detected": embedding.faces_detected,
                    "matches": [],
                },
            }

    matches = correlate_faces(
        embeddings,
        query_ids=query_ids,
        threshold=threshold,
        maximum_matches_per_query=settings.face_match_max_per_finding,
    )
    for match in matches:
        matched_finding = candidate_map.get(match.left_id)
        peer_finding = candidate_map.get(match.right_id)
        if not matched_finding or not peer_finding:
            continue
        correlation = dict(matched_finding.extra.get("face_matching", {}))
        entries = list(correlation.get("matches", []))
        peer_profile = peer_finding.extra.get("profile", {})
        peer_investigation = db.get(Investigation, peer_finding.investigation_id)
        entries.append(
            {
                "peer_id": peer_finding.id,
                "peer_investigation_id": peer_finding.investigation_id,
                "peer_investigation_title": (
                    peer_investigation.title if peer_investigation else "Unknown investigation"
                ),
                "peer_platform": peer_finding.platform,
                "peer_value": peer_finding.value,
                "peer_display_name": (
                    peer_profile.get("display_name") if isinstance(peer_profile, dict) else None
                ),
                "similarity": round(match.similarity, 6),
                "decision": previous_decisions.get(
                    (matched_finding.id, peer_finding.id), "unreviewed"
                ),
            }
        )
        correlation["matches"] = entries
        matched_finding.extra = {
            **matched_finding.extra,
            "face_matching": correlation,
        }

    db.add(
        AuditEvent(
            investigation_id=investigation_id,
            event_type="local_face_matching_completed",
            details={
                "query_candidates": len(query_findings),
                "local_corpus_candidates": len(candidate_map),
                "embedded": len(embeddings),
                "matches": len(matches),
                "failures": failures,
                "threshold": threshold,
                "model": "OpenCV YuNet + SFace",
            },
        )
    )
    db.commit()
    return RedirectResponse(f"/investigations/{investigation_id}#findings-section", status_code=303)


@router.post("/investigations/{investigation_id}/face-match/{finding_id}/{peer_id}")
async def review_face_match(
    investigation_id: str,
    finding_id: str,
    peer_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    decision = str(form.get("decision", ""))
    if decision not in {"accepted", "rejected", "unreviewed"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid match decision")
    investigation = load_investigation(db, investigation_id)
    finding = next((item for item in investigation.findings if item.id == finding_id), None)
    if not finding:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "face match not found")

    correlation = dict(finding.extra.get("face_matching", {}))
    entries = []
    updated = False
    for entry in correlation.get("matches", []):
        if isinstance(entry, dict) and entry.get("peer_id") == peer_id:
            entry = {**entry, "decision": decision}
            updated = True
        entries.append(entry)
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "face match not found")
    correlation["matches"] = entries
    finding.extra = {**finding.extra, "face_matching": correlation}
    db.add(
        AuditEvent(
            investigation_id=investigation_id,
            event_type="face_match_reviewed",
            details={
                "finding_id": finding_id,
                "peer_id": peer_id,
                "decision": decision,
            },
        )
    )
    db.commit()
    return RedirectResponse(f"/investigations/{investigation_id}#findings-section", status_code=303)


@router.post("/investigations/{investigation_id}/image-match/{finding_id}/{peer_id}")
async def review_image_match(
    investigation_id: str,
    finding_id: str,
    peer_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    decision = str(form.get("decision", ""))
    if decision not in {"accepted", "rejected", "unreviewed"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid match decision")
    investigation = load_investigation(db, investigation_id)
    finding = next((item for item in investigation.findings if item.id == finding_id), None)
    peer = next((item for item in investigation.findings if item.id == peer_id), None)
    if not finding or not peer:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "image match not found")

    updated = False
    for item, other_id in ((finding, peer_id), (peer, finding_id)):
        correlation = dict(item.extra.get("image_correlation", {}))
        entries = []
        for entry in correlation.get("matches", []):
            if isinstance(entry, dict) and entry.get("peer_id") == other_id:
                entry = {**entry, "decision": decision}
                updated = True
            entries.append(entry)
        correlation["matches"] = entries
        item.extra = {**item.extra, "image_correlation": correlation}
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "image match not found")

    db.add(
        AuditEvent(
            investigation_id=investigation_id,
            event_type="image_match_reviewed",
            details={
                "finding_id": finding_id,
                "peer_id": peer_id,
                "decision": decision,
            },
        )
    )
    db.commit()
    return RedirectResponse(f"/investigations/{investigation_id}", status_code=303)


def _save_cluster_controls(db: Session, investigation_id: str, value: dict[str, Any]) -> None:
    key = cluster_setting_key(investigation_id)
    setting = db.get(ApplicationSetting, key)
    if setting is None:
        db.add(ApplicationSetting(key=key, value=value))
    else:
        setting.value = value
        setting.updated_at = datetime.now(UTC)


@router.post("/investigations/{investigation_id}/clusters/pair")
async def update_evidence_cluster_pair(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    left_id = str(form.get("left_id", ""))
    right_id = str(form.get("right_id", ""))
    action = str(form.get("action", ""))
    setting = db.get(ApplicationSetting, cluster_setting_key(investigation.id))
    current = setting.value if setting is not None else None
    try:
        updated = update_pair_override(
            current,
            {finding.id for finding in investigation.findings},
            left_id,
            right_id,
            action,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    _save_cluster_controls(db, investigation.id, updated)
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="evidence_cluster_pair_overridden",
            details={"left_id": left_id, "right_id": right_id, "action": action},
        )
    )
    db.commit()
    return RedirectResponse(f"/investigations/{investigation.id}#analysis", status_code=303)


@router.post("/investigations/{investigation_id}/clusters/{cluster_id}/note")
async def update_evidence_cluster_note(
    investigation_id: str,
    cluster_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    setting = db.get(ApplicationSetting, cluster_setting_key(investigation.id))
    current = setting.value if setting is not None else None
    valid_ids = {finding.id for finding in investigation.findings}
    clusters = build_evidence_clusters(investigation, current)
    if cluster_id not in {cluster.id for cluster in clusters}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "evidence cluster not found")
    updated = update_cluster_note(current, valid_ids, cluster_id, str(form.get("note", "")))
    _save_cluster_controls(db, investigation.id, updated)
    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type="evidence_cluster_note_updated",
            details={"cluster_id": cluster_id, "has_note": bool(str(form.get("note", "")).strip())},
        )
    )
    db.commit()
    return RedirectResponse(f"/investigations/{investigation.id}#analysis", status_code=303)


@router.post("/investigations/{investigation_id}/correlate-accounts")
async def correlate_account_evidence(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    try:
        minimum_score = int(str(form.get("minimum_score", "35")))
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid correlation score"
        ) from exc
    if not 0 <= minimum_score <= 100:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid correlation score")

    evidence: dict[str, AccountEvidence] = {}
    previous_decisions: dict[tuple[str, str], str] = {}
    for finding in investigation.findings:
        profile = finding.extra.get("profile", {})
        if not isinstance(profile, dict):
            profile = {}
        existing = finding.extra.get("account_correlation", {})
        if isinstance(existing, dict):
            for match in existing.get("matches", []):
                if isinstance(match, dict) and match.get("peer_id"):
                    previous_decisions[(finding.id, str(match["peer_id"]))] = str(
                        match.get("decision", "unreviewed")
                    )
        image_matches: dict[str, tuple[str, int]] = {}
        image_data = finding.extra.get("image_correlation", {})
        if isinstance(image_data, dict):
            for match in image_data.get("matches", []):
                if isinstance(match, dict) and match.get("peer_id"):
                    image_matches[str(match["peer_id"])] = (
                        str(match.get("match_type", "perceptual")),
                        int(match.get("distance", 64)),
                    )
        username = profile.get("account_id") or finding.value or investigation.query
        evidence[finding.id] = AccountEvidence(
            finding_id=finding.id,
            username=str(username) if username else None,
            display_name=str(profile.get("display_name")) if profile.get("display_name") else None,
            biography=str(profile.get("biography")) if profile.get("biography") else None,
            location=str(profile.get("location")) if profile.get("location") else None,
            domains=extract_domains(
                {"canonical_url": finding.canonical_url, "extra": finding.extra}
            ),
            urls=extract_urls(finding.extra),
            image_matches=image_matches,
        )

    matches = correlate_accounts(evidence, minimum_score=minimum_score)
    match_map: dict[str, list[dict[str, object]]] = {
        finding.id: [] for finding in investigation.findings
    }
    for match in matches:
        for finding_id, peer_id in (
            (match.left_id, match.right_id),
            (match.right_id, match.left_id),
        ):
            match_map[finding_id].append(
                {
                    "peer_id": peer_id,
                    "score": match.score,
                    "signals": list(match.signals),
                    "decision": previous_decisions.get((finding_id, peer_id), "unreviewed"),
                }
            )
    for finding in investigation.findings:
        finding.extra = {
            **finding.extra,
            "account_correlation": {
                "minimum_score": minimum_score,
                "matches": match_map[finding.id],
            },
        }

    db.add(
        AuditEvent(
            investigation_id=investigation_id,
            event_type="account_correlation_completed",
            details={
                "findings": len(evidence),
                "matches": len(matches),
                "minimum_score": minimum_score,
            },
        )
    )
    db.commit()
    return RedirectResponse(f"/investigations/{investigation_id}", status_code=303)


@router.post("/investigations/{investigation_id}/account-match/{finding_id}/{peer_id}")
async def review_account_match(
    investigation_id: str,
    finding_id: str,
    peer_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    decision = str(form.get("decision", ""))
    if decision not in {"accepted", "rejected", "unreviewed"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid match decision")
    investigation = load_investigation(db, investigation_id)
    updated = False
    for finding_id_value, other_id in ((finding_id, peer_id), (peer_id, finding_id)):
        finding = next(
            (item for item in investigation.findings if item.id == finding_id_value), None
        )
        if not finding:
            continue
        correlation = dict(finding.extra.get("account_correlation", {}))
        entries = []
        for entry in correlation.get("matches", []):
            if isinstance(entry, dict) and entry.get("peer_id") == other_id:
                entry = {**entry, "decision": decision}
                updated = True
            entries.append(entry)
        correlation["matches"] = entries
        finding.extra = {**finding.extra, "account_correlation": correlation}
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account match not found")
    db.add(
        AuditEvent(
            investigation_id=investigation_id,
            event_type="account_match_reviewed",
            details={"finding_id": finding_id, "peer_id": peer_id, "decision": decision},
        )
    )
    db.commit()
    return RedirectResponse(f"/investigations/{investigation_id}", status_code=303)


@router.get("/investigations/{investigation_id}/events")
async def events(investigation_id: str, db: Session = Depends(get_db)) -> StreamingResponse:
    load_investigation(db, investigation_id)

    async def stream() -> AsyncIterator[str]:
        async for queue in job_manager.subscribe(investigation_id):
            snapshot = job_manager.live_snapshot(investigation_id)
            if snapshot is not None:
                yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
                if snapshot["status"] not in {"queued", "running"}:
                    yield f"event: complete\ndata: {json.dumps({'status': snapshot['status']})}\n\n"
                    return
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), 15)
                    yield f"event: {message['event']}\ndata: {json.dumps(message['data'])}\n\n"
                    if message["event"] == "complete":
                        return
                except TimeoutError:
                    # EventSource reconnects do not replay the in-memory queue.  Periodically send
                    # authoritative persisted state so dropped/disconnected terminal events cannot
                    # leave the browser showing a collector as running forever.
                    snapshot = job_manager.live_snapshot(investigation_id)
                    if snapshot is None:
                        return
                    yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
                    if snapshot["status"] not in {"queued", "running"}:
                        yield f"event: complete\ndata: {json.dumps({'status': snapshot['status']})}\n\n"
                        return

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/investigations/{investigation_id}/validate-profiles")
async def validate_investigation_profiles(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    investigation = load_investigation(db, investigation_id)
    if investigation.status in {"queued", "running"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "account validation already runs automatically when collection finishes",
        )
    started = job_manager.start_profile_validation(investigation_id)
    db.add(
        AuditEvent(
            investigation_id=investigation_id,
            event_type="profile_validation_requested",
            details={"started": started},
        )
    )
    db.commit()
    state = "started" if started else "running"
    return RedirectResponse(
        f"/investigations/{investigation_id}?validation={state}#findings-section",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/investigations/{investigation_id}/cancel")
async def cancel_investigation(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    load_investigation(db, investigation_id)
    await job_manager.cancel(investigation_id)
    db.add(
        AuditEvent(investigation_id=investigation_id, event_type="execution_cancelled", details={})
    )
    db.commit()
    return RedirectResponse(f"/investigations/{investigation_id}", status_code=303)


@router.post("/investigations/{investigation_id}/update")
async def update_investigation(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    item = load_investigation(db, investigation_id)
    title, notes = str(form.get("title", "")).strip(), str(form.get("notes", ""))
    if not title or len(title) > 200 or len(notes) > 10000:
        raise HTTPException(422, "invalid title or notes")
    item.title, item.notes, item.updated_at = title, notes, datetime.now(UTC)
    db.commit()
    return RedirectResponse(f"/investigations/{investigation_id}", status_code=303)


@router.post("/investigations/delete-selected")
async def delete_selected_investigations(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    if str(form.get("confirm", "")) != "DELETE SELECTED":
        raise HTTPException(422, "type DELETE SELECTED to confirm")
    investigation_ids = tuple(
        dict.fromkeys(str(value) for value in form.getlist("investigation_ids") if value)
    )
    if not investigation_ids or len(investigation_ids) > 50:
        raise HTTPException(422, "select between 1 and 50 investigations")
    items = list(
        db.scalars(
            select(Investigation)
            .options(
                selectinload(Investigation.runs),
                selectinload(Investigation.findings),
                selectinload(Investigation.location_observations),
                selectinload(Investigation.browser_runs),
            )
            .where(Investigation.id.in_(investigation_ids))
        )
    )
    if len(items) != len(investigation_ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "one or more investigations were not found")
    if any(item.status in {"queued", "running"} for item in items):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "cancel active investigations before deleting them",
        )

    deletion_details: dict[str, dict[str, int]] = {}
    for item in items:
        deletion_details[item.id] = await purge_investigation_artifacts(item)
    for item in items:
        db.execute(delete(AuditEvent).where(AuditEvent.investigation_id == item.id))
        graph_setting = db.get(ApplicationSetting, graph_layout_setting_key(item.id))
        if graph_setting is not None:
            db.delete(graph_setting)
        _discard_dork_run(db, item.id)
        db.add(
            AuditEvent(
                investigation_id=item.id,
                event_type="investigation_deleted",
                details={"bulk_delete": True, **deletion_details[item.id]},
            )
        )
        db.delete(item)
    db.commit()
    return RedirectResponse(f"/?deleted={len(items)}", status_code=303)


@router.post("/investigations/{investigation_id}/delete")
async def delete_investigation(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    if str(form.get("confirm", "")) != "DELETE":
        raise HTTPException(422, "type DELETE to confirm")
    item = load_investigation(db, investigation_id)
    deletion_details = await purge_investigation_artifacts(item)
    db.execute(delete(AuditEvent).where(AuditEvent.investigation_id == item.id))
    graph_setting = db.get(ApplicationSetting, graph_layout_setting_key(item.id))
    if graph_setting is not None:
        db.delete(graph_setting)
    _discard_dork_run(db, item.id)
    db.add(
        AuditEvent(
            investigation_id=investigation_id,
            event_type="investigation_deleted",
            details={"bulk_delete": False, **deletion_details},
        )
    )
    db.delete(item)
    db.commit()
    return RedirectResponse("/?deleted=1", status_code=303)


@router.post("/investigations/{investigation_id}/auto-archiver/bundle")
async def auto_archiver_bundle(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    requested_ids = tuple(dict.fromkeys(str(value) for value in form.getlist("finding_ids")))
    if not requested_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "select a finding to archive")
    item = load_investigation(db, investigation_id)
    findings_by_id = {finding.id: finding for finding in item.findings}
    if any(finding_id not in findings_by_id for finding_id in requested_ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "selected finding was not found")

    candidates: list[AutoArchiverCandidate] = []
    for finding_id in requested_ids:
        finding = findings_by_id[finding_id]
        if not finding.canonical_url:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "selected finding does not contain a public URL",
            )
        candidates.append(
            AutoArchiverCandidate(
                finding_id=finding.id,
                url=finding.canonical_url,
                platform=finding.platform,
                status=finding.status,
                source_tools=tuple(finding.source_tools or [finding.source_tool]),
                observed_at=finding.observed_at,
            )
        )
    try:
        bundle = build_auto_archiver_bundle(
            investigation_id=item.id,
            investigation_title=item.title,
            candidates=tuple(candidates),
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    db.add(
        AuditEvent(
            investigation_id=item.id,
            event_type="auto_archiver_bundle_exported",
            details={
                "finding_ids": requested_ids,
                "candidate_count": bundle.candidate_count,
                "auto_archiver_version": AUTO_ARCHIVER_VERSION,
                "auto_archiver_commit": AUTO_ARCHIVER_COMMIT,
                "bundle_sha256": bundle.sha256,
                "manifest_sha256": bundle.manifest_sha256,
            },
        )
    )
    db.commit()
    return Response(
        bundle.content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.filename}"',
            "X-Content-SHA256": bundle.sha256,
        },
    )


@router.get("/investigations/{investigation_id}/report", response_class=HTMLResponse)
def report_preview(investigation_id: str, db: Session = Depends(get_db)) -> Response:
    item = load_investigation(db, investigation_id)
    return Response(render_html_report(item, interactive=True), media_type="text/html")


@router.post("/investigations/{investigation_id}/export/bundle")
async def export_evidence_bundle(
    investigation_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    item = load_investigation(db, investigation_id)
    selected_ids = frozenset(str(value) for value in form.getlist("finding_ids") if str(value))
    options = BundleOptions(
        finding_ids=selected_ids,
        redact_identifiers=str(form.get("redact_identifiers", "")) == "on",
        redact_notes=str(form.get("redact_notes", "")) == "on",
        redact_urls=str(form.get("redact_urls", "")) == "on",
        include_raw=str(form.get("include_raw", "")) == "on",
    )
    try:
        content = build_evidence_bundle(item, get_settings().data_dir, options)
    except EvidenceBundleError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    db.add(
        AuditEvent(
            investigation_id=investigation_id,
            event_type="portable_evidence_bundle_exported",
            details={
                "selected_findings": len(selected_ids),
                "selection_applied": bool(selected_ids),
                "redact_identifiers": options.redact_identifiers,
                "redact_notes": options.redact_notes,
                "redact_urls": options.redact_urls,
                "include_raw": options.include_raw,
                "bundle_sha256": hashlib.sha256(content).hexdigest(),
                "bundle_size_bytes": len(content),
            },
        )
    )
    db.commit()
    return Response(
        content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="corvus-evidence-{investigation_id}.zip"',
            "X-Content-SHA256": hashlib.sha256(content).hexdigest(),
        },
    )


@router.get("/investigations/{investigation_id}/export/{kind}")
def export(investigation_id: str, kind: str, db: Session = Depends(get_db)) -> Response:
    item = load_investigation(db, investigation_id)
    exporters = {
        "json": (json_export, "application/json"),
        "csv": (csv_export, "text/csv"),
        "html": (html_export, "text/html"),
    }
    if kind not in exporters:
        raise HTTPException(404, "export format not found")
    fn, media = exporters[kind]
    db.add(
        AuditEvent(
            investigation_id=investigation_id,
            event_type="investigation_exported",
            details={"format": kind},
        )
    )
    db.commit()
    return Response(
        fn(item),
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="investigation-{investigation_id}.{kind}"'
        },
    )
