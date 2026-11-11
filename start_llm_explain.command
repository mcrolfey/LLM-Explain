#!/bin/bash
# Runs LLM Explain from source on macOS (double-click in Finder, or run in Terminal).
# The first run creates .venv and installs the requirements. Quit from the menu bar icon.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv || { echo "Python 3.10+ with Tk is needed (python.org installer)."; exit 1; }
  .venv/bin/python -m pip install -q -r requirements.txt || exit 1
fi
[ -f icon_menubar.png ] || .venv/bin/python make_icon.py
nohup .venv/bin/python llm_explain.py >/dev/null 2>&1 &
echo "LLM Explain is running. Look for its icon in the menu bar."
