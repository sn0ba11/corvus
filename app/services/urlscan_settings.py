from __future__ import annotations

import asyncio
import re
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.db import ApplicationSetting

URLSCAN_API_KEY_SETTING_KEY = "urlscan_api_key"
URLSCAN_API_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")


def validate_urlscan_api_key(value: Any) -> str:
    key = str(value or "").strip()
    if not URLSCAN_API_KEY_RE.fullmatch(key):
        raise ValueError("URLScan API key must be 16–256 characters using letters, numbers, '_' or '-'")
    return key


def get_urlscan_api_key(db: Session) -> str | None:
    setting = db.get(ApplicationSetting, URLSCAN_API_KEY_SETTING_KEY)
    if setting is not None and isinstance(setting.value, dict):
        stored = setting.value.get("key")
        if isinstance(stored, str):
            try:
                return validate_urlscan_api_key(stored)
            except ValueError:
                pass
    configured = get_settings().urlscan_api_key.strip()
    if configured:
        try:
            return validate_urlscan_api_key(configured)
        except ValueError:
            return None
    return None


def urlscan_api_key_configured(db: Session) -> bool:
    return get_urlscan_api_key(db) is not None


def set_urlscan_adapter_enabled(enabled: bool) -> None:
    from app.adapters.builtin import REGISTRY
    from app.adapters.urlscan_historical import URLScanHistoricalAdapter
    from app.services.jobs import job_manager

    if enabled:
        REGISTRY[URLScanHistoricalAdapter.tool_id] = URLScanHistoricalAdapter
        job_manager.tool_limits.setdefault(
            URLScanHistoricalAdapter.tool_id,
            asyncio.Semaphore(URLScanHistoricalAdapter.concurrency_limit),
        )
    else:
        REGISTRY.pop(URLScanHistoricalAdapter.tool_id, None)
        job_manager.tool_limits.pop(URLScanHistoricalAdapter.tool_id, None)


def save_urlscan_api_key(db: Session, value: Any) -> None:
    key = validate_urlscan_api_key(value)
    setting = db.get(ApplicationSetting, URLSCAN_API_KEY_SETTING_KEY)
    payload = {"key": key}
    if setting is None:
        db.add(ApplicationSetting(key=URLSCAN_API_KEY_SETTING_KEY, value=payload))
    else:
        setting.value = payload


def clear_urlscan_api_key(db: Session) -> None:
    setting = db.get(ApplicationSetting, URLSCAN_API_KEY_SETTING_KEY)
    if setting is not None:
        db.delete(setting)
