from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.db import ApplicationSetting

RUNTIME_OPTIONS_SETTING_KEY = "runtime_options"
DEFAULT_RUNTIME_OPTIONS: dict[str, bool] = {
    "browser_share_sessions": True,
    "automatic_wayback": True,
    "automatic_dork_run": True,
    "phone_presence_via_tor": False,
}


def get_runtime_options(db: Session) -> dict[str, bool]:
    setting = db.get(ApplicationSetting, RUNTIME_OPTIONS_SETTING_KEY)
    values = setting.value if setting is not None and isinstance(setting.value, dict) else {}
    return {
        key: bool(values.get(key, default))
        for key, default in DEFAULT_RUNTIME_OPTIONS.items()
    }


def save_runtime_options(db: Session, values: dict[str, Any]) -> dict[str, bool]:
    resolved = {key: bool(values.get(key, False)) for key in DEFAULT_RUNTIME_OPTIONS}
    setting = db.get(ApplicationSetting, RUNTIME_OPTIONS_SETTING_KEY)
    if setting is None:
        db.add(ApplicationSetting(key=RUNTIME_OPTIONS_SETTING_KEY, value=resolved))
    else:
        setting.value = resolved
    return resolved


def reset_runtime_options(db: Session) -> dict[str, bool]:
    setting = db.get(ApplicationSetting, RUNTIME_OPTIONS_SETTING_KEY)
    if setting is not None:
        db.delete(setting)
    return dict(DEFAULT_RUNTIME_OPTIONS)
