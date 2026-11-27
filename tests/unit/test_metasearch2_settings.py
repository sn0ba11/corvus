from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.db import ApplicationSetting, Base
from app.services.metasearch2_settings import (
    DEFAULT_ENGINE_IDS,
    METASEARCH2_ENGINES_SETTING_KEY,
    get_active_metasearch2_engines,
    reset_active_metasearch2_engines,
    save_active_metasearch2_engines,
)


def test_metasearch2_engine_settings_round_trip_and_preserve_catalog_order() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert get_active_metasearch2_engines(db) == DEFAULT_ENGINE_IDS
        selected = save_active_metasearch2_engines(db, ["yep", "brave", "unknown"])
        db.commit()
        assert selected == ("brave", "yep")
        assert get_active_metasearch2_engines(db) == ("brave", "yep")
        setting = db.get(ApplicationSetting, METASEARCH2_ENGINES_SETTING_KEY)
        assert setting is not None
        assert setting.value == {"engines": ["brave", "yep"]}
        assert reset_active_metasearch2_engines(db) == DEFAULT_ENGINE_IDS
        db.commit()
        assert db.get(ApplicationSetting, METASEARCH2_ENGINES_SETTING_KEY) is None


def test_metasearch2_engine_settings_require_one_known_engine() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        try:
            save_active_metasearch2_engines(db, ["unknown"])
        except ValueError as exc:
            assert str(exc) == "Select at least one metasearch2 engine."
        else:
            raise AssertionError("expected empty engine selection to fail")
