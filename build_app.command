#!/bin/bash
# Builds "Blender Batch Renderer.app" (macOS) with its own icon and Dock name.
# Unused parts of Qt are left out (see bbr.spec). The finished app is tested
# by starting it once in the background; if that fails, it is rebuilt with
# everything included instead. Re-run after updating the code.
cd "$(dirname "$0")" || exit 1
[ -x .venv/bin/python ] || ./setup.command || exit 1
APP="Blender Batch Renderer"
.venv/bin/python -m pip install --quiet "pyinstaller>=6.3" || { read -r -p "Installing PyInstaller failed. Press Enter"; exit 1; }

build() {
  rm -rf build dist
  .venv/bin/python -m PyInstaller --noconfirm --clean --log-level WARN bbr.spec
}

smoke_test() {
  # starts the app with an empty test queue and closes it after a moment
  local tmp; tmp=$(mktemp -d)
  BBR_SMOKE_TEST=1 BBR_API_PORT=0 BBR_DATA_DIR="$tmp" "dist/$APP.app/Contents/MacOS/$APP" > "$tmp/out.txt" 2>&1
  local rc=$?
  if [ $rc -ne 0 ] || ! grep -q "SMOKE TEST OK" "$tmp/out.txt" || grep -q "Traceback" "$tmp/out.txt"; then
    echo "--- app output ---"; tail -20 "$tmp/out.txt"; echo "------------------"
    rm -rf "$tmp"; return 1
  fi
  rm -rf "$tmp"
}

echo "Building the trimmed app…"
if build && smoke_test; then
  echo "Trimmed build works."
else
  echo "The trimmed build did not start - building with all of Qt included instead…"
  BBR_FULL_BUILD=1 build && smoke_test || { read -r -p "Build failed. Press Enter"; exit 1; }
fi

rm -rf "$APP.app"
mv "dist/$APP.app" .
rm -rf build dist
echo
echo "Built: $(pwd)/$APP.app  ($(du -sh "$APP.app" | cut -f1))"
open -R "$APP.app"
