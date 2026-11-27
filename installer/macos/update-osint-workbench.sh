#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
source "$SCRIPT_DIR/common.sh"

SCHEDULED=false
while (( $# )); do
  case "$1" in
    --scheduled) SCHEDULED=true ;;
    *) fail "Unknown option: $1" ;;
  esac
  shift
done

require_macos
require_app_tree
require_python
mkdir -p "$LOG_ROOT"

if [[ "$SCHEDULED" == true ]]; then
  exec >>"$LOG_ROOT/scheduled-update.log" 2>&1
  export GIT_TERMINAL_PROMPT=0
  export GCM_INTERACTIVE=never
else
  exec > >(tee -a "$LOG_ROOT/manual-update.log") 2>&1
fi

log "Checking Corvus for an update."
start_and_wait_docker
if (cd "$APP_ROOT" && "$PYTHON_BIN" scripts/installer.py update --only-if-changed); then
  log "Update check completed."
else
  status=$?
  log "Update failed with exit code $status. Existing containers and rollback metadata were preserved."
  if [[ "$SCHEDULED" != true ]]; then
    show_dialog "Corvus update failed" "The update did not complete. Review $LOG_ROOT/manual-update.log."
  fi
  exit "$status"
fi
