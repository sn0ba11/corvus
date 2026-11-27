import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SESSION_KEY_FILENAME = ".session-key"
MIN_SECRET_LENGTH = 32


# Hosts the sock browser may open. A value in the environment adds to this list; it does not
# replace it. An installation whose .env was written by an older version would otherwise keep
# refusing every host added since (found on 2026-11-13: an older .env had no t.me).
BUILT_IN_BROWSER_ALLOWED_HOSTS = (
    "google.com,google.ie,bing.com,mojeek.com,duckduckgo.com,brave.com,qwant.com,startpage.com,tripadvisor.com,tripadvisor.ie,tripadvisor.co.uk,"
    "strava.com,alltrails.com,komoot.com,garmin.com,instagram.com,facebook.com,"
    "facebook.net,fb.com,fbcdn.net,fbsbx.com,messenger.com,meta.com,linkedin.com,"
    "x.com,twitter.com,tiktok.com,pinterest.com,threads.com,threads.net,snapchat.com,t.me,telegram.org,discord.com,discord.gg,whatsapp.com,wa.me,reddit.com,github.com,gitlab.com"
)


def load_or_create_local_secret(data_dir: Path) -> str:
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = data_dir / SESSION_KEY_FILENAME
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = secrets.token_urlsafe(48)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            value = path.read_text(encoding="utf-8").strip()
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(value + "\n")
    if len(value) < MIN_SECRET_LENGTH:
        raise ValueError("persisted Corvus session secret is missing or too short")
    path.chmod(0o600)
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OSINT_", env_file=".env", extra="ignore")
    app_name: str = "Corvus"
    bind_host: str = "127.0.0.1"
    port: int = 8000
    database_url: str = "sqlite:///./data/osint.db"
    data_dir: Path = Path("data")
    secret_key: str = ""
    version: str = "0.2.0-rc13"
    build_commit: str = ""
    max_concurrent_investigations: int = Field(2, ge=1, le=16)
    max_concurrent_collectors: int = Field(3, ge=1, le=12)
    image_cache_max_bytes: int = Field(5 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    profile_enrichment_max_bytes: int = Field(1024 * 1024, ge=1024, le=5 * 1024 * 1024)
    profile_enrichment_interval_seconds: float = Field(1.0, ge=0.1, le=30.0)
    image_correlation_threshold: int = Field(10, ge=0, le=64)
    image_correlation_max_candidates: int = Field(50, ge=2, le=250)
    face_detection_model_path: Path = Path("/app/models/face_detection_yunet_2023mar.onnx")
    face_recognition_model_path: Path = Path("/app/models/face_recognition_sface_2021dec.onnx")
    face_detection_threshold: float = Field(0.8, gt=0.0, le=1.0)
    face_match_threshold: float = Field(0.45, ge=0.0, le=1.0)
    face_match_max_candidates: int = Field(250, ge=2, le=1000)
    face_match_max_per_finding: int = Field(5, ge=1, le=25)
    log_queries: bool = False
    local_auth_enabled: bool = False
    local_auth_username: str = "investigator"
    local_auth_password_hash: str = ""
    secure_cookies: bool = False
    http_proxy: str | None = None
    https_proxy: str | None = None
    socks_proxy: str | None = None
    tor_proxy: str | None = None
    steam_web_api_key: str = ""
    urlscan_api_key: str = ""
    companies_house_api_key: str = ""
    abuseipdb_api_key: str = ""
    openphish_feed_url: str = ""
    searxng_url: str = "http://searxng:8080"
    metasearch2_url: str = "http://metasearch2:28019"
    browser_enabled: bool = False
    browser_agent_url: str = "http://browser-agent:8080"
    browser_agent_token: str = ""
    browser_vnc_password: str = ""
    browser_share_sessions: bool = False
    browser_desktop_url: str = "http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale"
    browser_allowed_hosts: str = BUILT_IN_BROWSER_ALLOWED_HOSTS
    browser_maigret_session_hosts: str = (
        "instagram.com,facebook.com,linkedin.com,x.com,twitter.com,tiktok.com,reddit.com,"
        "github.com,gitlab.com"
    )
    browser_request_timeout_seconds: float = Field(45.0, ge=5.0, le=180.0)
    browser_evidence_dir: Path = Path("/browser-evidence")
    update_changelog_path: Path = Path("/runtime/last-update-changes.txt")
    update_build_range_path: Path = Path("/runtime/last-update-range.json")

    @model_validator(mode="after")
    def safe_non_loopback(self) -> "Settings":
        self.secret_key = self.secret_key.strip()
        if self.secret_key:
            if len(self.secret_key) < MIN_SECRET_LENGTH or self.secret_key.startswith("replace-with-"):
                raise ValueError("OSINT_SECRET_KEY requires a random value of at least 32 characters")
        else:
            self.secret_key = load_or_create_local_secret(self.data_dir)
        if self.bind_host not in {"127.0.0.1", "localhost", "::1"} and not self.local_auth_enabled:
            raise ValueError("local authentication is required for non-loopback binding")
        if self.local_auth_enabled and not self.local_auth_password_hash.strip():
            raise ValueError("local authentication requires a configured password hash")
        if self.browser_enabled and (
            len(self.browser_agent_token) < 32 or self.browser_agent_token.startswith("replace-with-")
        ):
            raise ValueError("browser agent token must contain at least 32 characters when enabled")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
