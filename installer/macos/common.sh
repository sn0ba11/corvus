#!/bin/zsh
set -euo pipefail

PRODUCT_NAME="Corvus"
BUNDLE_ID="ie.osintworkbench.desktop"
INSTALL_ROOT="${OSINT_INSTALL_ROOT:-$HOME/Library/Application Support/OSINT Workbench}"
APP_ROOT="${OSINT_APP_ROOT:-$INSTALL_ROOT/app}"
LOG_ROOT="$INSTALL_ROOT/logs"
APP_BUNDLE="${OSINT_APP_BUNDLE:-$HOME/Applications/OSINT Workbench.app}"
UPDATE_COMMAND="$HOME/Applications/Corvus Update.command"
UNINSTALL_COMMAND="$HOME/Applications/Uninstall OSINT Workbench.command"
FACTORY_RESET_COMMAND="$HOME/Applications/Factory Reset OSINT Workbench.command"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/ie.osintworkbench.daily-update.plist"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
  log "ERROR: $*" >&2
  exit 1
}

require_macos() {
  [[ "$(uname -s)" == "Darwin" ]] || fail "This launcher supports macOS only."
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "$2"
}

require_python() {
  [[ -n "$PYTHON_BIN" ]] || fail "Python 3 is required. Install it with Homebrew or python.org, then retry."
  "$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit("Python 3.9 or newer is required by the macOS management scripts.")
PY
}

app_tree_available() {
  [[ -f "$APP_ROOT/docker-compose.yml" && -f "$APP_ROOT/scripts/installer.py" ]]
}

require_app_tree() {
  app_tree_available || fail "The installed application tree is missing or incomplete: $APP_ROOT"
}

validate_repository_url() {
  local value="$1"
  require_python
  "$PYTHON_BIN" - "$value" <<'PY'
import re
import sys

value = sys.argv[1]
valid = bool(
    re.fullmatch(r"(?:https?|ssh)://\S+", value)
    or re.fullmatch(r"[^@\s]+@[^:\s]+:.+", value)
)
if not valid:
    raise SystemExit("Repository URL must be HTTP(S), SSH, or Git's user@host:path form.")
PY
}

docker_ready() {
  docker info --format '{{.ServerVersion}}' >/dev/null 2>&1
}

start_and_wait_docker() {
  require_command docker "Docker Desktop is required. Install Docker Desktop for Mac and retry."
  docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required."
  if docker_ready; then
    return
  fi
  log "Starting Docker Desktop."
  open -gj -a Docker >/dev/null 2>&1 || fail "Docker Desktop could not be opened. Install it or start it manually."
  local deadline=$((SECONDS + 240))
  local next_update=$((SECONDS + 15))
  while (( SECONDS < deadline )); do
    if docker_ready; then
      log "Docker Desktop is ready."
      return
    fi
    if (( SECONDS >= next_update )); then
      log "Docker Desktop is still starting; setup will continue automatically."
      next_update=$((next_update + 15))
    fi
    sleep 3
  done
  fail "Docker Desktop did not become ready within four minutes."
}

configured_value() {
  local key="$1"
  local fallback="$2"
  local env_file="$APP_ROOT/.env"
  if [[ -f "$env_file" ]]; then
    local value
    value="$(awk -F= -v wanted="$key" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' "$env_file")"
    if [[ -n "$value" ]]; then
      printf '%s' "$value"
      return
    fi
  fi
  printf '%s' "$fallback"
}

workbench_url() {
  local port
  port="$(configured_value OSINT_PORT 8000)"
  [[ "$port" == <-> ]] || port=8000
  printf 'http://127.0.0.1:%s/' "$port"
}

browser_url() {
  local port
  port="$(configured_value OSINT_BROWSER_DESKTOP_PORT 6080)"
  [[ "$port" == <-> ]] || port=6080
  printf 'http://127.0.0.1:%s/vnc.html?autoconnect=true&resize=scale' "$port"
}

compose() {
  (cd "$APP_ROOT" && docker compose "$@")
}

wait_for_stack() {
  require_python
  (cd "$APP_ROOT" && "$PYTHON_BIN" -c 'from scripts.installer import health; raise SystemExit(0 if health(240) else 1)')
}

show_dialog() {
  local title="$1"
  local message="$2"
  /usr/bin/osascript - "$title" "$message" >/dev/null 2>&1 <<'APPLESCRIPT' || true
on run argv
  display dialog (item 2 of argv) with title (item 1 of argv) buttons {"OK"} default button "OK"
end run
APPLESCRIPT
}

unload_launch_agent() {
  if [[ -f "$LAUNCH_AGENT" ]]; then
    launchctl bootout "gui/$UID" "$LAUNCH_AGENT" >/dev/null 2>&1 || launchctl unload "$LAUNCH_AGENT" >/dev/null 2>&1 || true
  fi
}
