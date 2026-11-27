#!/bin/zsh
set -euo pipefail
INSTALLED_UNINSTALLER="$HOME/Library/Application Support/OSINT Workbench/app/installer/macos/uninstall-osint-workbench.sh"
if [[ -x "$INSTALLED_UNINSTALLER" ]]; then
  exec "$INSTALLED_UNINSTALLER" "$@"
fi
SCRIPT_DIR="${0:A:h}"
exec "$SCRIPT_DIR/installer/macos/uninstall-osint-workbench.sh" "$@"
