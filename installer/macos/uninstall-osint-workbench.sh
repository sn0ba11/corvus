#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
source "$SCRIPT_DIR/common.sh"

PURGE=false
ASSUME_YES=false
while (( $# )); do
  case "$1" in
    --purge) PURGE=true ;;
    --yes) ASSUME_YES=true ;;
    *) fail "Unknown option: $1" ;;
  esac
  shift
done

require_macos

if [[ "$ASSUME_YES" != true ]]; then
  if [[ "$PURGE" == true ]]; then
    printf 'This will remove Corvus, all three Docker data volumes, browser sessions and built images.\n'
    printf 'Type FULL FACTORY RESET to continue: '
    read -r confirmation
    [[ "$confirmation" == "FULL FACTORY RESET" ]] || fail "Factory reset cancelled."
  else
    printf 'Remove Corvus while preserving Docker data volumes and browser sessions? [y/N] '
    read -r confirmation
    [[ "$confirmation" == [yY] ]] || fail "Uninstall cancelled."
  fi
fi

log "Removing the daily update LaunchAgent."
unload_launch_agent
rm -f "$LAUNCH_AGENT"

if [[ -f "$APP_ROOT/docker-compose.yml" ]] && command -v docker >/dev/null 2>&1; then
  start_and_wait_docker
  if [[ "$PURGE" == true ]]; then
    log "Stopping the stack and deleting project volumes and images."
    (cd "$APP_ROOT" && docker compose down --remove-orphans --volumes --rmi all)
    docker volume rm \
      osint-workbench_workbench-data \
      osint-workbench_browser-profile \
      osint-workbench_browser-evidence >/dev/null 2>&1 || true
  else
    log "Stopping the stack while preserving Docker data volumes."
    (cd "$APP_ROOT" && docker compose down --remove-orphans)
  fi
elif [[ -f "$APP_ROOT/docker-compose.yml" ]]; then
  fail "Docker Desktop is required to stop and remove the installed Corvus resources. Reinstall or start Docker Desktop, then retry."
fi

rm -rf "$APP_BUNDLE"
rm -f "$UPDATE_COMMAND" "$UNINSTALL_COMMAND" "$FACTORY_RESET_COMMAND"
rm -rf "$INSTALL_ROOT"

if [[ "$PURGE" == true ]]; then
  log "Corvus was fully reset."
else
  log "Corvus was removed. Docker data volumes were preserved for a later reinstall."
fi
