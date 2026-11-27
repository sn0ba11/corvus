from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.db import ApplicationSetting, Base
from app.services.phone_presence_settings import (
    DEFAULT_PLATFORM_IDS,
    PHONE_PRESENCE_PLATFORMS_SETTING_KEY,
    get_active_phone_presence_platforms,
    reset_active_phone_presence_platforms,
    save_active_phone_presence_platforms,
)


def test_default_phone_presence_platforms_are_snapchat_only() -> None:
    assert DEFAULT_PLATFORM_IDS == ("snapchat",)


def test_phone_presence_platform_settings_round_trip_and_preserve_catalog_order() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert get_active_phone_presence_platforms(db) == DEFAULT_PLATFORM_IDS
        selected = save_active_phone_presence_platforms(db, ["amazon", "snapchat", "unknown"])
        db.commit()
        assert selected == ("snapchat", "amazon")
        assert get_active_phone_presence_platforms(db) == ("snapchat", "amazon")
        setting = db.get(ApplicationSetting, PHONE_PRESENCE_PLATFORMS_SETTING_KEY)
        assert setting is not None
        assert setting.value == {"platforms": ["snapchat", "amazon"]}
        assert reset_active_phone_presence_platforms(db) == DEFAULT_PLATFORM_IDS
        db.commit()
        assert db.get(ApplicationSetting, PHONE_PRESENCE_PLATFORMS_SETTING_KEY) is None


def test_phone_presence_platform_settings_allow_disabling_every_platform() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        selected = save_active_phone_presence_platforms(db, [])
        db.commit()
        assert selected == ()
        assert get_active_phone_presence_platforms(db) == ()
