from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.db import ApplicationSetting, Base
from app.services.searxng_settings import (
    DEFAULT_ENGINE_IDS,
    SEARXNG_ENGINES_SETTING_KEY,
    get_active_searxng_engines,
    reset_active_searxng_engines,
    save_active_searxng_engines,
)


def test_default_searxng_engines_use_curated_high_signal_set() -> None:
    assert DEFAULT_ENGINE_IDS == ("google", "mojeek", "qwant")


def test_searxng_engine_settings_round_trip_and_preserve_catalog_order() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert get_active_searxng_engines(db) == DEFAULT_ENGINE_IDS
        selected = save_active_searxng_engines(db, ["mwmbl", "bing", "unknown"])
        db.commit()
        assert selected == ("bing",)
        assert get_active_searxng_engines(db) == ("bing",)
        setting = db.get(ApplicationSetting, SEARXNG_ENGINES_SETTING_KEY)
        assert setting is not None
        assert setting.value == {"engines": ["bing"]}
        assert reset_active_searxng_engines(db) == DEFAULT_ENGINE_IDS
        db.commit()
        assert db.get(ApplicationSetting, SEARXNG_ENGINES_SETTING_KEY) is None


def test_stale_unavailable_engine_setting_falls_back_to_safe_defaults() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            ApplicationSetting(
                key=SEARXNG_ENGINES_SETTING_KEY,
                value={"engines": ["mwmbl"]},
            )
        )
        db.commit()
        assert get_active_searxng_engines(db) == DEFAULT_ENGINE_IDS


def test_searxng_engine_settings_require_one_known_engine() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        try:
            save_active_searxng_engines(db, ["unknown"])
        except ValueError as exc:
            assert str(exc) == "Select at least one SearXNG engine."
        else:
            raise AssertionError("expected empty engine selection to fail")
