"""Build a portable updater from an exact committed revision, never the dirty tree."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> bytes:
    executable = shutil.which("git")
    if executable is None:
        raise SystemExit("Git is required to build the updater.")
    return subprocess.run(  # noqa: S603
        [executable, *args], cwd=ROOT, check=True, capture_output=True
    ).stdout


def archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name, payload in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            output.writestr(entry, payload)
    return buffer.getvalue()


def build(output: Path, revision: str = "HEAD") -> Path:
    commit = git("rev-parse", "--verify", f"{revision}^{{commit}}").decode().strip()
    version = tomllib.loads(git("show", f"{commit}:pyproject.toml").decode())["project"]["version"]
    launcher = git("show", f"{commit}:Update-Corvus.py")
    windows = git("show", f"{commit}:installer/windows/Update-CorvusLauncher.ps1")
    batch = git("show", f"{commit}:installer/windows/Update-Corvus.cmd")
    repair = git("show", f"{commit}:installer/windows/Repair-CorvusUpdate.cmd")
    guide = git("show", f"{commit}:docs/UPDATER.md")
    files = {
        "Update-Corvus.pyz": archive({"__main__.py": launcher}),
        "Update-CorvusLauncher.ps1": windows,
        "Update-Corvus.cmd": batch,
        "Repair-CorvusUpdate.cmd": repair,
        "UPDATER.md": guide,
    }
    licence_files = git("ls-tree", "-r", "--name-only", commit, "--", "LICENSES/").decode().splitlines()
    for name in ("LICENSE", "COPYRIGHT", "THIRD_PARTY_NOTICES.md", *licence_files):
        files[name] = git("show", f"{commit}:{name}")
    manifest = {"product": "Corvus Updater", "schema_version": 1, "commit": commit, "version": version,
                "files": {name: hashlib.sha256(payload).hexdigest()
                          for name, payload in sorted(files.items())}}
    files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"Corvus-Updater-{version}-{commit[:12]}.zip"
    payload = archive(files)
    destination.write_bytes(payload)
    destination.with_suffix(".zip.sha256").write_text(
        f"{hashlib.sha256(payload).hexdigest()}  {destination.name}\n", encoding="ascii"
    )
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "updater")
    args = parser.parse_args()
    print(build(args.output, args.revision))


if __name__ == "__main__":
    main()
