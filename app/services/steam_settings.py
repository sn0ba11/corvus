from __future__ import annotations

import asyncio
import re
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.db import ApplicationSetting

STEAM_API_KEY_SETTING_KEY = "steam_web_api_key"
STEAM_API_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def validate_steam_api_key(value: Any) -> str:
    key = str(value or "").strip()
    if not STEAM_API_KEY_RE.fullmatch(key):
        raise ValueError(
            "Steam Web API key must be 16–128 characters using letters, numbers, '_' or '-'"
        )
    return key


def get_steam_api_key(db: Session) -> str | None:
    setting = db.get(ApplicationSetting, STEAM_API_KEY_SETTING_KEY)
    if setting is not None and isinstance(setting.value, dict):
        stored = setting.value.get("key")
        if isinstance(stored, str):
            try:
                return validate_steam_api_key(stored)
            except ValueError:
                pass
    configured = get_settings().steam_web_api_key.strip()
    if configured:
        try:
            return validate_steam_api_key(configured)
        except ValueError:
            return None
    return None


def steam_api_key_configured(db: Session) -> bool:
    return get_steam_api_key(db) is not None


def set_steam_adapter_enabled(enabled: bool) -> None:
    from app.adapters.builtin import REGISTRY
    from app.adapters.steam_public import SteamPublicAdapter
    from app.services.jobs import job_manager

    if enabled:
        REGISTRY[SteamPublicAdapter.tool_id] = SteamPublicAdapter
        job_manager.tool_limits.setdefault(
            SteamPublicAdapter.tool_id,
            asyncio.Semaphore(SteamPublicAdapter.concurrency_limit),
        )
    else:
        REGISTRY.pop(SteamPublicAdapter.tool_id, None)
        job_manager.tool_limits.pop(SteamPublicAdapter.tool_id, None)


def save_steam_api_key(db: Session, value: Any) -> None:
    key = validate_steam_api_key(value)
    setting = db.get(ApplicationSetting, STEAM_API_KEY_SETTING_KEY)
    payload = {"key": key}
    if setting is None:
        db.add(ApplicationSetting(key=STEAM_API_KEY_SETTING_KEY, value=payload))
    else:
        setting.value = payload


def clear_steam_api_key(db: Session) -> None:
    setting = db.get(ApplicationSetting, STEAM_API_KEY_SETTING_KEY)
    if setting is not None:
        db.delete(setting)
