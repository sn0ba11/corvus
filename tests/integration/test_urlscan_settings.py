import json
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.adapters import REGISTRY
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.db import ApplicationSetting, AuditEvent, Base
from app.services.urlscan_settings import (
    URLSCAN_API_KEY_SETTING_KEY,
    set_urlscan_adapter_enabled,
)


def test_urlscan_api_key_settings_store_without_echoing_or_auditing_secret() -> None:
    Base.metadata.create_all(engine)
    test_key = "U1" * 16
    with SessionLocal.begin() as db:
        existing = db.get(ApplicationSetting, URLSCAN_API_KEY_SETTING_KEY)
        if existing is not None:
            db.delete(existing)
    set_urlscan_adapter_enabled(False)
    try:
        assert "urlscan_historical" not in REGISTRY
        with TestClient(app) as client:
            page = client.get("/settings")
            assert "URLScan API" in page.text
            token_match = re.search(r'name="csrf" value="([^"]+)"', page.text)
            assert token_match is not None
            saved = client.post(
                "/settings/urlscan-api-key",
                data={
                    "csrf": token_match.group(1),
                    "action": "save",
                    "urlscan_api_key": test_key,
                },
                follow_redirects=False,
            )
            assert saved.status_code == 303
            assert saved.headers["location"] == "/settings?urlscan_saved=1"
            assert "urlscan_historical" in REGISTRY

            refreshed = client.get("/settings?urlscan_saved=1")
            assert test_key not in refreshed.text
            assert "URLScan API key saved." in refreshed.text

        with SessionLocal() as db:
            setting = db.get(ApplicationSetting, URLSCAN_API_KEY_SETTING_KEY)
            assert setting is not None
            assert setting.value == {"key": test_key}
            event = db.scalar(
                select(AuditEvent)
                .where(AuditEvent.event_type == "urlscan_api_key_updated")
                .order_by(AuditEvent.occurred_at.desc())
            )
            assert event is not None
            assert test_key not in json.dumps(event.details)
    finally:
        with SessionLocal.begin() as db:
            existing = db.get(ApplicationSetting, URLSCAN_API_KEY_SETTING_KEY)
            if existing is not None:
                db.delete(existing)
        set_urlscan_adapter_enabled(False)
