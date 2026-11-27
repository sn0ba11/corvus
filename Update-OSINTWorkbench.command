#!/bin/zsh
set -euo pipefail
INSTALLED_UPDATER="$HOME/Library/Application Support/OSINT Workbench/app/installer/macos/update-osint-workbench.sh"
if [[ -x "$INSTALLED_UPDATER" ]]; then
  exec "$INSTALLED_UPDATER" "$@"
fi
SCRIPT_DIR="${0:A:h}"
export OSINT_APP_ROOT="$SCRIPT_DIR"
export OSINT_INSTALL_ROOT="$SCRIPT_DIR/.macos-runtime"
exec "$SCRIPT_DIR/installer/macos/update-osint-workbench.sh" "$@"
