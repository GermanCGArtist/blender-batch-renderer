#!/bin/bash
# One-time setup on macOS: creates a private Python environment in .venv and installs the packages.
cd "$(dirname "$0")" || exit 1
PY=""
for c in /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY=$(command -v "$c"); break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.9 or newer was not found. Install it from https://www.python.org/downloads/ and run this again."
  read -r -p "Press Enter to close"; exit 1
fi
echo "Using $PY ($("$PY" --version))"
# start from a clean environment (also removes the old full PySide6 if present)
rm -rf .venv
"$PY" -m venv .venv || { read -r -p "Creating the environment failed. Press Enter"; exit 1; }
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt || { read -r -p "Installing packages failed. Press Enter"; exit 1; }
echo
echo "Setup finished. Start the app with run.command"
