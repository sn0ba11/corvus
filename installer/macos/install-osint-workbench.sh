#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
SOURCE_ROOT="${SCRIPT_DIR:h:h}"
source "$SCRIPT_DIR/common.sh"

NO_LAUNCH=false
NO_DAILY_UPDATE=false
REPLACE=false
REPOSITORY_URL="${OSINT_REPOSITORY_URL:-https://github.com/sn0ba11/corvus.git}"

create_app_icon() {
  local source_svg="$APP_ROOT/app/static/corvus-mark.svg"
  local resources="$APP_BUNDLE/Contents/Resources"
  local iconset="$resources/OSINTWorkbench.iconset"
  local preview_dir preview_png

  if [[ ! -f "$source_svg" ]] || ! command -v qlmanage >/dev/null 2>&1 || \
     ! command -v sips >/dev/null 2>&1 || ! command -v iconutil >/dev/null 2>&1; then
    log "Brand icon tools are unavailable; the launcher will use the standard macOS application icon."
    return 0
  fi

  mkdir -p "$resources"
  preview_dir="$(mktemp -d -t osint-workbench-icon.XXXXXX)"
  if ! qlmanage -t -s 1024 -o "$preview_dir" "$source_svg" >/dev/null 2>&1; then
    rm -rf "$preview_dir"
    log "The Corvus SVG could not be rendered for the macOS launcher icon."
    return 0
  fi
  preview_png="$(find "$preview_dir" -type f -name '*.png' -print -quit)"
  if [[ -z "$preview_png" ]]; then
    rm -rf "$preview_dir"
    log "macOS did not return a rendered branding preview; using the standard application icon."
    return 0
  fi

  rm -rf "$iconset"
  mkdir -p "$iconset"
  for definition in \
    '16 icon_16x16.png' '32 icon_16x16@2x.png' \
    '32 icon_32x32.png' '64 icon_32x32@2x.png' \
    '128 icon_128x128.png' '256 icon_128x128@2x.png' \
    '256 icon_256x256.png' '512 icon_256x256@2x.png' \
    '512 icon_512x512.png' '1024 icon_512x512@2x.png'; do
    local pixels="${definition%% *}"
    local filename="${definition#* }"
    sips -z "$pixels" "$pixels" "$preview_png" --out "$iconset/$filename" >/dev/null
  done
  iconutil -c icns "$iconset" -o "$resources/OSINTWorkbench.icns"
  rm -rf "$iconset" "$preview_dir"
}

while (( $# )); do
  case "$1" in
    --no-launch) NO_LAUNCH=true ;;
    --no-daily-update) NO_DAILY_UPDATE=true ;;
    --replace) REPLACE=true ;;
    --repository-url)
      shift
      (( $# )) || fail "--repository-url requires a value."
      REPOSITORY_URL="$1"
      ;;
    *) fail "Unknown option: $1" ;;
  esac
  shift
done

require_macos
if [[ "${SOURCE_ROOT:A}" == "${APP_ROOT:A}" ]]; then
  fail "The installer cannot replace its own installed tree. Use Corvus Update.command."
fi
require_command git "Git is required. Install Xcode Command Line Tools with: xcode-select --install"
require_command rsync "The macOS rsync utility is required."
require_python
validate_repository_url "$REPOSITORY_URL"
start_and_wait_docker

mkdir -p "$INSTALL_ROOT" "$LOG_ROOT" "$HOME/Applications" "$HOME/Library/LaunchAgents"

if [[ -d "$APP_ROOT" ]]; then
  if [[ "$REPLACE" != true ]]; then
    fail "Corvus is already installed at $APP_ROOT. Use the updater, or rerun with --replace."
  fi
  log "Replacing the installed application while preserving its local .env secrets."
  preserved_env=""
  if [[ -f "$APP_ROOT/.env" ]]; then
    preserved_env="$(mktemp -t osint-workbench-env.XXXXXX)"
    cp "$APP_ROOT/.env" "$preserved_env"
  fi
  (cd "$APP_ROOT" && docker compose down --remove-orphans >/dev/null 2>&1) || true
  rm -rf "$APP_ROOT"
  mkdir -p "$APP_ROOT"
else
  preserved_env=""
fi

if [[ -f "$SOURCE_ROOT/osint-workbench.bundle" ]]; then
  log "Installing from the embedded updateable Git bundle."
  rm -rf "$APP_ROOT"
  git clone --branch main --single-branch "$SOURCE_ROOT/osint-workbench.bundle" "$APP_ROOT"
  git -C "$APP_ROOT" remote set-url origin "$REPOSITORY_URL"
else
  log "Installing the checked release tree into Application Support."
  mkdir -p "$APP_ROOT"
  rsync -a --delete \
    --exclude '.env' \
    --exclude '.venv/' \
    --exclude '.pytest_cache/' \
    --exclude '.mypy_cache/' \
    --exclude '.ruff_cache/' \
    --exclude '__pycache__/' \
    --exclude 'dist/' \
    --exclude 'data/*' \
    --exclude 'reports/*' \
    --exclude 'tool_output/*' \
    --exclude 'sessions/*' \
    "$SOURCE_ROOT/" "$APP_ROOT/"
fi

mkdir -p "$APP_ROOT/data" "$APP_ROOT/reports" "$APP_ROOT/tool_output" "$APP_ROOT/sessions/instaloader"
touch \
  "$APP_ROOT/data/.gitkeep" \
  "$APP_ROOT/reports/.gitkeep" \
  "$APP_ROOT/tool_output/.gitkeep" \
  "$APP_ROOT/sessions/.gitkeep" \
  "$APP_ROOT/sessions/instaloader/.gitkeep"
if [[ -n "$preserved_env" && -f "$preserved_env" ]]; then
  mv "$preserved_env" "$APP_ROOT/.env"
fi

log "Building the pinned containers and waiting for all service health checks."
(cd "$APP_ROOT" && "$PYTHON_BIN" scripts/installer.py install)

mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"
create_app_icon
cat > "$APP_BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDisplayName</key><string>Corvus</string>
  <key>CFBundleExecutable</key><string>OSINTWorkbench</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleIconFile</key><string>OSINTWorkbench</string>
  <key>CFBundleName</key><string>Corvus</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
cat > "$APP_BUNDLE/Contents/MacOS/OSINTWorkbench" <<LAUNCHER
#!/bin/zsh
exec "$APP_ROOT/installer/macos/launch-osint-workbench.sh" "\$@"
LAUNCHER
chmod 0755 "$APP_BUNDLE/Contents/MacOS/OSINTWorkbench"

cat > "$UPDATE_COMMAND" <<COMMAND
#!/bin/zsh
exec "$APP_ROOT/installer/macos/update-osint-workbench.sh" "\$@"
COMMAND
cat > "$UNINSTALL_COMMAND" <<COMMAND
#!/bin/zsh
exec "$APP_ROOT/installer/macos/uninstall-osint-workbench.sh" "\$@"
COMMAND
cat > "$FACTORY_RESET_COMMAND" <<COMMAND
#!/bin/zsh
exec "$APP_ROOT/installer/macos/uninstall-osint-workbench.sh" --purge "\$@"
COMMAND
chmod 0755 "$UPDATE_COMMAND" "$UNINSTALL_COMMAND" "$FACTORY_RESET_COMMAND"

if [[ "$NO_DAILY_UPDATE" != true ]]; then
  escaped_update="$($PYTHON_BIN -c 'import html,sys; print(html.escape(sys.argv[1], quote=True))' "$APP_ROOT/installer/macos/update-osint-workbench.sh")"
  escaped_stdout="$($PYTHON_BIN -c 'import html,sys; print(html.escape(sys.argv[1], quote=True))' "$LOG_ROOT/scheduled-update.log")"
  cat > "$LAUNCH_AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ie.osintworkbench.daily-update</string>
  <key>ProgramArguments</key>
  <array><string>/bin/zsh</string><string>$escaped_update</string><string>--scheduled</string></array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer></dict>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$escaped_stdout</string>
  <key>StandardErrorPath</key><string>$escaped_stdout</string>
</dict>
</plist>
PLIST
  unload_launch_agent
  launchctl bootstrap "gui/$UID" "$LAUNCH_AGENT" >/dev/null 2>&1 || launchctl load "$LAUNCH_AGENT"
  log "Registered a promptless daily update check at 04:00."
fi

log "Installed application: $APP_BUNDLE"
log "Application data and logs: $INSTALL_ROOT"
log "Workbench URL: $(workbench_url)"
log "Sock-browser URL: $(browser_url)"

if [[ "$NO_LAUNCH" != true ]]; then
  open "$APP_BUNDLE"
fi
