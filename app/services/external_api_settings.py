from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.db import ApplicationSetting

COMPANIES_HOUSE_KEY = "companies_house_api_key"
ABUSEIPDB_KEY = "abuseipdb_api_key"
OPENPHISH_FEED_URL_KEY = "openphish_feed_url"
KEY_RE = re.compile(r"^[A-Za-z0-9._-]{12,256}$")


def _validate(value: Any, label: str) -> str:
    key = str(value or "").strip()
    if not KEY_RE.fullmatch(key):
        raise ValueError(f"{label} must be 12–256 characters using letters, numbers, '.', '_' or '-'")
    return key


def validate_companies_house_api_key(value: Any) -> str:
    return _validate(value, "Companies House API key")


def validate_abuseipdb_api_key(value: Any) -> str:
    return _validate(value, "AbuseIPDB API key")


def validate_openphish_feed_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url or len(url) > 2048:
        raise ValueError("OpenPhish feed URL is required")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise ValueError("OpenPhish feed URL must be an HTTPS URL without embedded credentials")
    if host != "openphish.com" and not host.endswith(".openphish.com"):
        raise ValueError("OpenPhish feed URL must use an OpenPhish host")
    return url


def _stored_key(
    db: Session,
    setting_key: str,
    validator: Callable[[Any], str],
    configured: str,
) -> str | None:
    setting = db.get(ApplicationSetting, setting_key)
    if setting is not None and isinstance(setting.value, dict):
        stored = setting.value.get("key")
        if isinstance(stored, str):
            try:
                return validator(stored)
            except ValueError:
                pass
    if configured.strip():
        try:
            return validator(configured)
        except ValueError:
            pass
    return None


def get_companies_house_api_key(db: Session) -> str | None:
    return _stored_key(
        db,
        COMPANIES_HOUSE_KEY,
        validate_companies_house_api_key,
        get_settings().companies_house_api_key,
    )


def get_abuseipdb_api_key(db: Session) -> str | None:
    return _stored_key(
        db,
        ABUSEIPDB_KEY,
        validate_abuseipdb_api_key,
        get_settings().abuseipdb_api_key,
    )


def get_openphish_feed_url(db: Session) -> str | None:
    return _stored_key(
        db,
        OPENPHISH_FEED_URL_KEY,
        validate_openphish_feed_url,
        get_settings().openphish_feed_url,
    )


def companies_house_configured(db: Session) -> bool:
    return get_companies_house_api_key(db) is not None


def abuseipdb_configured(db: Session) -> bool:
    return get_abuseipdb_api_key(db) is not None


def openphish_configured(db: Session) -> bool:
    return get_openphish_feed_url(db) is not None


def _save(db: Session, setting_key: str, key: str) -> None:
    setting = db.get(ApplicationSetting, setting_key)
    payload = {"key": key}
    if setting is None:
        db.add(ApplicationSetting(key=setting_key, value=payload))
    else:
        setting.value = payload


def save_companies_house_api_key(db: Session, value: Any) -> None:
    _save(db, COMPANIES_HOUSE_KEY, validate_companies_house_api_key(value))


def save_abuseipdb_api_key(db: Session, value: Any) -> None:
    _save(db, ABUSEIPDB_KEY, validate_abuseipdb_api_key(value))


def save_openphish_feed_url(db: Session, value: Any) -> None:
    _save(db, OPENPHISH_FEED_URL_KEY, validate_openphish_feed_url(value))


def _clear(db: Session, setting_key: str) -> None:
    setting = db.get(ApplicationSetting, setting_key)
    if setting is not None:
        db.delete(setting)


def clear_companies_house_api_key(db: Session) -> None:
    _clear(db, COMPANIES_HOUSE_KEY)


def clear_abuseipdb_api_key(db: Session) -> None:
    _clear(db, ABUSEIPDB_KEY)


def clear_openphish_feed_url(db: Session) -> None:
    _clear(db, OPENPHISH_FEED_URL_KEY)


def set_openphish_adapter_enabled(enabled: bool) -> None:
    from app.adapters.builtin import REGISTRY
    from app.adapters.openphish import OpenPhishAdapter
    from app.services.jobs import job_manager

    if enabled:
        REGISTRY[OpenPhishAdapter.tool_id] = OpenPhishAdapter
        job_manager.tool_limits.setdefault(
            OpenPhishAdapter.tool_id,
            asyncio.Semaphore(OpenPhishAdapter.concurrency_limit),
        )
    else:
        REGISTRY.pop(OpenPhishAdapter.tool_id, None)
        job_manager.tool_limits.pop(OpenPhishAdapter.tool_id, None)


def set_companies_house_adapter_enabled(enabled: bool) -> None:
    from app.adapters.builtin import REGISTRY
    from app.adapters.companies_house import CompaniesHouseAdapter
    from app.services.jobs import job_manager

    if enabled:
        REGISTRY[CompaniesHouseAdapter.tool_id] = CompaniesHouseAdapter
        job_manager.tool_limits.setdefault(
            CompaniesHouseAdapter.tool_id,
            asyncio.Semaphore(CompaniesHouseAdapter.concurrency_limit),
        )
    else:
        REGISTRY.pop(CompaniesHouseAdapter.tool_id, None)
        job_manager.tool_limits.pop(CompaniesHouseAdapter.tool_id, None)
