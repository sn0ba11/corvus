#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="${0:A:h}"
INSTALLED_LAUNCHER="$HOME/Library/Application Support/OSINT Workbench/app/installer/macos/launch-osint-workbench.sh"
if [[ -x "$INSTALLED_LAUNCHER" ]]; then
  exec "$INSTALLED_LAUNCHER" "$@"
fi
export OSINT_APP_ROOT="$SCRIPT_DIR"
export OSINT_INSTALL_ROOT="$SCRIPT_DIR/.macos-runtime"
exec "$SCRIPT_DIR/installer/macos/launch-osint-workbench.sh" "$@"
