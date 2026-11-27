import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.adapters.base import (
    Adapter,
    ExecutionResult,
    failure_message,
    runner_error_message,
    structured_collector_status,
)
from app.models.schemas import FindingRecord, QueryType


class DummyAdapter(Adapter):
    tool_id = "dummy"
    display_name = "Dummy"
    executable = sys.executable
    capabilities = frozenset({"test"})
    supported_query_types = frozenset({QueryType.USERNAME})

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        return [self.executable, "-c", query]

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        return []

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        return []


class LocalInstaloaderAdapter(DummyAdapter):
    tool_id = "instaloader"
    local_session_format = "json_context"


class LocalMaigretAdapter(DummyAdapter):
    tool_id = "maigret"
    local_session_format = "netscape_cookie_jar"


@pytest.mark.asyncio
async def test_local_instaloader_receives_and_cleans_browser_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OSINT_RUNNER_INSTALOADER_URL", raising=False)
    temporary_root = Path(tempfile.gettempdir())
    before = set(temporary_root.glob("osint-instaloader-session-*.json"))
    adapter = LocalInstaloaderAdapter()
    adapter.set_session_context(
        [
            {
                "name": "sessionid",
                "value": "synthetic-session",
                "domain": ".instagram.com",
                "path": "/",
                "expires": 1_900_000_000,
                "secure": True,
                "http_only": True,
            }
        ],
        user_agent="Mozilla/5.0 Synthetic Chrome/149.0",
        accept_language="en-IE,en;q=0.9",
    )
    script = (
        "import json,os,pathlib;"
        "p=pathlib.Path(os.environ['OSINT_SESSION_COOKIE_FILE']);"
        "d=json.loads(p.read_text());"
        "print(json.dumps({'ua':d['user_agent'],'lang':d['accept_language'],'cookies':len(d['cookies'])}))"
    )

    result = await adapter.execute(
        adapter.build_command(script, QueryType.USERNAME, tmp_path, 10),
        tmp_path,
        10,
        "test",
    )

    payload = json.loads(result.stdout)
    assert payload == {
        "ua": "Mozilla/5.0 Synthetic Chrome/149.0",
        "lang": "en-IE,en;q=0.9",
        "cookies": 1,
    }
    assert result.session_loaded is True
    assert set(temporary_root.glob("osint-instaloader-session-*.json")) == before


@pytest.mark.asyncio
async def test_local_maigret_receives_scoped_cookie_jar_and_cleans_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OSINT_RUNNER_MAIGRET_URL", raising=False)
    temporary_root = Path(tempfile.gettempdir())
    before = set(temporary_root.glob("osint-maigret-session-*.txt"))
    adapter = LocalMaigretAdapter()
    adapter.set_session_context(
        [
            {
                "name": "sessionid",
                "value": "synthetic-session",
                "domain": ".instagram.com",
                "path": "/",
                "expires": 1_900_000_000,
                "secure": True,
                "http_only": True,
            }
        ],
        user_agent="Mozilla/5.0 Synthetic Chrome/149.0",
        accept_language="en-IE,en;q=0.9",
    )
    script = (
        "import json,pathlib,sys;"
        "i=sys.argv.index('--cookies-jar-file');"
        "p=pathlib.Path(sys.argv[i+1]);"
        "text=p.read_text();"
        "print(json.dumps({'status':'found','has_cookie':'synthetic-session' in text,'mode':oct(p.stat().st_mode & 0o777)}))"
    )

    result = await adapter.execute(
        adapter.build_command(script, QueryType.USERNAME, tmp_path, 10),
        tmp_path,
        10,
        "test",
    )

    payload = json.loads(result.stdout)
    assert payload == {"status": "found", "has_cookie": True, "mode": "0o600"}
    assert result.session_loaded is True
    assert set(temporary_root.glob("osint-maigret-session-*.txt")) == before


@pytest.mark.asyncio
async def test_remote_runner_has_overall_deadline_and_cancels_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deleted: list[str] = []

    class ImmediateTimeout:
        async def __aenter__(self) -> None:
            raise TimeoutError

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def delete(self, url: str) -> None:
            deleted.append(url)

    monkeypatch.setattr("app.adapters.base.asyncio.timeout", lambda _seconds: ImmediateTimeout())
    monkeypatch.setattr("app.adapters.base.httpx.AsyncClient", FakeClient)
    adapter = DummyAdapter()

    result = await adapter._execute_remote(
        "http://runner.test",
        [adapter.executable, "-c", "pass"],
        tmp_path,
        5,
        "test",
        None,
    )

    assert result.timed_out is True
    assert result.error_code == "timeout"
    assert deleted and deleted[0].startswith("http://runner.test/jobs/")


@pytest.mark.asyncio
async def test_remote_runner_recovers_one_lost_job_before_first_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts = 0
    polls = 0

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def post(self, url: str, json: object) -> httpx.Response:
            nonlocal posts
            posts += 1
            return httpx.Response(202, json={"job_id": "ok"}, request=httpx.Request("POST", url))

        async def get(self, url: str, params: object) -> httpx.Response:
            nonlocal polls
            polls += 1
            if polls == 1:
                return httpx.Response(404, json={"error": "not found"}, request=httpx.Request("GET", url))
            return httpx.Response(
                200,
                json={
                    "state": "completed",
                    "logs": [],
                    "stdout": "",
                    "stderr": "",
                    "files": {},
                    "returncode": 0,
                    "error_code": None,
                    "error_message": None,
                    "session_loaded": False,
                },
                request=httpx.Request("GET", url),
            )

        async def delete(self, url: str) -> None:
            return None

    monkeypatch.setattr("app.adapters.base.httpx.AsyncClient", FakeClient)
    adapter = DummyAdapter()

    result = await adapter._execute_remote(
        "http://runner.test",
        [adapter.executable, "-c", "pass"],
        tmp_path,
        5,
        "test",
        None,
    )

    assert result.returncode == 0
    assert result.error_code is None
    assert posts == 2
    assert polls == 2


@pytest.mark.asyncio
async def test_timeout_handling(tmp_path: Path) -> None:
    adapter = DummyAdapter()
    command = adapter.build_command("import time; time.sleep(5)", QueryType.USERNAME, tmp_path, 1)
    result = await adapter.execute(command, tmp_path, 1, "test")
    assert result.timed_out
    assert result.error_code == "timeout"


def test_runner_error_message_preserves_sanitised_json_detail() -> None:
    response = httpx.Response(
        400,
        json={"detail": "session for target rejected token=secret-value"},
        request=httpx.Request("POST", "http://runner.test/jobs"),
    )

    message = runner_error_message(response, "target")

    assert message == "runner returned HTTP 400: session for [QUERY] rejected [REDACTED]"


def test_structured_collector_status_reads_latest_json_status() -> None:
    stdout = "debug line\n{\"status\": \"found\"}\n{\"status\": \"rate_limited\"}"
    assert structured_collector_status(stdout) == "rate_limited"
    assert structured_collector_status("plain text only") is None


@pytest.mark.asyncio
async def test_local_execution_prefers_structured_status_over_429_text(tmp_path: Path) -> None:
    adapter = DummyAdapter()
    script = (
        "import json; "
        "print('diagnostic URL /429/not-a-status'); "
        "print(json.dumps({'status':'found'}))"
    )
    result = await adapter.execute(adapter.build_command(script, QueryType.USERNAME, tmp_path, 10), tmp_path, 10, "test")
    assert result.returncode == 0
    assert result.rate_limited is False
    assert result.error_code is None


@pytest.mark.asyncio
async def test_local_execution_uses_structured_rate_limited_status(tmp_path: Path) -> None:
    adapter = DummyAdapter()
    script = "import json; print(json.dumps({'status':'rate_limited'}))"
    result = await adapter.execute(adapter.build_command(script, QueryType.USERNAME, tmp_path, 10), tmp_path, 10, "test")
    assert result.returncode == 0
    assert result.rate_limited is True
    assert result.error_code == "rate_limited"


def test_failure_message_prefers_structured_tool_error() -> None:
    stdout = '{"status": "error", "error": "Instagram lookup failed: ConnectionException"}'
    assert (
        failure_message(stdout, "debug noise", "target", 1)
        == "Instagram lookup failed: ConnectionException"
    )


def test_failure_message_sanitises_last_diagnostic_line() -> None:
    message = failure_message("", "request for target failed token=secret-value", "target", 1)
    assert message == "request for [QUERY] failed [REDACTED]"


@pytest.mark.asyncio
async def test_cancellation(tmp_path: Path) -> None:
    adapter = DummyAdapter()
    command = adapter.build_command("import time; time.sleep(10)", QueryType.USERNAME, tmp_path, 30)
    task = asyncio.create_task(adapter.execute(command, tmp_path, 30, "test"))
    await asyncio.sleep(0.2)
    await adapter.cancel()
    result = await task
    assert result.returncode != 0
