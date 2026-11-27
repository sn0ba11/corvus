from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.db import ApplicationSetting, Base
from app.services.runtime_settings import (
    DEFAULT_RUNTIME_OPTIONS,
    RUNTIME_OPTIONS_SETTING_KEY,
    get_runtime_options,
    reset_runtime_options,
    save_runtime_options,
)


def test_runtime_options_round_trip() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert get_runtime_options(db) == DEFAULT_RUNTIME_OPTIONS
        saved = save_runtime_options(
            db,
            {"browser_share_sessions": False, "automatic_wayback": True},
        )
        db.commit()
        assert saved == {
            "browser_share_sessions": False,
            "automatic_wayback": True,
            "automatic_dork_run": False,
            "phone_presence_via_tor": False,
        }
        assert get_runtime_options(db) == saved
        setting = db.get(ApplicationSetting, RUNTIME_OPTIONS_SETTING_KEY)
        assert setting is not None
        assert setting.value == saved
        assert reset_runtime_options(db) == DEFAULT_RUNTIME_OPTIONS
        db.commit()
        assert db.get(ApplicationSetting, RUNTIME_OPTIONS_SETTING_KEY) is None


def test_phone_presence_via_tor_defaults_off_and_can_be_enabled() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert get_runtime_options(db)["phone_presence_via_tor"] is False
        saved = save_runtime_options(db, {"phone_presence_via_tor": True})
        db.commit()
        assert saved["phone_presence_via_tor"] is True
        assert get_runtime_options(db)["phone_presence_via_tor"] is True
