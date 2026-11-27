#!/usr/bin/env python3
"""Dependency-free Corvus updater entrypoint for Windows, macOS and Linux."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@contextlib.contextmanager
def update_lock(root: Path) -> Iterator[None]:
    """OS-owned lock: release automatically on exit, including process termination."""
    runtime = root / "runtime"
    runtime.mkdir(exist_ok=True)
    with (runtime / "corvus-update.lock").open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise SystemExit("Another Corvus updater is already running.") from error
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise SystemExit("Another Corvus updater is already running.") from error
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-dir", type=Path, default=ROOT,
                        help="Existing Corvus Git checkout (required when running the packaged updater).")
    parser.add_argument("--check", action="store_true", help="Fetch and report updates without deploying.")
    parser.add_argument("--only-if-changed", action="store_true", help="Skip rebuilding an unchanged release.")
    parser.add_argument("--repository-url", metavar="URL",
                        help="Git address to update from; saved as this install's update source "
                             "(a --check only tests it and saves nothing).")
    parser.add_argument("--show-source", action="store_true",
                        help="Print the current update source and exit without fetching.")
    parser.add_argument("--non-interactive", action="store_true", help="Disable Git credential prompts.")
    args = parser.parse_args()
    root = args.install_dir.expanduser().resolve()
    if not (root / "scripts" / "installer.py").is_file() or not (root / ".git").exists():
        parser.error("--install-dir must point to an existing Corvus Git checkout.")
    # scripts/installer.py is stdlib-only at import time, so it is the one source of the rules.
    spec = importlib.util.spec_from_file_location("corvus_installer", root / "scripts" / "installer.py")
    if spec is None or spec.loader is None:
        parser.error("scripts/installer.py could not be loaded.")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    if args.show_source:
        print(installer.current_update_source())
        return 0
    if args.repository_url is not None:
        args.repository_url = args.repository_url.strip()
        if not installer.repository_url_valid(args.repository_url):
            parser.error("--repository-url must be an HTTP(S) or SSH URL, or Git's user@host:path form.")
    environment = os.environ.copy()
    if args.non_interactive:
        environment.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
    if os.name == "nt" and not args.check:
        powershell = shutil.which("powershell.exe")
        if not powershell:
            parser.error("Windows PowerShell is required for the native Windows update workflow.")
        command = [powershell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-File", str(root / "Update-Corvus.ps1")]
        if args.repository_url:
            command += ["-RepositoryUrl", args.repository_url]
        if args.non_interactive:
            command.append("-NonInteractive")
        if args.only_if_changed:
            command.append("-OnlyIfChanged")
    else:
        command = [sys.executable, str(root / "scripts" / "installer.py"),
                   "check-update" if args.check else "update"]
        if args.repository_url:
            command += ["--repository-url", args.repository_url]
        if args.only_if_changed and not args.check:
            command.append("--only-if-changed")
    with update_lock(root):
        return subprocess.run(command, cwd=root, env=environment, check=False).returncode  # noqa: S603


if __name__ == "__main__":
    raise SystemExit(main())
