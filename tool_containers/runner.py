"""Private fixed-command runner used inside one isolated tool container."""

import base64
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen

from session_bridge import (
    validate_session_context,
    write_instaloader_session_bundle,
    write_netscape_cookie_jar,
)

TOOL_ID = os.environ["TOOL_ID"]
UPSTREAM_COMMIT = os.environ.get("UPSTREAM_COMMIT", "unknown")
EXECUTABLES = {
    "maigret": "maigret",
    "sherlock": "sherlock",
    "blackbird": "blackbird",
    "naminter": "naminter",
    "holehe": "holehe",
    "theharvester": "theHarvester",
    "instaloader": "instaloader-osint",
    "gallery_dl": "gallery-dl",
    "yt_dlp_metadata": "yt-dlp",
    "nexfil": "nexfil",
    "social_analyzer": "social-analyzer-osint",
    "subfinder": "subfinder",
}
ALLOWED_FLAGS = {
    "maigret": {
        "--json",
        "--folderoutput",
        "--db",
        "--timeout",
        "--no-recursion",
        "--no-extracting",
        "--no-progressbar",
        "--proxy",
        "--no-autoupdate",
        "--all-sites",
    },
    "sherlock": {
        "--csv",
        "--folderoutput",
        "--no-color",
        "--timeout",
        "--proxy",
        "--local",
    },
    "blackbird": {"--username", "--email", "--json", "--no-update", "--timeout", "--proxy"},
    "naminter": {"--username", "--json", "--json-path", "--local-data", "--skip-validation", "--no-color", "--no-progressbar", "--filter-all", "--impersonate", "--max-tasks", "--timeout", "--proxy"},
    "holehe": {"--only-used", "--no-color"},
    "theharvester": {"-d", "-b", "-f"},
    "instaloader": {"--username", "--proxy"},
    "gallery_dl": {"--dump-json", "--no-download", "--no-input", "--config-ignore", "-o", "--proxy"},
    "yt_dlp_metadata": {"--skip-download", "--dump-single-json", "--no-playlist", "--no-warnings", "--socket-timeout", "--extractor-retries", "--impersonate", "--proxy", "--no-progress", "--no-part", "--max-filesize", "-f", "--write-info-json", "-o"},
    "nexfil": {"-u", "-t"},
    "social_analyzer": {"--username"},
    "subfinder": {"-d", "-o", "-oJ", "-cs", "-silent", "-timeout"},
}
FORBIDDEN = {"--ai", "--setup-ai", "--web", "--browse", "--pdf", "--update", "--self-check"}
jobs: dict[str, dict[str, Any]] = {}
lock = threading.Lock()
execution_lock = threading.Lock()
probe_lock = threading.Lock()
MAX_JOB_TIMEOUT_SECONDS = 3600
PROBE_RETRY_SECONDS = 5.0
Path("/tmp/tool-home").mkdir(mode=0o700, parents=True, exist_ok=True)


def probe_health() -> tuple[bool, str]:
    executable = EXECUTABLES[TOOL_ID]
    if not shutil.which(executable):
        return False, f"{executable} is not installed"
    probes = {
        "maigret": [executable, "--version", "--no-autoupdate"],
        "sherlock": [
            "python",
            "-c",
            "from importlib.metadata import version; print(version('sherlock-project'))",
        ],
        "blackbird": [executable, "--about", "--no-update"],
        "naminter": [executable, "--version"],
        "holehe": [executable, "--help"],
        "theharvester": [
            "python",
            "-c",
            "from importlib.metadata import version; print(version('theHarvester'))",
        ],
        "instaloader": ["python", "-c", "import instaloader; print(instaloader.__version__)"],
        "gallery_dl": ["gallery-dl", "--version"],
        "yt_dlp_metadata": ["yt-dlp", "--version"],
        "nexfil": [executable, "-v"],
        "social_analyzer": [
            "python",
            "-c",
            (
                "from importlib.util import module_from_spec,spec_from_file_location;"
                "s=spec_from_file_location('social_analyzer_upstream','/opt/tool/app.py');"
                "assert s and s.loader;m=module_from_spec(s);s.loader.exec_module(m);"
                "print(m.SocialAnalyzer.__name__)"
            ),
        ],
        "subfinder": [executable, "-version"],
    }
    try:
        result = subprocess.run(
            probes[TOOL_ID],
            cwd="/tmp/tool-home",
            env={"PATH": os.environ.get("PATH", ""), "HOME": "/tmp/tool-home", "NO_COLOR": "1"},
            capture_output=True,
            text=True,
            errors="replace",
            shell=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"startup probe failed: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return False, (detail[-1] if detail else f"probe exited {result.returncode}")[:300]
    return True, "ready"


PROBE_HEALTHY = False
PROBE_MESSAGE = "startup probe has not completed"
PROBE_CHECKED_AT = -PROBE_RETRY_SECONDS


def current_probe_health() -> tuple[bool, str]:
    """Retry a transient startup probe until the installed tool is confirmed ready."""
    global PROBE_CHECKED_AT, PROBE_HEALTHY, PROBE_MESSAGE

    now = time.monotonic()
    if PROBE_HEALTHY or now - PROBE_CHECKED_AT < PROBE_RETRY_SECONDS:
        return PROBE_HEALTHY, PROBE_MESSAGE
    with probe_lock:
        now = time.monotonic()
        if PROBE_HEALTHY or now - PROBE_CHECKED_AT < PROBE_RETRY_SECONDS:
            return PROBE_HEALTHY, PROBE_MESSAGE
        PROBE_HEALTHY, PROBE_MESSAGE = probe_health()
        PROBE_CHECKED_AT = now
    return PROBE_HEALTHY, PROBE_MESSAGE


PROBE_HEALTHY, PROBE_MESSAGE = current_probe_health()


def write_maigret_public_enrichment(output: Path, command: list[str]) -> None:
    """Retain bounded public metadata that Maigret detects but does not fully extract."""
    if len(command) < 2 or not re.fullmatch(r"[\w.\-]{1,64}", command[1], re.UNICODE):
        return
    username = command[1]
    report_paths = sorted(
        (
            path
            for path in output.glob("report_*_simple.json")
            if path.is_file() and path.stat().st_size <= 5_000_000
        ),
        key=lambda path: path.stat().st_size,
        reverse=True,
    )
    if not report_paths:
        return
    try:
        report = json.loads(report_paths[0].read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    if not isinstance(report, dict):
        return

    def claimed(site_name: str) -> bool:
        site = report.get(site_name)
        status = site.get("status", {}) if isinstance(site, dict) else {}
        return str(status.get("status", "")).casefold() in {"claimed", "found", "exists", "true"}

    enrichment: dict[str, Any] = {}

    if claimed("Duolingo"):
        request = Request(
            "https://www.duolingo.com/2017-06-30/users?username=" + quote(username),
            headers={"Accept": "application/json", "User-Agent": "Corvus/1.0"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=12) as response:  # noqa: S310 - fixed public HTTPS endpoint
                payload = response.read(1_000_001)
            data = json.loads(payload) if len(payload) <= 1_000_000 else None
        except (OSError, ValueError, json.JSONDecodeError):
            data = None
        users = data.get("users") if isinstance(data, dict) else None
        if isinstance(users, list):
            user = next(
                (
                    item
                    for item in users
                    if isinstance(item, dict)
                    and str(item.get("username", "")).casefold() == username.casefold()
                ),
                None,
            )
            if user is not None:
                enrichment["Duolingo"] = user

    if claimed("SoundCloud"):
        request = Request(
            "https://soundcloud.com/" + quote(username),
            headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0 Corvus/1.0"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=12) as response:  # noqa: S310 - fixed public HTTPS endpoint
                payload = response.read(1_000_001)
            page = payload.decode("utf-8", errors="replace") if len(payload) <= 1_000_000 else ""
        except OSError:
            page = ""
        hydration = re.search(
            r"window\.__sc_hydration\s*=\s*(\[.*?\])\s*;</script>",
            page,
            re.DOTALL,
        )
        if hydration:
            try:
                items = json.loads(hydration.group(1))
            except json.JSONDecodeError:
                items = None
            if isinstance(items, list):
                user = next(
                    (
                        entry.get("data")
                        for entry in items
                        if isinstance(entry, dict)
                        and entry.get("hydratable") == "user"
                        and isinstance(entry.get("data"), dict)
                        and str(entry["data"].get("permalink") or entry["data"].get("username") or "").casefold()
                        == username.casefold()
                    ),
                    None,
                )
                if isinstance(user, dict):
                    allowed = {
                        "avatar_url",
                        "city",
                        "comments_count",
                        "country_code",
                        "created_at",
                        "description",
                        "followers_count",
                        "followings_count",
                        "full_name",
                        "id",
                        "last_modified",
                        "likes_count",
                        "permalink",
                        "permalink_url",
                        "playlist_count",
                        "reposts_count",
                        "track_count",
                        "username",
                        "verified",
                    }
                    enrichment["SoundCloud"] = {
                        key: value for key, value in user.items() if key in allowed and value is not None
                    }

    if claimed("Twitch"):
        request = Request(
            "https://twitchtracker.com/" + quote(username),
            headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0 Corvus/1.0"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=12) as response:  # noqa: S310 - fixed public HTTPS endpoint
                payload = response.read(1_000_001)
            page = payload.decode("utf-8", errors="replace") if len(payload) <= 1_000_000 else ""
        except OSError:
            page = ""
        supplemental: dict[str, Any] = {}
        channel = re.search(
            r"window\.channel\s*=\s*\{\s*id:\s*(\d+),\s*name:\s*'([^']+)',\s*created_at:\s*'([^']+)'\s*\}",
            page,
        )
        if channel and channel.group(2).casefold() == username.casefold():
            supplemental["twitch_channel_id"] = channel.group(1)
            supplemental["twitch_username"] = channel.group(2)
            supplemental["created_at"] = channel.group(3)
        full_created = re.search(
            r'title="(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"[^>]*>\s*<div[^>]*>Created</div>',
            page,
            re.DOTALL | re.IGNORECASE,
        )
        if full_created:
            supplemental["created_at"] = full_created.group(1)
        updated = re.search(
            r'id="page-update-dt"[^>]*>\s*([^<]+?)\s*</span>',
            page,
            re.IGNORECASE,
        )
        if updated:
            supplemental["tracker_updated_at"] = updated.group(1).strip()
        if supplemental:
            enrichment["Twitch"] = supplemental

    if not enrichment:
        return
    destination = output / "maigret-public-enrichment.json"
    destination.write_text(json.dumps(enrichment, ensure_ascii=False, indent=2), encoding="utf-8")
    destination.chmod(0o600)


def write_blackbird_public_enrichment(output: Path, command: list[str]) -> None:
    """Retain bounded first-party account evidence Blackbird itself discards."""
    if "--username" not in command:
        return
    index = command.index("--username")
    if index + 1 >= len(command):
        return
    username = command[index + 1]
    if not re.fullmatch(r"[\w.\-]{1,64}", username, re.UNICODE):
        return

    # Blackbird's Roblox check uses the first-party username-validation endpoint,
    # but its JSON output keeps only the URL/status and discards the useful API
    # response. Resolve the same username through Roblox's documented public user
    # lookup so Corvus can retain an account ID/display name as evidence instead of
    # treating the hit as a bare constructed URL assertion.
    body = json.dumps({"usernames": [username], "excludeBannedUsers": False}).encode("utf-8")
    request = Request(
        "https://users.roblox.com/v1/usernames/users",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Corvus/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=12) as response:  # noqa: S310 - fixed public HTTPS endpoint
            payload = response.read(500_001)
        data = json.loads(payload) if len(payload) <= 500_000 else None
    except (OSError, ValueError, json.JSONDecodeError):
        data = None
    users = data.get("data") if isinstance(data, dict) else None
    user = next(
        (
            item
            for item in users or []
            if isinstance(item, dict)
            and str(item.get("requestedUsername") or item.get("name") or "").casefold()
            == username.casefold()
        ),
        None,
    )
    if not isinstance(user, dict) or not user.get("id"):
        return
    enrichment = {
        "Roblox": {
            "blackbird_public_verification": {
                "source": "https://users.roblox.com/v1/usernames/users",
                "result": "exact_username_match",
            },
            "roblox_user_id": user.get("id"),
            "roblox_username": user.get("name") or username,
            "roblox_display_name": user.get("displayName"),
            "roblox_verified": user.get("hasVerifiedBadge"),
        }
    }
    destination = output / "blackbird-public-enrichment.json"
    destination.write_text(json.dumps(enrichment, ensure_ascii=False, indent=2), encoding="utf-8")
    destination.chmod(0o600)


def validate(command: Any, job_id: str) -> list[str]:
    if not isinstance(command, list) or not command or len(command) > 20:
        raise ValueError("invalid command array")
    args = [str(item) for item in command]
    if args[0] != EXECUTABLES[TOOL_ID]:
        raise ValueError("executable mismatch")
    for arg in args:
        if len(arg) > 600 or "\x00" in arg or "\n" in arg or "\r" in arg:
            raise ValueError("invalid argument")
        if (
            arg.startswith("-")
            and arg not in ALLOWED_FLAGS[TOOL_ID]
            and not re.fullmatch(r"-?\d+", arg)
        ):
            raise ValueError(f"option is not allowed: {arg}")
        if arg in FORBIDDEN:
            raise ValueError("disabled feature")
        if arg.startswith("/output/") and not arg.startswith(f"/output/{job_id}"):
            raise ValueError("output path escapes job directory")
    return args


def reader(pipe: Any, channel: str, job: dict[str, Any]) -> None:
    for line in iter(pipe.readline, ""):
        with lock:
            job["logs"].append({"channel": channel, "line": line.rstrip()[:8000]})
            job[channel].append(line)
    pipe.close()


def dns_probe(
    hosts: tuple[str, ...] = ("example.com", "cloudflare.com", "github.com"),
    resolver: Any = socket.getaddrinfo,
) -> bool:
    """True as soon as one host resolves; False when every lookup fails."""
    for host in hosts:
        try:
            resolver(host, 443)
            return True
        except OSError:
            continue
    return False


def _bounded_dns_probe() -> bool | None:
    """Run dns_probe in a daemon thread; None when unknown (error or no answer in time)."""
    outcome: list[bool] = []

    def target() -> None:
        try:
            outcome.append(dns_probe())
        except Exception:  # noqa: S110 - an unknown result is reported as None
            return

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=6)
    return outcome[0] if outcome else None


def _run_job(
    job_id: str,
    command: list[str],
    timeout: int,
    session_context: dict[str, Any],
) -> None:
    output = Path("/output") / job_id
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    if TOOL_ID == "maigret":
        shutil.copyfile("/opt/tool/maigret/resources/data.json", output / "maigret-data.json")
    if TOOL_ID == "blackbird":
        for name in ("assets", "data"):
            source = Path("/opt/tool") / name
            if source.exists():
                (output / name).symlink_to(source, target_is_directory=True)
    job = jobs[job_id]
    session_file: Path | None = None
    try:
        prepared_command = list(command)
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": "/tmp/tool-home",
            "NO_COLOR": "1",
        }
        if TOOL_ID == "blackbird":
            environment["BLACKBIRD_RESULTS_DIR"] = str(output)
        if TOOL_ID == "instaloader":
            environment["INSTALOADER_SESSION_USERNAME"] = os.environ.get(
                "INSTALOADER_SESSION_USERNAME", ""
            )
        session_cookies = session_context.get("cookies", [])
        session_loaded = False
        if session_cookies:
            suffix = ".json" if TOOL_ID == "instaloader" else ".txt"
            session_file = Path("/tmp") / f"osint-session-{job_id}{suffix}"
            if TOOL_ID == "instaloader":
                write_instaloader_session_bundle(session_file, session_context)
                environment["OSINT_SESSION_COOKIE_FILE"] = str(session_file)
                session_loaded = True
            elif TOOL_ID == "maigret":
                write_netscape_cookie_jar(session_file, session_cookies)
                prepared_command.extend(("--cookies-jar-file", str(session_file)))
                session_loaded = True
            elif TOOL_ID == "gallery_dl":
                write_netscape_cookie_jar(session_file, session_cookies)
                prepared_command.extend(("--cookies", str(session_file)))
                session_loaded = True
        process = subprocess.Popen(
            prepared_command,
            cwd=output,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
            shell=False,
        )
        with lock:
            job.update(state="running", process=process, session_loaded=session_loaded)
        threads = [
            threading.Thread(target=reader, args=(process.stdout, "stdout", job), daemon=True),
            threading.Thread(target=reader, args=(process.stderr, "stderr", job), daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            returncode = process.wait(timeout=timeout)
            state = "completed" if returncode == 0 else "failed"
            if TOOL_ID == "maigret" and returncode == 0:
                write_maigret_public_enrichment(output, prepared_command)
            elif TOOL_ID == "blackbird" and returncode == 0:
                write_blackbird_public_enrichment(output, prepared_command)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            returncode, state = None, "timed_out"
        for thread in threads:
            thread.join(timeout=2)
        files = {}
        for path in output.rglob("*"):
            if path.is_file() and path.stat().st_size <= 25_000_000:
                files[str(path.relative_to(output))] = base64.b64encode(path.read_bytes()).decode()
        dns_ok: bool | None = None
        if state != "timed_out" and "--proxy" not in prepared_command:
            if TOOL_ID != "nexfil" or "-ph" not in prepared_command:
                dns_ok = _bounded_dns_probe()
        with lock:
            if job["state"] != "cancelled":
                job.update(
                    state=state,
                    dns_ok=dns_ok,
                    returncode=returncode,
                    files=files,
                    process=None,
                    error_code="timeout" if state == "timed_out" else None,
                    error_message=(
                        f"tool exceeded the configured {timeout}s timeout"
                        if state == "timed_out"
                        else None
                    ),
                )
    except (OSError, ValueError) as exc:
        with lock:
            job.update(
                state="failed", error_code="execution_error", error_message=str(exc), process=None
            )
    finally:
        if session_file is not None:
            session_file.unlink(missing_ok=True)
        session_context.get("cookies", []).clear()
        session_context.clear()


def run_job(
    job_id: str,
    command: list[str],
    timeout: int,
    session_context: dict[str, Any],
) -> None:
    with execution_lock:
        _run_job(job_id, command, timeout, session_context)


class Handler(BaseHTTPRequestHandler):
    server_version = "OSINTToolRunner/1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_json(self, code: int, data: dict[str, Any]) -> None:
        payload = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/healthz":
            healthy, message = current_probe_health()
            self.send_json(
                200 if healthy else 503,
                {
                    "status": "ok" if healthy else "error",
                    "tool": TOOL_ID,
                    "version": f"source@{UPSTREAM_COMMIT[:12]}",
                    "commit": UPSTREAM_COMMIT,
                    "message": message,
                },
            )
            return
        job_id = parts.path.removeprefix("/jobs/")
        with lock:
            job = jobs.get(job_id)
            if not job:
                self.send_json(404, {"error": "not found"})
                return
            after = max(0, int(parse_qs(parts.query).get("after", [0])[0]))
            data = {key: value for key, value in job.items() if key != "process"}
            data["logs"] = job["logs"][after:]
            data["stdout"] = "".join(job["stdout"])
            data["stderr"] = "".join(job["stderr"])
        self.send_json(200, data)

    def do_POST(self) -> None:
        if self.path != "/jobs":
            self.send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 512_000:
                raise ValueError("invalid request size")
            data = json.loads(self.rfile.read(length))
            job_id = str(data["job_id"])
            if not re.fullmatch(r"[0-9a-f-]{36}", job_id):
                raise ValueError("invalid job id")
            timeout = max(10, min(MAX_JOB_TIMEOUT_SECONDS, int(data["timeout"])))
            command = validate(data["command"], job_id)
            raw_session_context = data.get("session_context")
            if raw_session_context is None and data.get("session_cookies") is not None:
                raw_session_context = {"cookies": data.get("session_cookies")}
            session_context = validate_session_context(TOOL_ID, raw_session_context)
            with lock:
                if job_id in jobs:
                    raise ValueError("duplicate job")
                jobs[job_id] = {
                    "state": "queued",
                    "logs": [],
                    "stdout": [],
                    "stderr": [],
                    "files": {},
                    "returncode": None,
                    "error_code": None,
                    "error_message": None,
                    "session_loaded": False,
                    "created": time.time(),
                    "process": None,
                }
            threading.Thread(
                target=run_job,
                args=(job_id, command, timeout, session_context),
                daemon=True,
            ).start()
            self.send_json(202, {"job_id": job_id})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})

    def do_DELETE(self) -> None:
        job_id = self.path.removeprefix("/jobs/")
        with lock:
            job = jobs.get(job_id)
            process = job.get("process") if job else None
            if job:
                job["state"] = "cancelled"
                job["error_code"] = "cancelled"
                job["error_message"] = "cancelled by investigator"
        if not job:
            self.send_json(404, {"error": "not found"})
            return
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.send_json(200, {"state": "cancelled"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
