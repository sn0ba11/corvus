#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h}"
VERSION="${1:-0.1.0-dev}"
OUTPUT_ROOT="$ROOT/dist/release"
PACKAGE_NAME="OSINT-Workbench-$VERSION-macOS"
ARCHIVE="$OUTPUT_ROOT/$PACKAGE_NAME.zip"
CHECKSUM="$ARCHIVE.sha256"
MANIFEST="$OUTPUT_ROOT/$PACKAGE_NAME.manifest.json"

command -v git >/dev/null 2>&1 || { print -u2 'Git is required.'; exit 1; }
command -v zip >/dev/null 2>&1 || { print -u2 'zip is required.'; exit 1; }
command -v python3 >/dev/null 2>&1 || { print -u2 'Python 3 is required.'; exit 1; }
if ! command -v shasum >/dev/null 2>&1 && ! command -v sha256sum >/dev/null 2>&1; then
  print -u2 'shasum or sha256sum is required.'
  exit 1
fi
python3 - "$VERSION" <<'PY'
import re
import sys

if re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,63}", sys.argv[1]) is None:
    raise SystemExit("Version must contain only letters, numbers, dots, underscores, or hyphens.")
PY

cd "$ROOT"
[[ -z "$(git status --porcelain --untracked-files=normal)" ]] || {
  print -u2 'The Git tree must be clean before building a release package.'
  exit 1
}

commit="$(git rev-parse HEAD)"
temp_root="$(mktemp -d -t osint-workbench-macos.XXXXXX)"
trap 'rm -rf "$temp_root"' EXIT
stage="$temp_root/$PACKAGE_NAME"
mkdir -p "$stage" "$OUTPUT_ROOT"

git archive --format=tar HEAD | tar -xf - -C "$stage"
git bundle create "$stage/osint-workbench.bundle" main
rm -rf "$stage/.github" "$stage/.pytest_cache" "$stage/.mypy_cache" "$stage/.ruff_cache"
find "$stage" -type d -name '__pycache__' -prune -exec rm -rf {} +
chmod 0755 \
  "$stage/Install-OSINTWorkbench.command" \
  "$stage/OSINTWorkbench.command" \
  "$stage/Update-OSINTWorkbench.command" \
  "$stage/Uninstall-OSINTWorkbench.command" \
  "$stage/installer/macos/"*.sh

rm -f "$ARCHIVE" "$CHECKSUM" "$MANIFEST"
(
  cd "$temp_root"
  zip -qry "$ARCHIVE" "$PACKAGE_NAME"
)
if command -v shasum >/dev/null 2>&1; then
  digest="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
else
  digest="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
fi
printf '%s  %s\n' "$digest" "$(basename "$ARCHIVE")" > "$CHECKSUM"
python3 - "$MANIFEST" "$VERSION" "$commit" "$ARCHIVE" "$digest" <<'PY'
import json
import pathlib
import sys

manifest, version, commit, archive, digest = sys.argv[1:]
payload = {
    "product": "Corvus",
    "version": version,
    "commit": commit,
    "platform": "macOS (Docker Desktop with Linux containers)",
    "archive": pathlib.Path(archive).name,
    "sha256": digest,
    "installer": "Install-OSINTWorkbench.command",
    "signed": False,
}
pathlib.Path(manifest).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY

print "Created: $ARCHIVE"
print "SHA-256: $digest"
print 'The package is unsigned; macOS Gatekeeper acceptance must be tested before release.'
