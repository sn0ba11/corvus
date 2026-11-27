from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.db import ApplicationSetting

SEARXNG_ENGINES_SETTING_KEY = "searxng_active_engines"


@dataclass(frozen=True, slots=True)
class SearXNGEngine:
    engine_id: str
    name: str
    description: str
    keyless: bool = True


ENGINE_CATALOG: tuple[SearXNGEngine, ...] = (
    SearXNGEngine("bing", "Bing", "Broad general-web coverage through Corvus' deterministic Irish-market Bing engine override, tuned to improve profile and dork-query consistency."),
    SearXNGEngine("brave", "Brave", "Independent general-web index with useful profile and document coverage."),
    SearXNGEngine("duckduckgo", "DuckDuckGo", "General web search with a different ranking/index mix from Bing and Google."),
    SearXNGEngine("google", "Google", "General Google web results. Automated access may occasionally be challenged."),
    SearXNGEngine("mojeek", "Mojeek", "Independent crawler and index with useful corroborating coverage."),
    SearXNGEngine("mwmbl", "Mwmbl", "Community-built index. Smaller coverage but useful as an independent source."),
    SearXNGEngine("presearch", "Presearch", "General web search source available through SearXNG."),
    SearXNGEngine("qwant", "Qwant", "European general web search source."),
    SearXNGEngine("searchmysite", "SearchMySite", "Independent index focused on personal and independent websites."),
    SearXNGEngine("startpage", "Startpage", "General web search source that may provide different ranking from direct Google."),
    SearXNGEngine("swisscows", "Swisscows", "General web search source with independent ranking behaviour."),
    SearXNGEngine("wiby", "Wiby", "Small-web index useful for older and less commercial pages."),
    SearXNGEngine("yahoo", "Yahoo", "General web search source with additional ranking diversity."),
    SearXNGEngine("yandex", "Yandex", "General web search source useful for broader international coverage."),
    SearXNGEngine("yep", "Yep", "Independent web index from Ahrefs."),
)

CATALOG_ENGINE_IDS: tuple[str, ...] = tuple(engine.engine_id for engine in ENGINE_CATALOG)
CONFIGURED_ENGINE_IDS: tuple[str, ...] = (
    "bing",
    "brave",
    "duckduckgo",
    "google",
    "mojeek",
    "qwant",
    "startpage",
)
DEFAULT_ENGINE_IDS: tuple[str, ...] = ("google", "mojeek", "qwant")
KNOWN_ENGINE_IDS = frozenset(CATALOG_ENGINE_IDS)
CONFIGURED_ENGINE_ID_SET = frozenset(CONFIGURED_ENGINE_IDS)


def _normalise_engine_ids(value: object, *, default: tuple[str, ...] = DEFAULT_ENGINE_IDS) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return default
    selected = {str(item).strip().casefold() for item in value}
    return tuple(engine_id for engine_id in CONFIGURED_ENGINE_IDS if engine_id in selected)


def get_active_searxng_engines(db: Session) -> tuple[str, ...]:
    setting = db.get(ApplicationSetting, SEARXNG_ENGINES_SETTING_KEY)
    if setting is None:
        return DEFAULT_ENGINE_IDS
    value = setting.value.get("engines") if isinstance(setting.value, dict) else None
    selected = _normalise_engine_ids(value, default=DEFAULT_ENGINE_IDS)
    return selected or DEFAULT_ENGINE_IDS


def save_active_searxng_engines(db: Session, values: list[Any]) -> tuple[str, ...]:
    selected = _normalise_engine_ids(values, default=())
    if not selected:
        raise ValueError("Select at least one SearXNG engine.")
    setting = db.get(ApplicationSetting, SEARXNG_ENGINES_SETTING_KEY)
    payload = {"engines": list(selected)}
    if setting is None:
        db.add(ApplicationSetting(key=SEARXNG_ENGINES_SETTING_KEY, value=payload))
    else:
        setting.value = payload
    return selected


def reset_active_searxng_engines(db: Session) -> tuple[str, ...]:
    setting = db.get(ApplicationSetting, SEARXNG_ENGINES_SETTING_KEY)
    if setting is not None:
        db.delete(setting)
    return DEFAULT_ENGINE_IDS
