#!/bin/bash
# Installs "LLM Explain.app" into /Applications (or ~/Applications) and starts it.
#   ./install_mac.sh             install
#   ./install_mac.sh --startup   also start it at login (a LaunchAgent)
#   ./install_mac.sh --no-startup   stop starting it at login
set -euo pipefail
cd "$(dirname "$0")"
AGENT="$HOME/Library/LaunchAgents/io.github.mcrolfey.llm-explain.plist"

if [ "${1:-}" = "--no-startup" ]; then
  launchctl bootout "gui/$(id -u)" "$AGENT" 2>/dev/null || true
  rm -f "$AGENT"
  echo "LLM Explain will no longer start at login."
  exit 0
fi

SRC="dist/LLM Explain.app"
[ -d "$SRC" ] || { echo "Build it first: ./build_mac.sh (missing $SRC)"; exit 1; }

DEST_DIR=/Applications
[ -w "$DEST_DIR" ] || { DEST_DIR="$HOME/Applications"; mkdir -p "$DEST_DIR"; }
DEST="$DEST_DIR/LLM Explain.app"

# Stop a running copy so the app can be replaced
pkill -f "LLM Explain.app/Contents/MacOS/LLM Explain" 2>/dev/null && sleep 1 || true
rm -rf "$DEST"
ditto "$SRC" "$DEST"
echo "Installed to $DEST"

if [ "${1:-}" = "--startup" ]; then
  mkdir -p "$(dirname "$AGENT")"
  cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>io.github.mcrolfey.llm-explain</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/open</string><string>-a</string><string>$DEST</string></array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
PLIST
  echo "Starts at login (undo with ./install_mac.sh --no-startup)."
fi

open "$DEST"
echo "Config: ~/Library/Application Support/LLM Explain/config.json"
