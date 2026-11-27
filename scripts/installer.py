"""Cross-platform, explicit installer/updater for a checked release tree."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".install-state.json"
DEFAULT_REPOSITORY_URL = "https://github.com/sn0ba11/corvus.git"
UPDATE_BRANCH = "main"
UNKNOWN_BUILD_COMMIT = "0000000000000000000000000000000000000000"
CORE_SERVICES = (
    "app",
    "maigret",
    "sherlock",
    "naminter",
    "holehe",
    "theharvester",
    "subfinder",
    "instaloader",
    "gallery-dl",
    "yt-dlp",
    "nexfil",
    "social_analyzer",
    "searxng",
    "browser-agent",
)
ALL_SERVICES = CORE_SERVICES
IMAGE_TAGS = {
    service: f"corvus-{service.replace('_', '-')}:latest" for service in ALL_SERVICES
}


def run(args: list[str], *, capture: bool = False, check: bool = True) -> str:
    result = subprocess.run(
        args, cwd=ROOT, check=check, shell=False, text=True, capture_output=capture
    )
    return result.stdout.strip() if capture else ""


def env_values() -> dict[str, str]:
    target = ROOT / ".env"
    if not target.exists():
        return {}
    values: dict[str, str] = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def services() -> tuple[str, ...]:
    return CORE_SERVICES


def compose(*args: str, capture: bool = False, check: bool = True) -> str:
    command = ["docker", "compose"]
    command.extend(args)
    return run(command, capture=capture, check=check)


def repository_url_valid(value: str) -> bool:
    # The address is passed to git as an argument, so it must never look like an option.
    if value.startswith("-"):
        return False
    return bool(
        re.fullmatch(r"(?:https?|ssh)://\S+", value) or re.fullmatch(r"[^@\s]+@[^:\s]+:.+", value)
    )


def update_changelog_path() -> Path:
    return ROOT / ".last-update-changes.txt"


def pending_update_changelog_path() -> Path:
    return ROOT / ".pending-update-changes.txt"


def runtime_update_changelog_path() -> Path:
    return ROOT / "runtime" / "last-update-changes.txt"


def update_range_path() -> Path:
    return ROOT / ".last-update-range.json"


def pending_update_range_path() -> Path:
    return ROOT / ".pending-update-range.json"


def runtime_update_range_path() -> Path:
    return ROOT / "runtime" / "last-update-range.json"


def source_commit() -> str:
    if (ROOT / ".git").exists() and shutil.which("git"):
        commit = run(["git", "rev-parse", "HEAD"], capture=True, check=False).strip().lower()
        if re.fullmatch(r"[0-9a-f]{40}", commit):
            return commit
    configured = env_values().get("OSINT_BUILD_COMMIT", "").strip().lower()
    return configured if re.fullmatch(r"[0-9a-f]{7,40}", configured) else UNKNOWN_BUILD_COMMIT


def _write_update_range(path: Path, previous: str, current: str, *, fallback_used: bool) -> None:
    path.write_text(
        json.dumps(
            {
                "from_commit": previous,
                "to_commit": current,
                "fallback_used": fallback_used,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def sync_update_metadata_to_runtime() -> None:
    runtime_root = ROOT / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(update_changelog_path(), runtime_update_changelog_path())
    if update_range_path().is_file():
        shutil.copyfile(update_range_path(), runtime_update_range_path())


def _render_update_changelog(previous: str, current: str) -> str | None:
    if previous == current:
        return None
    changes = run(
        [
            "git",
            "log",
            "--no-merges",
            "--date=short",
            "--pretty=format:@@CHANGE@@%n%ad%n%H%n%s%n%b%n@@END@@",
            f"{previous}..{current}",
        ],
        capture=True,
        check=False,
    ).strip()
    if not changes:
        changes = f"Updated source from {previous[:12]} to {current[:12]}."
    return f"Corvus changes installed:\n{changes}\n"


def _print_update_changelog(previous: str, current: str) -> None:
    """Print and retain a concise list of commits included in an update."""
    rendered = _render_update_changelog(previous, current)
    if rendered is None:
        return
    print(f"\n{rendered}")
    update_changelog_path().write_text(rendered, encoding="utf-8")
    _write_update_range(update_range_path(), previous, current, fallback_used=False)
    sync_update_metadata_to_runtime()


def _stage_update_changelog(previous: str, current: str) -> None:
    rendered = _render_update_changelog(previous, current)
    if rendered is not None:
        pending_update_changelog_path().write_text(rendered, encoding="utf-8")
        _write_update_range(pending_update_range_path(), previous, current, fallback_used=False)


def promote_pending_update_changelog() -> None:
    pending_changelog = pending_update_changelog_path()
    pending_range = pending_update_range_path()
    rendered = ""
    promoted = False
    if pending_changelog.is_file():
        rendered = pending_changelog.read_text(encoding="utf-8")
        pending_changelog.replace(update_changelog_path())
        promoted = True
    if pending_range.is_file():
        pending_range.replace(update_range_path())
        promoted = True
    if not promoted:
        return
    sync_update_metadata_to_runtime()
    if rendered:
        print(f"\n{rendered}")


def ensure_update_changelog() -> None:
    path = update_changelog_path()
    if not path.is_file():
        path.write_text(
            "Corvus changes installed:\nInitial installation.\n",
            encoding="utf-8",
        )
    if not update_range_path().is_file():
        current = source_commit()
        existing_install = (ROOT / ".install-state.json").is_file()
        _write_update_range(
            update_range_path(),
            UNKNOWN_BUILD_COMMIT if existing_install else current,
            current,
            fallback_used=existing_install,
        )
    sync_update_metadata_to_runtime()


def _selected_source(origin: str, repository_url: str | None = None) -> str:
    """The address an update would use: an explicit one, else origin (legacy ones migrate)."""
    return repository_url or origin or DEFAULT_REPOSITORY_URL


def current_update_source() -> str:
    """The address the next update would fetch from, without contacting it."""
    origin = ""
    if (ROOT / ".git").exists() and shutil.which("git"):
        origin = run(["git", "remote", "get-url", "origin"], capture=True, check=False)
    return _selected_source(origin)


def update_source(repository_url: str | None = None, *, check_only: bool = False) -> bool:
    if not (ROOT / ".git").exists():
        if check_only:
            raise SystemExit("Checking for updates requires a Git checkout.")
        print("This is not a Git clone; updating the currently extracted release only.")
        return False
    if not shutil.which("git"):
        raise SystemExit(
            "Git is required to update a cloned release; install Git or use --skip-source-update."
        )
    origin = run(["git", "remote", "get-url", "origin"], capture=True, check=False)
    selected = _selected_source(origin, repository_url)
    if not repository_url_valid(selected):
        raise SystemExit("Repository URL must be HTTP(S), SSH, or Git's user@host:path form.")
    if run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        capture=True,
    ):
        raise SystemExit("The source checkout has local changes; commit or remove them first.")
    branch = run(["git", "branch", "--show-current"], capture=True)
    if branch != UPDATE_BRANCH:
        raise SystemExit(
            f"Updates require branch {UPDATE_BRANCH}; current checkout is {branch or 'detached HEAD'}. "
            "Switch branches explicitly before updating."
        )
    previous = run(["git", "rev-parse", "HEAD"], capture=True)
    branch_ref = f"refs/heads/{UPDATE_BRANCH}"
    tracking_ref = f"refs/remotes/origin/{UPDATE_BRANCH}"
    # A check is a dry run: a different address is fetched into a throwaway ref and origin is
    # left alone. Only a real update saves the address as origin.
    if check_only and selected != origin:
        probe_ref = "refs/corvus/update-check"
        try:
            run(["git", "fetch", selected, f"{branch_ref}:{probe_ref}"])
            available = run(["git", "rev-parse", probe_ref], capture=True)
            counts = run(["git", "rev-list", "--left-right", "--count", f"HEAD...{probe_ref}"],
                         capture=True)
        finally:
            run(["git", "update-ref", "-d", probe_ref], check=False)
        ahead, behind = (int(value) for value in counts.split())
        if ahead:
            raise SystemExit(
                f"Local main has {ahead} unpublished commit(s) and is {behind} commit(s) behind "
                f"{selected}. Resolve local history before updating."
            )
        print(json.dumps({"current_commit": previous, "available_commit": available,
                          "commits_behind": behind, "update_available": bool(behind),
                          "source": selected}, indent=2))
        return bool(behind)
    if selected != origin:
        action = "set-url" if origin else "add"
        run(["git", "remote", action, "origin", selected])
    run(["git", "fetch", "--prune", "origin", f"{branch_ref}:{tracking_ref}"])
    available = run(["git", "rev-parse", tracking_ref], capture=True)
    # A local-ahead checkout must not be reported as an up-to-date release.
    counts = run(["git", "rev-list", "--left-right", "--count", f"HEAD...{tracking_ref}"], capture=True)
    ahead, behind = (int(value) for value in counts.split())
    if ahead:
        raise SystemExit(
            f"Local main has {ahead} unpublished commit(s) and is {behind} commit(s) behind origin/main. "
            "Resolve local history before updating; no source was merged."
        )
    if check_only:
        print(json.dumps({"current_commit": previous, "available_commit": available,
                          "commits_behind": behind, "update_available": bool(behind)}, indent=2))
        return bool(behind)
    run(["git", "config", f"branch.{UPDATE_BRANCH}.remote", "origin"])
    run(["git", "config", f"branch.{UPDATE_BRANCH}.merge", branch_ref])
    if available != previous:
        run(["git", "merge", "--ff-only", tracking_ref])
    current = run(["git", "rev-parse", "HEAD"], capture=True)
    if current != available:
        raise SystemExit("Source update verification failed: HEAD does not match fetched origin/main.")
    changed = current != previous
    if changed:
        _stage_update_changelog(previous, current)
    return changed


def docker_engine_available() -> bool:
    docker_path = shutil.which("docker")
    if docker_path is None:
        return False
    result = subprocess.run(
        [docker_path, "info", "--format", "{{.ServerVersion}}"],
        cwd=ROOT,
        check=False,
        shell=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def start_docker_desktop() -> bool:
    if sys.platform != "darwin":
        return False
    result = subprocess.run(
        ["/usr/bin/open", "-gj", "-a", "Docker"],
        cwd=ROOT,
        check=False,
        shell=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def ensure_docker_engine(*, deadline: int = 240) -> None:
    if docker_engine_available():
        return
    launched = start_docker_desktop()
    if sys.platform == "darwin" and not launched:
        raise SystemExit(
            "Docker Desktop is not running and macOS could not open the Docker application. "
            "Install Docker Desktop, start it once, and retry."
        )
    if launched:
        print("Starting Docker Desktop and waiting for its Linux engine...")
    else:
        print("Waiting for the Docker engine to become available...")
    end = time.monotonic() + deadline
    next_update = time.monotonic() + 15
    while time.monotonic() < end:
        if docker_engine_available():
            print("Docker engine is ready.")
            return
        if time.monotonic() >= next_update:
            remaining = max(0, int(end - time.monotonic()))
            print(f"Docker is still starting; up to {remaining} seconds remain.")
            next_update += 15
        time.sleep(3)
    raise SystemExit(
        "Docker did not become ready within four minutes. Open Docker Desktop, wait until it "
        "reports that the engine is running, and retry."
    )


def prerequisites() -> None:
    if not shutil.which("docker"):
        raise SystemExit(
            "Docker Desktop (Windows/macOS) or Docker Engine with Compose (Linux) is required."
        )
    try:
        compose("version", capture=True)
    except subprocess.CalledProcessError as exc:
        raise SystemExit("Docker Compose v2 is required.") from exc
    ensure_docker_engine()


def set_env_value(content: str, key: str, value: str) -> str:
    lines = content.splitlines()
    replacement = f"{key}={value}"
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    return "\n".join(lines) + "\n"


def ensure_env(*, enable_browser: bool = True) -> None:
    target = ROOT / ".env"
    if target.exists():
        content = target.read_text(encoding="utf-8")
    else:
        content = (ROOT / ".env.example").read_text(encoding="utf-8")
    # The managed browser is an integral service. Keep the argument only so older
    # automation that still passes --browser remains compatible.
    _ = enable_browser
    content = set_env_value(content, "OSINT_BROWSER_ENABLED", "true")
    content = set_env_value(content, "OSINT_BUILD_COMMIT", source_commit())
    content = set_env_value(content, "OSINT_VERSION", "0.2.0-rc13")
    values = {
        line.partition("=")[0]: line.partition("=")[2]
        for line in content.splitlines()
        if "=" in line
    }
    session_secret = values.get("OSINT_SECRET_KEY", "").strip()
    if len(session_secret) < 32 or session_secret.startswith("replace-with-"):
        content = set_env_value(content, "OSINT_SECRET_KEY", secrets.token_urlsafe(48))
    current_searxng_value = values.get("OSINT_SEARXNG_SECRET", "")
    if (
        len(current_searxng_value) < 32
        or current_searxng_value == "replace-with-a-distinct-searxng-secret"
    ):
        content = set_env_value(content, "OSINT_SEARXNG_SECRET", secrets.token_urlsafe(48))
    current_agent_value = values.get("OSINT_BROWSER_AGENT_TOKEN", "")
    if len(current_agent_value) < 32 or current_agent_value == "replace-with-browser-agent-token":
        content = set_env_value(content, "OSINT_BROWSER_AGENT_TOKEN", secrets.token_urlsafe(48))
    example_values = {
        line.partition("=")[0]: line.partition("=")[2]
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line
    }
    configured_browser_hosts = values.get("OSINT_BROWSER_ALLOWED_HOSTS", "").strip()
    default_browser_hosts = example_values.get("OSINT_BROWSER_ALLOWED_HOSTS", "")
    browser_hosts = [
        item.strip().casefold()
        for item in (configured_browser_hosts or default_browser_hosts).split(",")
        if item.strip()
    ]
    for required_host in default_browser_hosts.split(","):
        required_host = required_host.strip().casefold()
        if required_host and required_host not in browser_hosts:
            browser_hosts.append(required_host)
    content = set_env_value(
        content,
        "OSINT_BROWSER_ALLOWED_HOSTS",
        ",".join(dict.fromkeys(browser_hosts)),
    )
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=ROOT, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.close()
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def port_available(port: int) -> bool:
    if not 1 <= port <= 65535:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def available_port(start: int, end: int, *, excluded: set[int] | None = None) -> int:
    blocked = excluded or set()
    for port in range(start, end + 1):
        if port in blocked:
            continue
        if port_available(port):
            return port
    for _ in range(16):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            try:
                listener.bind(("127.0.0.1", 0))
            except OSError:
                continue
            candidate = int(listener.getsockname()[1])
        if candidate not in blocked:
            return candidate
    raise SystemExit(
        f"The operating system could not allocate a usable loopback port after {start}-{end} was unavailable."
    )


def ensure_available_ports() -> None:
    target = ROOT / ".env"
    content = target.read_text(encoding="utf-8")
    values = env_values()
    assignments = (
        ("OSINT_PORT", 8000, 8099),
        ("OSINT_BROWSER_DESKTOP_PORT", 6080, 6179),
    )
    selected_ports: set[int] = set()
    for key, default, maximum in assignments:
        raw = values.get(key, str(default))
        selected = int(raw) if raw.isdigit() and 1 <= int(raw) <= 65535 else default
        if port_available(selected) and selected not in selected_ports:
            selected_ports.add(selected)
            continue
        replacement = available_port(default, maximum, excluded=selected_ports)
        content = set_env_value(content, key, str(replacement))
        values[key] = str(replacement)
        selected_ports.add(replacement)
        print(f"Loopback port {selected} is occupied or unavailable; {key} was set to {replacement}.")
    browser_port = values.get("OSINT_BROWSER_DESKTOP_PORT", "6080")
    content = set_env_value(
        content,
        "OSINT_BROWSER_DESKTOP_URL",
        f"http://127.0.0.1:{browser_port}/vnc.html?autoconnect=true&resize=scale",
    )
    target.write_text(content, encoding="utf-8")


def configured_url() -> str:
    port = env_values().get("OSINT_PORT", "8000")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        port = "8000"
    return f"http://127.0.0.1:{port}"


def health(deadline: int = 180) -> bool:
    end = time.monotonic() + deadline
    expected = services()
    while time.monotonic() < end:
        output = compose("ps", "--format", "json", capture=True, check=False)
        try:
            parsed = json.loads(output)
            containers = parsed if isinstance(parsed, list) else [parsed]
        except (json.JSONDecodeError, TypeError):
            try:
                containers = [json.loads(line) for line in output.splitlines() if line.strip()]
            except (json.JSONDecodeError, TypeError):
                containers = []
        states = {
            str(item.get("Service")): (item.get("State"), item.get("Health")) for item in containers
            if isinstance(item, dict)
        }
        if all(states.get(service) == ("running", "healthy") for service in expected):
            return True
        time.sleep(3)
    return False


def snapshot_images() -> dict[str, str]:
    result = {}
    for service in services():
        container = compose("ps", "-q", service, capture=True, check=False)
        if container:
            image = run(
                ["docker", "inspect", "--format", "{{.Image}}", container],
                capture=True,
                check=False,
            )
            if image:
                result[service] = image
    return result


def service_running(service: str) -> bool:
    container = compose("ps", "-q", service, capture=True, check=False)
    if not container:
        return False
    running = run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container],
        capture=True,
        check=False,
    )
    return running.strip().lower() == "true"


def backup() -> None:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    code = (
        "import sqlite3,pathlib; p=pathlib.Path('/data/backups'); p.mkdir(exist_ok=True); s=sqlite3.connect('/data/osint.db'); d=sqlite3.connect(str(p/'osint-"
        + stamp
        + ".db')); s.backup(d); d.close(); s.close()"
    )
    if service_running("app"):
        compose("exec", "-T", "app", "python", "-c", code)
    else:
        print(
            "Application container is stopped; creating the backup through a temporary recovery container."
        )
        compose("run", "--rm", "--no-deps", "--entrypoint", "python", "app", "-c", code)
    print(f"Database backup created in the persistent volume: backups/osint-{stamp}.db")


def install(*, enable_browser: bool = True) -> None:
    prerequisites()
    first_install = not STATE.exists()
    ensure_update_changelog()
    ensure_env(enable_browser=enable_browser)
    if first_install:
        ensure_available_ports()
    compose("build")
    compose("up", "-d")
    if not health():
        compose("ps")
        raise SystemExit(
            "Installation started but health checks did not pass; run docker compose logs."
        )
    STATE.write_text(
        json.dumps(
            {
                "installed_at": time.time(),
                "version": "0.2.0-rc13",
                "build_commit": source_commit(),
                "images": snapshot_images(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Corvus is ready at {configured_url()}")


def update(*, enable_browser: bool = True) -> None:
    prerequisites()
    previous = snapshot_images()
    ensure_update_changelog()
    ensure_env(enable_browser=enable_browser)
    if previous:
        backup()
    # Persist recovery data before Docker can change tags or recreate containers.
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.is_file() else {}
    state.update({"update_started_at": time.time(), "rollback_images": previous})
    write_state(state)
    try:
        compose("build", "--pull")
        compose("up", "-d", "--force-recreate", "--remove-orphans")
        if not health():
            raise RuntimeError("Update health checks did not pass.")
    except (subprocess.CalledProcessError, RuntimeError, KeyboardInterrupt):
        state["failed_update_at"] = time.time()
        write_state(state)
        print("Update failed. Recovery images are recorded in .install-state.json; "
              "inspect docker compose logs and use the rollback command if needed.", file=sys.stderr)
        raise
    write_state({
        "updated_at": time.time(),
        "version": "0.2.0-rc13",
        "build_commit": source_commit(),
        "previous_images": previous,
        "images": snapshot_images(),
    })
    promote_pending_update_changelog()
    print("Update completed and health checks passed.")


def rollback() -> None:
    prerequisites()
    if not STATE.exists():
        raise SystemExit("No installer state is available for rollback.")
    state = json.loads(STATE.read_text(encoding="utf-8"))
    previous = state.get("rollback_images") or state.get("previous_images")
    if not isinstance(previous, dict) or not previous:
        raise SystemExit(
            "No previous image set is recorded. Restore a previous release tree instead."
        )
    ensure_update_changelog()
    ensure_env()
    for service, image_id in previous.items():
        if service not in ALL_SERVICES or not isinstance(image_id, str):
            raise SystemExit("Installer state contains an invalid rollback image record.")
        run(["docker", "image", "tag", image_id, IMAGE_TAGS[service]])
    compose("up", "-d", "--no-build", "--remove-orphans")
    if not health():
        raise SystemExit(
            "Rollback images started but did not become healthy; inspect docker compose logs."
        )
    print(
        "Previous application images restored. Restore the matching database backup if migrations changed."
    )


def status_command() -> None:
    prerequisites()
    compose("ps")
    if STATE.exists():
        print(STATE.read_text(encoding="utf-8"))


def write_state(state: dict[str, object]) -> None:
    temporary = STATE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE)


def main() -> None:
    parser = argparse.ArgumentParser(description="Install, update, or recover Corvus.")
    parser.add_argument("command", choices=("install", "update", "check-update", "rollback", "backup", "status"))
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Deprecated compatibility flag; the managed browser is always installed.",
    )
    parser.add_argument(
        "--repository-url",
        help=f"Git update source; defaults to {DEFAULT_REPOSITORY_URL}.",
    )
    parser.add_argument(
        "--skip-source-update",
        action="store_true",
        help="Rebuild the current checkout without fetching source changes.",
    )
    parser.add_argument(
        "--only-if-changed",
        action="store_true",
        help="Exit without rebuilding when the Git source is already current.",
    )
    args = parser.parse_args()
    if args.command == "check-update":
        update_source(args.repository_url, check_only=True)
        return
    if args.command == "update" and not args.skip_source_update:
        if update_source(args.repository_url):
            restart = [
                sys.executable,
                str(Path(__file__).resolve()),
                "update",
                "--skip-source-update",
            ]
            if args.browser:
                restart.append("--browser")
            run(restart)
            return
        metadata_current = env_values().get("OSINT_BUILD_COMMIT") == source_commit()
        if (args.only_if_changed and metadata_current
                and not pending_update_changelog_path().is_file()
                and not pending_update_range_path().is_file()):
            return
    if args.command == "install":
        install(enable_browser=args.browser)
    elif args.command == "update":
        update(enable_browser=args.browser)
    elif args.command == "rollback":
        rollback()
    elif args.command == "backup":
        backup()
    else:
        status_command()


if __name__ == "__main__":
    main()
