import asyncio
import base64
import contextlib
import json
import os
import re
import shutil
import signal
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from app.core.security import redact
from app.models.schemas import FindingRecord, QueryType

LogCallback = Callable[[str, str], Awaitable[None]]


def runner_error_message(response: httpx.Response, query: str) -> str:
    """Return a bounded, sanitised runner error instead of discarding its response body."""

    detail = ""
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        for key in ("error_message", "error", "detail", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip()
                break
    if not detail:
        detail = response.text.strip()
    detail = redact(detail, query)[:500] if detail else response.reason_phrase
    return f"runner returned HTTP {response.status_code}: {detail}"


def structured_collector_status(stdout: str) -> str | None:
    """Return a collector-emitted terminal status when stdout contains structured JSON."""

    for line in reversed(stdout.splitlines()):
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        status = payload.get("status")
        if isinstance(status, str) and status.strip():
            return status.strip().casefold()
    return None


def failure_message(stdout: str, stderr: str, query: str, returncode: object) -> str:
    """Return one concise, sanitised reason instead of an opaque exit code."""
    for stream in (stdout, stderr):
        for line in reversed(stream.splitlines()):
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                clean = redact(payload["error"].strip(), query)
                if clean:
                    return clean[:500]
    for stream in (stderr, stdout):
        for line in reversed(stream.splitlines()):
            clean = redact(line.strip(), query)
            if clean:
                return clean[:500]
    return f"upstream exited {returncode}"


@dataclass(slots=True)
class HealthResult:
    healthy: bool
    message: str
    version: str | None = None


@dataclass(slots=True)
class ExecutionResult:
    returncode: int | None
    stdout: str
    stderr: str
    output_files: list[Path] = field(default_factory=list)
    timed_out: bool = False
    cancelled: bool = False
    rate_limited: bool = False
    error_code: str | None = None
    error_message: str | None = None
    session_loaded: bool | None = None
    network_ok: bool | None = None


class Adapter(ABC):
    tool_id: str
    display_name: str
    capabilities: frozenset[str]
    supported_query_types: frozenset[QueryType]
    executable: str
    version_args: tuple[str, ...] = ("--version",)
    recovery_endpoint: bool = False
    local_session_format: str | None = None
    local_session_flag: str = "--cookies-jar-file"
    default_selected: bool = True
    default_timeout: int = 180
    concurrency_limit: int = 1

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._remote_job: tuple[str, str] | None = None
        self._session_context: dict[str, Any] | None = None

    def coverage_warning(self, result: ExecutionResult) -> str | None:
        """A note for the analyst when the tool ran but could not check a large share of its sites."""
        if result.network_ok is False:
            return (
                f"{self.display_name} could not resolve hostnames when its run ended, so the "
                "sites it reported nothing for were probably not checked. Accounts on those "
                "sites are not ruled out; check the network or DNS resolver (a resolver rate "
                "limit is a common cause) and run it again."
            )
        return None

    def set_session_context(
        self,
        cookies: list[dict[str, Any]],
        *,
        user_agent: str,
        accept_language: str,
    ) -> None:
        """Attach one ephemeral browser identity to the next execution."""
        self._session_context = {
            "cookies": [dict(cookie) for cookie in cookies],
            "user_agent": user_agent,
            "accept_language": accept_language,
        }

    def set_session_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Compatibility helper for collectors that only consume cookies."""
        self._session_context = {
            "cookies": [dict(cookie) for cookie in cookies],
            "user_agent": None,
            "accept_language": None,
        }

    def _runner_url(self) -> str | None:
        return os.environ.get(f"OSINT_RUNNER_{self.tool_id.upper()}_URL")

    async def healthcheck(self) -> HealthResult:
        runner_url = self._runner_url()
        if runner_url:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.get(f"{runner_url}/healthz")
                    response.raise_for_status()
                    data = response.json()
                if data.get("status") != "ok" or data.get("tool") != self.tool_id:
                    return HealthResult(False, "tool runner returned an unexpected identity")
                return HealthResult(True, "ready", str(data.get("version") or "unknown")[:100])
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                return HealthResult(False, f"tool runner health check failed: {exc}")
        path = shutil.which(self.executable)
        if not path:
            return HealthResult(False, f"{self.executable} is not installed in this runner")
        version = await self.get_version()
        return HealthResult(True, "ready", version)

    async def get_version(self) -> str | None:
        runner_url = self._runner_url()
        if runner_url:
            health = await self.healthcheck()
            return health.version if health.healthy else None
        try:
            proc = await asyncio.create_subprocess_exec(
                self.executable,
                *self.version_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await asyncio.wait_for(proc.communicate(), 10)
            return output.decode(errors="replace").strip().splitlines()[0][:100]
        except (OSError, TimeoutError, IndexError):
            return None

    @abstractmethod
    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        """Return a fixed executable/argument array; never a shell string."""

    async def execute(
        self,
        command: list[str],
        output_dir: Path,
        timeout: int,
        query: str,
        log: LogCallback | None = None,
    ) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not command or command[0] != self.executable:
            self._session_context = None
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="adapter executable mismatch",
            )
        runner_url = self._runner_url()
        if runner_url:
            try:
                return await self._execute_remote(
                    runner_url, command, output_dir, timeout, query, log
                )
            finally:
                if self._session_context:
                    self._session_context.get("cookies", []).clear()
                    self._session_context.clear()
                self._session_context = None
        env = {"PATH": os.environ.get("PATH", ""), "HOME": "/tmp/osint-tool-home", "NO_COLOR": "1"}
        local_session_file: Path | None = None
        local_session_loaded: bool | None = None
        try:
            if self._session_context and self.local_session_format:
                suffix = ".json" if self.local_session_format == "json_context" else ".txt"
                descriptor, session_name = tempfile.mkstemp(
                    prefix=f"osint-{self.tool_id}-session-", suffix=suffix, dir="/tmp"
                )
                os.close(descriptor)
                local_session_file = Path(session_name)
                cookies = list(self._session_context.get("cookies", []))
                if self.local_session_format == "json_context":
                    local_session_file.write_text(
                        json.dumps(self._session_context, separators=(",", ":")), encoding="utf-8"
                    )
                    env["OSINT_SESSION_COOKIE_FILE"] = str(local_session_file)
                elif self.local_session_format == "netscape_cookie_jar":
                    lines = ["# Netscape HTTP Cookie File", "# Scoped export from Corvus"]
                    for cookie in cookies:
                        domain = str(cookie.get("domain") or "")
                        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
                        domain_field = (
                            f"#HttpOnly_{domain}" if cookie.get("http_only") else domain
                        )
                        expires_value = float(cookie.get("expires", -1) or -1)
                        expires = int(expires_value) if expires_value > 0 else 0
                        lines.append(
                            "\t".join(
                                (
                                    domain_field,
                                    include_subdomains,
                                    str(cookie.get("path") or "/"),
                                    "TRUE" if cookie.get("secure") else "FALSE",
                                    str(expires),
                                    str(cookie.get("name") or ""),
                                    str(cookie.get("value") or ""),
                                )
                            )
                        )
                    local_session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    command = [*command, self.local_session_flag, str(local_session_file)]
                else:
                    raise ValueError(f"unsupported local session format: {self.local_session_format}")
                local_session_file.chmod(0o600)
                local_session_loaded = bool(cookies)
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=output_dir,
                env=env,
                start_new_session=True,
            )
            stdout_task = asyncio.create_task(
                self._read_stream(self._process.stdout, query, "stdout", log)
            )
            stderr_task = asyncio.create_task(
                self._read_stream(self._process.stderr, query, "stderr", log)
            )
            try:
                await asyncio.wait_for(self._process.wait(), timeout)
            except TimeoutError:
                await self.cancel()
                stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
                return ExecutionResult(
                    None,
                    stdout,
                    stderr,
                    timed_out=True,
                    error_code="timeout",
                    error_message=f"tool exceeded {timeout}s",
                )
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            structured_status = structured_collector_status(stdout)
            combined = f"{stdout}\n{stderr}".lower()
            limited = (
                structured_status == "rate_limited"
                if structured_status is not None
                else self.detect_rate_limit(combined)
            )
            failed = bool(self._process.returncode)
            return ExecutionResult(
                self._process.returncode,
                stdout,
                stderr,
                output_files=[p for p in output_dir.iterdir() if p.is_file()],
                rate_limited=limited,
                error_code="upstream_exit" if failed else ("rate_limited" if limited else None),
                error_message=(
                    failure_message(stdout, stderr, query, self._process.returncode)
                    if failed
                    else ("upstream reported rate limiting" if limited else None)
                ),
                session_loaded=local_session_loaded,
            )
        except FileNotFoundError:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="unavailable",
                error_message=f"{self.executable} is unavailable",
            )
        except OSError as exc:
            return ExecutionResult(
                None, "", "", error_code="execution_error", error_message=str(exc)
            )
        finally:
            self._process = None
            if local_session_file is not None:
                local_session_file.unlink(missing_ok=True)
            if self._session_context:
                self._session_context.get("cookies", []).clear()
                self._session_context.clear()
            self._session_context = None

    async def _execute_remote(
        self,
        runner_url: str,
        command: list[str],
        output_dir: Path,
        timeout: int,
        query: str,
        log: LogCallback | None,
    ) -> ExecutionResult:
        job_id = str(uuid4())
        runner_output = f"/output/{job_id}"
        mapped = [arg.replace(str(output_dir), runner_output) for arg in command]
        payload: dict[str, Any] = {"job_id": job_id, "command": mapped, "timeout": timeout}
        if self._session_context:
            payload["session_context"] = self._session_context
        self._remote_job = (runner_url, job_id)
        try:
            async with asyncio.timeout(max(timeout + 15, 20)):
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(f"{runner_url}/jobs", json=payload)
                    if response.is_error:
                        return ExecutionResult(
                            None,
                            "",
                            "",
                            error_code="runner_error",
                            error_message=runner_error_message(response, query),
                        )
                    offset = 0
                    runner_job_recovered = False
                    while True:
                        poll = await client.get(
                            f"{runner_url}/jobs/{job_id}", params={"after": offset}
                        )
                        if poll.status_code == 404 and offset == 0 and not runner_job_recovered:
                            # A runner container can restart between accepting the job and the
                            # first poll, losing its in-memory job registry. Re-submit the same
                            # bounded UUID-scoped command once; output isolation makes this safe
                            # and avoids turning a transient runner restart into a false tool
                            # failure.
                            retry = await client.post(f"{runner_url}/jobs", json=payload)
                            if retry.is_error:
                                return ExecutionResult(
                                    None,
                                    "",
                                    "",
                                    error_code="runner_job_lost",
                                    error_message=(
                                        "runner lost the submitted job and recovery failed: "
                                        f"{runner_error_message(retry, query)}"
                                    ),
                                )
                            runner_job_recovered = True
                            await asyncio.sleep(0.1)
                            continue
                        poll.raise_for_status()
                        data = poll.json()
                        lines = data.get("logs", [])
                        offset += len(lines)
                        if log:
                            for entry in lines:
                                channel = str(entry.get("channel") or "output").casefold()
                                if channel not in {"stdout", "stderr"}:
                                    channel = "output"
                                await log(channel, redact(str(entry.get("line", "")), query))
                        if data.get("state") in {"completed", "failed", "timed_out", "cancelled"}:
                            for name, encoded in data.get("files", {}).items():
                                relative = Path(name)
                                if relative.is_absolute() or ".." in relative.parts:
                                    continue
                                destination = output_dir / relative
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                destination.write_bytes(base64.b64decode(encoded))
                            state = data["state"]
                            stdout, stderr = str(data.get("stdout", "")), str(data.get("stderr", ""))
                            structured_status = structured_collector_status(stdout)
                            limited = (
                                structured_status == "rate_limited"
                                if structured_status is not None
                                else self.detect_rate_limit(f"{stdout}\n{stderr}".lower())
                            )
                            error_code = data.get("error_code")
                            error_message = data.get("error_message")
                            if state == "failed" and not error_code:
                                error_code = "upstream_exit"
                                error_message = failure_message(
                                    stdout, stderr, query, data.get("returncode")
                                )
                            return ExecutionResult(
                                data.get("returncode"),
                                stdout,
                                stderr,
                                list(output_dir.iterdir()),
                                state == "timed_out",
                                state == "cancelled",
                                limited,
                                error_code or ("rate_limited" if limited else None),
                                error_message
                                or ("upstream reported rate limiting" if limited else None),
                                data.get("session_loaded"),
                                network_ok=data.get("dns_ok")
                                if isinstance(data.get("dns_ok"), bool)
                                else None,
                            )
                        await asyncio.sleep(0.4)
        except TimeoutError:
            with contextlib.suppress(httpx.HTTPError):
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.delete(f"{runner_url}/jobs/{job_id}")
            return ExecutionResult(
                None,
                "",
                "",
                timed_out=True,
                error_code="timeout",
                error_message=f"remote runner exceeded the {timeout}s tool deadline",
            )
        except (httpx.HTTPError, ValueError, OSError) as exc:
            return ExecutionResult(None, "", "", error_code="runner_error", error_message=str(exc))
        finally:
            self._remote_job = None

    async def _read_stream(
        self,
        stream: asyncio.StreamReader | None,
        query: str,
        channel: str,
        log: LogCallback | None,
    ) -> str:
        if stream is None:
            return ""
        chunks: list[str] = []
        while line := await stream.readline():
            clean = redact(line.decode(errors="replace").rstrip(), query)
            chunks.append(clean)
            if log:
                await log(channel, clean)
        return "\n".join(chunks)

    async def cancel(self) -> None:
        if self._remote_job:
            runner_url, job_id = self._remote_job
            with contextlib.suppress(httpx.HTTPError):
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.delete(f"{runner_url}/jobs/{job_id}")
            return
        if not self._process or self._process.returncode is not None:
            return
        try:
            if os.name == "nt":
                self._process.terminate()
            else:
                os.killpg(self._process.pid, signal.SIGTERM)
            await asyncio.wait_for(self._process.wait(), 5)
        except (ProcessLookupError, TimeoutError):
            if self._process.returncode is None:
                if os.name == "nt":
                    self._process.kill()
                else:
                    os.killpg(self._process.pid, signal.SIGKILL)
                await self._process.wait()

    @staticmethod
    def detect_rate_limit(text: str) -> bool:
        lowered = text.casefold()
        contextual_429 = re.search(
            r"\bhttp(?:/\d(?:\.\d)?)?\s+429\b|"
            r"\b(?:status(?:\s+code)?|error|response|returned|code)\s*[:=]?\s*429\b|"
            r"\b429\b\D{0,24}(?:too many requests|rate limit)",
            lowered,
        )
        return bool(contextual_429) or any(
            marker in lowered
            for marker in ("rate limited", "rate-limit", "too many requests", "temporarily blocked")
        )

    @abstractmethod
    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        pass
