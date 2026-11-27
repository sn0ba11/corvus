import json
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.adapters import REGISTRY
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.db import ApplicationSetting, AuditEvent, Base
from app.services.external_api_settings import (
    ABUSEIPDB_KEY,
    COMPANIES_HOUSE_KEY,
    OPENPHISH_FEED_URL_KEY,
    set_companies_house_adapter_enabled,
    set_openphish_adapter_enabled,
)


def _csrf(page: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)


def _clear_settings() -> None:
    with SessionLocal.begin() as db:
        for key in (COMPANIES_HOUSE_KEY, ABUSEIPDB_KEY, OPENPHISH_FEED_URL_KEY):
            existing = db.get(ApplicationSetting, key)
            if existing is not None:
                db.delete(existing)
    set_companies_house_adapter_enabled(False)
    set_openphish_adapter_enabled(False)


def test_external_api_settings_store_secrets_without_echo_or_audit_disclosure() -> None:
    Base.metadata.create_all(engine)
    _clear_settings()
    companies_key = "companies_house_key_123456"
    abuse_key = "abuse_ip_db_key_123456789"
    feed_url = "https://feeds.openphish.com/gov/secret-feed-token"
    try:
        with TestClient(app) as client:
            page = client.get("/settings")
            token = _csrf(page.text)

            response = client.post(
                "/settings/companies-house-api-key",
                data={"csrf": token, "action": "save", "companies_house_api_key": companies_key},
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert "companies_house" in REGISTRY

            response = client.post(
                "/settings/abuseipdb-api-key",
                data={"csrf": token, "action": "save", "abuseipdb_api_key": abuse_key},
                follow_redirects=False,
            )
            assert response.status_code == 303

            response = client.post(
                "/settings/openphish-feed",
                data={"csrf": token, "action": "save", "openphish_feed_url": feed_url},
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert "openphish" in REGISTRY

            refreshed = client.get("/settings?openphish_saved=1")
            assert companies_key not in refreshed.text
            assert abuse_key not in refreshed.text
            assert feed_url not in refreshed.text
            assert "OpenPhish feed saved." in refreshed.text

        with SessionLocal() as db:
            assert db.get(ApplicationSetting, COMPANIES_HOUSE_KEY).value == {"key": companies_key}  # type: ignore[union-attr]
            assert db.get(ApplicationSetting, ABUSEIPDB_KEY).value == {"key": abuse_key}  # type: ignore[union-attr]
            assert db.get(ApplicationSetting, OPENPHISH_FEED_URL_KEY).value == {"key": feed_url}  # type: ignore[union-attr]
            for event_type in (
                "companies_house_api_key_updated",
                "abuseipdb_api_key_updated",
                "openphish_feed_updated",
            ):
                event = db.scalar(
                    select(AuditEvent)
                    .where(AuditEvent.event_type == event_type)
                    .order_by(AuditEvent.occurred_at.desc())
                )
                assert event is not None
                rendered = json.dumps(event.details)
                assert companies_key not in rendered
                assert abuse_key not in rendered
                assert feed_url not in rendered
    finally:
        _clear_settings()
