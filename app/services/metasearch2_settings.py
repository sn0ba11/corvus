from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.db import ApplicationSetting

METASEARCH2_ENGINES_SETTING_KEY = "metasearch2_active_engines"


@dataclass(frozen=True, slots=True)
class Metasearch2Engine:
    engine_id: str
    name: str
    description: str


ENGINE_CATALOG: tuple[Metasearch2Engine, ...] = (
    Metasearch2Engine("google", "Google", "Google web search. May occasionally block automated requests."),
    Metasearch2Engine("bing", "Bing", "Bing web search; strict Corvus relevance filtering still applies."),
    Metasearch2Engine("brave", "Brave", "Independent general-web index with strong profile coverage."),
    Metasearch2Engine("marginalia", "Marginalia", "Independent small-web index; useful for forums and older personal pages."),
    Metasearch2Engine("rightdao", "Right Dao", "Independent search index available through metasearch2."),
    Metasearch2Engine("stract", "Stract", "Open-source web search engine with an independent index."),
    Metasearch2Engine("yep", "Yep", "Independent general web index from Ahrefs."),
)

CATALOG_ENGINE_IDS: tuple[str, ...] = tuple(engine.engine_id for engine in ENGINE_CATALOG)
DEFAULT_ENGINE_IDS: tuple[str, ...] = ("google", "bing", "brave", "marginalia")
KNOWN_ENGINE_IDS = frozenset(CATALOG_ENGINE_IDS)


def _normalise_engine_ids(value: object, *, default: tuple[str, ...] = DEFAULT_ENGINE_IDS) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return default
    selected = {str(item).strip().casefold() for item in value}
    return tuple(engine_id for engine_id in CATALOG_ENGINE_IDS if engine_id in selected)


def get_active_metasearch2_engines(db: Session) -> tuple[str, ...]:
    setting = db.get(ApplicationSetting, METASEARCH2_ENGINES_SETTING_KEY)
    if setting is None:
        return DEFAULT_ENGINE_IDS
    value = setting.value.get("engines") if isinstance(setting.value, dict) else None
    return _normalise_engine_ids(value)


def save_active_metasearch2_engines(db: Session, values: list[Any]) -> tuple[str, ...]:
    selected = _normalise_engine_ids(values, default=())
    if not selected:
        raise ValueError("Select at least one metasearch2 engine.")
    setting = db.get(ApplicationSetting, METASEARCH2_ENGINES_SETTING_KEY)
    payload = {"engines": list(selected)}
    if setting is None:
        db.add(ApplicationSetting(key=METASEARCH2_ENGINES_SETTING_KEY, value=payload))
    else:
        setting.value = payload
    return selected


def reset_active_metasearch2_engines(db: Session) -> tuple[str, ...]:
    setting = db.get(ApplicationSetting, METASEARCH2_ENGINES_SETTING_KEY)
    if setting is not None:
        db.delete(setting)
    return DEFAULT_ENGINE_IDS
