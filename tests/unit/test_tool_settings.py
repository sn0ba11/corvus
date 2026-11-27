from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.db import ApplicationSetting, Base
from app.services.tool_settings import TOOL_TIMEOUTS_SETTING_KEY, get_tool_timeouts


def test_legacy_collector_timeout_defaults_are_lifted_without_overwriting_custom_values() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        db.add(
            ApplicationSetting(
                key=TOOL_TIMEOUTS_SETTING_KEY,
                value={
                    "sherlock": 180,
                    "instaloader": 90,
                    "nexfil": 240,
                },
            )
        )
        db.commit()

        resolved = get_tool_timeouts(db)

    assert resolved["sherlock"] == 360
    assert resolved["instaloader"] == 90
    assert resolved["nexfil"] == 240
