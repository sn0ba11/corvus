import re

from fastapi.testclient import TestClient

from app.db.session import SessionLocal, engine
from app.main import app
from app.models.db import ApplicationSetting, Base
from app.services.metasearch2_settings import METASEARCH2_ENGINES_SETTING_KEY
from app.services.phone_presence_settings import PHONE_PRESENCE_PLATFORMS_SETTING_KEY
from app.services.runtime_settings import RUNTIME_OPTIONS_SETTING_KEY
from app.services.searxng_settings import SEARXNG_ENGINES_SETTING_KEY

SETTING_KEYS = (
    SEARXNG_ENGINES_SETTING_KEY,
    METASEARCH2_ENGINES_SETTING_KEY,
    RUNTIME_OPTIONS_SETTING_KEY,
    PHONE_PRESENCE_PLATFORMS_SETTING_KEY,
)


def _csrf(page: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)


def test_module_settings_page_and_routes_persist() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        for key in SETTING_KEYS:
            setting = db.get(ApplicationSetting, key)
            if setting is not None:
                db.delete(setting)

    try:
        with TestClient(app) as client:
            page = client.get("/settings")
            assert page.status_code == 200
            assert "DuckDuckGo" in page.text
            assert "Startpage" in page.text
            assert "Metasearch2 engines" in page.text
            assert "Share authenticated browser sessions" in page.text
            assert "Automatic Wayback lookup" in page.text
            assert "Phone-presence platforms" in page.text
            assert "Route phone-presence checks through Tor" in page.text

            token = _csrf(page.text)
            searx = client.post(
                "/settings/searxng-engines",
                data={"csrf": token, "action": "save", "engine": ["google", "duckduckgo"]},
                follow_redirects=False,
            )
            assert searx.status_code == 303
            assert searx.headers["location"] == "/settings?searxng_saved=1"

            page = client.get("/settings")
            token = _csrf(page.text)
            meta = client.post(
                "/settings/metasearch2-engines",
                data={"csrf": token, "action": "save", "engine": ["brave", "marginalia"]},
                follow_redirects=False,
            )
            assert meta.status_code == 303
            assert meta.headers["location"] == "/settings?metasearch2_saved=1"

            page = client.get("/settings")
            token = _csrf(page.text)
            runtime = client.post(
                "/settings/runtime-options",
                data={"csrf": token, "action": "save", "automatic_wayback": "on"},
                follow_redirects=False,
            )
            assert runtime.status_code == 303
            assert runtime.headers["location"] == "/settings?runtime_saved=1"

        with SessionLocal() as db:
            assert db.get(ApplicationSetting, SEARXNG_ENGINES_SETTING_KEY).value == {
                "engines": ["duckduckgo", "google"]
            }
            assert db.get(ApplicationSetting, METASEARCH2_ENGINES_SETTING_KEY).value == {
                "engines": ["brave", "marginalia"]
            }
            assert db.get(ApplicationSetting, RUNTIME_OPTIONS_SETTING_KEY).value == {
                "browser_share_sessions": False,
                "automatic_wayback": True,
                "automatic_dork_run": False,
                "phone_presence_via_tor": False,
            }
    finally:
        with SessionLocal.begin() as db:
            for key in SETTING_KEYS:
                setting = db.get(ApplicationSetting, key)
                if setting is not None:
                    db.delete(setting)
