#!/bin/bash
# Starts Blender Batch Renderer (macOS). Double-click in Finder.
cd "$(dirname "$0")" || exit 1
[ -x .venv/bin/python ] || ./setup.command || exit 1
exec .venv/bin/python -m bbr "$@"
