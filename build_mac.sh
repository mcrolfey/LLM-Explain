#!/bin/bash
# Builds "dist/LLM Explain.app" (runs without Python installed).
#   ./build_mac.sh
# Needs Python 3.10+ with Tk (python.org installer, Homebrew python-tk, or conda).
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt pyinstaller
[ -f icon.icns ] && [ -f icon_menubar.png ] || .venv/bin/python make_icon.py

.venv/bin/python -m PyInstaller --noconfirm --clean --windowed \
  --name "LLM Explain" --icon icon.icns \
  --osx-bundle-identifier io.github.mcrolfey.llm-explain \
  --add-data "icon_menubar.png:." --add-data "config.json:." \
  --hidden-import mac_support --hidden-import HIServices \
  --hidden-import keyring.backends.macOS --hidden-import anthropic \
  llm_explain.py

APP="dist/LLM Explain.app"
# Menu bar app: no Dock icon. Then re-sign, since editing Info.plist breaks the signature.
.venv/bin/python - "$APP/Contents/Info.plist" <<'PY'
import plistlib, sys
p = sys.argv[1]
with open(p, "rb") as f:
    info = plistlib.load(f)
info["LSUIElement"] = True
info["NSHumanReadableCopyright"] = "LLM Explain"
info["LSMinimumSystemVersion"] = "11.0"
with open(p, "wb") as f:
    plistlib.dump(info, f)
PY
codesign --force --deep --sign - "$APP"
# Stable designated requirement: macOS ties the Accessibility permission to it, so the
# permission survives rebuilds (an ad-hoc signature alone changes with every build).
# Trade-off: any app signed with this bundle identifier would match it.
codesign --force --sign - \
  -r='designated => identifier "io.github.mcrolfey.llm-explain"' "$APP"
codesign --verify --deep --strict "$APP"

"$APP/Contents/MacOS/LLM Explain" --selftest dist/selftest.txt
cat dist/selftest.txt
if grep -q FAIL dist/selftest.txt; then echo "Self-test failed."; exit 1; fi
echo
echo "Built: $PWD/$APP"
echo "Install it with: ./install_mac.sh   (add --startup to start it at login)"
