from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.db import ApplicationSetting

PHONE_PRESENCE_PLATFORMS_SETTING_KEY = "phone_presence_active_platforms"


@dataclass(frozen=True, slots=True)
class PhonePresencePlatform:
    platform_id: str
    name: str
    description: str
    risk_note: str


PLATFORM_CATALOG: tuple[PhonePresencePlatform, ...] = (
    PhonePresencePlatform(
        "snapchat",
        "Snapchat",
        "Checks Snapchat's public, unauthenticated signup-validation endpoint for phone registration.",
        "A request to the public web-signup flow. It discloses the number to Snapchat.",
    ),
    PhonePresencePlatform(
        "amazon",
        "Amazon",
        "Scrapes Amazon's sign-in form and submits the phone number as the account identifier.",
        "Fragile: depends on Amazon's current sign-in form fields and one internal HTML element ID that "
        "can change without notice. Amazon's sign-in page is heavily bot-defended, so requests may be "
        "challenged instead of answered.",
    ),
)

CATALOG_PLATFORM_IDS: tuple[str, ...] = tuple(platform.platform_id for platform in PLATFORM_CATALOG)
# Amazon requires an explicit opt-in through Settings.
DEFAULT_PLATFORM_IDS: tuple[str, ...] = ("snapchat",)
KNOWN_PLATFORM_IDS = frozenset(CATALOG_PLATFORM_IDS)


def _normalise_platform_ids(
    value: object, *, default: tuple[str, ...] = DEFAULT_PLATFORM_IDS
) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return default
    selected = {str(item).strip().casefold() for item in value}
    return tuple(platform_id for platform_id in CATALOG_PLATFORM_IDS if platform_id in selected)


def get_active_phone_presence_platforms(db: Session) -> tuple[str, ...]:
    setting = db.get(ApplicationSetting, PHONE_PRESENCE_PLATFORMS_SETTING_KEY)
    if setting is None:
        return DEFAULT_PLATFORM_IDS
    value = setting.value.get("platforms") if isinstance(setting.value, dict) else None
    return _normalise_platform_ids(value)


def save_active_phone_presence_platforms(db: Session, values: list[Any]) -> tuple[str, ...]:
    # Unlike SearXNG engines, an empty selection is valid here: an examiner may want the
    # phone-presence collector present but every live platform check disabled.
    selected = _normalise_platform_ids(values, default=())
    setting = db.get(ApplicationSetting, PHONE_PRESENCE_PLATFORMS_SETTING_KEY)
    payload = {"platforms": list(selected)}
    if setting is None:
        db.add(ApplicationSetting(key=PHONE_PRESENCE_PLATFORMS_SETTING_KEY, value=payload))
    else:
        setting.value = payload
    return selected


def reset_active_phone_presence_platforms(db: Session) -> tuple[str, ...]:
    setting = db.get(ApplicationSetting, PHONE_PRESENCE_PLATFORMS_SETTING_KEY)
    if setting is not None:
        db.delete(setting)
    return DEFAULT_PLATFORM_IDS
