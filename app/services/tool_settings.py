from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.adapters import REGISTRY
from app.models.db import ApplicationSetting

TOOL_TIMEOUTS_SETTING_KEY = "tool_timeouts"
MIN_TOOL_TIMEOUT_SECONDS = 10
MAX_TOOL_TIMEOUT_SECONDS = 3600
LEGACY_DEFAULT_TIMEOUTS = {
    "sherlock": 180,
    "instaloader": 90,
    "nexfil": 90,
}


def built_in_tool_timeouts() -> dict[str, int]:
    return {tool_id: adapter.default_timeout for tool_id, adapter in REGISTRY.items()}


def validate_tool_timeout(tool_id: str, value: Any) -> int:
    if tool_id not in REGISTRY:
        raise ValueError(f"unknown collector: {tool_id}")
    try:
        timeout = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{REGISTRY[tool_id].display_name} timeout must be a whole number"
        ) from exc
    if not MIN_TOOL_TIMEOUT_SECONDS <= timeout <= MAX_TOOL_TIMEOUT_SECONDS:
        raise ValueError(
            f"{REGISTRY[tool_id].display_name} timeout must be between "
            f"{MIN_TOOL_TIMEOUT_SECONDS} and {MAX_TOOL_TIMEOUT_SECONDS} seconds"
        )
    return timeout


def get_tool_timeouts(db: Session) -> dict[str, int]:
    defaults = built_in_tool_timeouts()
    setting = db.get(ApplicationSetting, TOOL_TIMEOUTS_SETTING_KEY)
    if setting is None or not isinstance(setting.value, dict):
        return defaults

    resolved = defaults.copy()
    for tool_id, raw_value in setting.value.items():
        if tool_id not in REGISTRY:
            continue
        try:
            timeout = validate_tool_timeout(tool_id, raw_value)
        except ValueError:
            continue
        legacy_default = LEGACY_DEFAULT_TIMEOUTS.get(tool_id)
        if legacy_default is not None and timeout == legacy_default:
            timeout = defaults[tool_id]
        resolved[tool_id] = timeout
    return resolved


def save_tool_timeouts(db: Session, values: dict[str, Any]) -> dict[str, int]:
    resolved = {
        tool_id: validate_tool_timeout(tool_id, values[tool_id])
        for tool_id in REGISTRY
        if tool_id in values
    }
    missing = [tool_id for tool_id in REGISTRY if tool_id not in resolved]
    if missing:
        labels = ", ".join(REGISTRY[tool_id].display_name for tool_id in missing)
        raise ValueError(f"timeout values are missing for: {labels}")

    setting = db.get(ApplicationSetting, TOOL_TIMEOUTS_SETTING_KEY)
    if setting is None:
        setting = ApplicationSetting(key=TOOL_TIMEOUTS_SETTING_KEY, value=resolved)
        db.add(setting)
    else:
        setting.value = resolved
        setting.updated_at = datetime.now(UTC)
    return resolved


def reset_tool_timeouts(db: Session) -> dict[str, int]:
    setting = db.get(ApplicationSetting, TOOL_TIMEOUTS_SETTING_KEY)
    if setting is not None:
        db.delete(setting)
    return built_in_tool_timeouts()
