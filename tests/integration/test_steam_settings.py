import json
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.adapters import REGISTRY
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.db import ApplicationSetting, AuditEvent, Base
from app.services.steam_settings import (
    STEAM_API_KEY_SETTING_KEY,
    set_steam_adapter_enabled,
)


def test_steam_api_key_settings_store_without_echoing_or_auditing_secret() -> None:
    Base.metadata.create_all(engine)
    test_key = "A1" * 16
    with SessionLocal.begin() as db:
        existing = db.get(ApplicationSetting, STEAM_API_KEY_SETTING_KEY)
        if existing is not None:
            db.delete(existing)
    set_steam_adapter_enabled(False)
    try:
        assert "steam_public" not in REGISTRY
        with TestClient(app) as client:
            page = client.get("/settings")
            assert "Steam Web API" in page.text
            token_match = re.search(r'name="csrf" value="([^"]+)"', page.text)
            assert token_match is not None
            saved = client.post(
                "/settings/steam-api-key",
                data={
                    "csrf": token_match.group(1),
                    "action": "save",
                    "steam_api_key": test_key,
                },
                follow_redirects=False,
            )
            assert saved.status_code == 303
            assert saved.headers["location"] == "/settings?steam_saved=1"
            assert "steam_public" in REGISTRY

            refreshed = client.get("/settings?steam_saved=1")
            assert test_key not in refreshed.text
            assert "Steam Web API key saved." in refreshed.text

        with SessionLocal() as db:
            setting = db.get(ApplicationSetting, STEAM_API_KEY_SETTING_KEY)
            assert setting is not None
            assert setting.value == {"key": test_key}
            event = db.scalar(
                select(AuditEvent)
                .where(AuditEvent.event_type == "steam_api_key_updated")
                .order_by(AuditEvent.occurred_at.desc())
            )
            assert event is not None
            assert test_key not in json.dumps(event.details)
    finally:
        with SessionLocal.begin() as db:
            existing = db.get(ApplicationSetting, STEAM_API_KEY_SETTING_KEY)
            if existing is not None:
                db.delete(existing)
        set_steam_adapter_enabled(False)
