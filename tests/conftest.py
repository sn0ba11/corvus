import atexit
import os
import secrets
import shutil
import tempfile
from pathlib import Path

_TEST_DATA = Path(tempfile.mkdtemp(prefix="osint-workbench-tests-"))
os.environ["OSINT_DATA_DIR"] = str(_TEST_DATA)
os.environ["OSINT_DATABASE_URL"] = f"sqlite:///{_TEST_DATA / 'osint.db'}"
os.environ["OSINT_SECRET_KEY"] = secrets.token_urlsafe(48)
os.environ["OSINT_BIND_HOST"] = "127.0.0.1"
os.environ["OSINT_LOCAL_AUTH_ENABLED"] = "false"
os.environ["OSINT_SECURE_COOKIES"] = "false"
os.environ["OSINT_BROWSER_ENABLED"] = "true"
os.environ["OSINT_BROWSER_AGENT_TOKEN"] = secrets.token_urlsafe(48)


@atexit.register
def _remove_test_data() -> None:
    shutil.rmtree(_TEST_DATA, ignore_errors=True)
