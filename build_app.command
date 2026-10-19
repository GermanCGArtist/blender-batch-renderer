#!/bin/bash
# Builds "Blender Batch Renderer.app" (macOS) with its own icon and Dock name.
# Run setup.command first. The finished app appears next to this script;
# drag it into /Applications if you like. Re-run after updating the code.
cd "$(dirname "$0")" || exit 1
[ -x .venv/bin/python ] || ./setup.command || exit 1
APP="Blender Batch Renderer"
.venv/bin/python -m pip install --quiet "pyinstaller>=6.3" || { read -r -p "Installing PyInstaller failed. Press Enter"; exit 1; }
rm -rf build dist
.venv/bin/python -m PyInstaller --noconfirm --clean --windowed \
  --name "$APP" \
  --icon bbr/assets/icon.icns \
  --osx-bundle-identifier io.github.germancgartist.blenderbatchrenderer \
  --add-data "bbr/blender_scripts:bbr/blender_scripts" \
  --add-data "bbr/assets:bbr/assets" \
  --exclude-module tkinter \
  bbr_app.py || { read -r -p "Build failed. Press Enter"; exit 1; }
rm -rf "$APP.app"
mv "dist/$APP.app" .
rm -rf build dist "$APP.spec"
echo
echo "Built: $(pwd)/$APP.app"
open -R "$APP.app"
