#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
source "$SCRIPT_DIR/common.sh"

require_macos
mkdir -p "$LOG_ROOT"
exec >>"$LOG_ROOT/launcher.log" 2>&1
trap 'status=$?; show_dialog "Corvus" "Corvus could not start. Review the launcher log under Library/Application Support/OSINT Workbench/logs."; exit $status' ZERR

log "Launching Corvus."
require_app_tree
start_and_wait_docker

log "Starting the Compose stack."
if ! compose up -d --remove-orphans; then
  show_dialog "Corvus" "The container stack could not be started. Review $LOG_ROOT/launcher.log."
  exit 1
fi
if ! wait_for_stack; then
  compose ps || true
  show_dialog "Corvus" "One or more services did not become healthy. Review $LOG_ROOT/launcher.log."
  exit 1
fi

url="$(workbench_url)"
log "Opening $url"
open "$url"
