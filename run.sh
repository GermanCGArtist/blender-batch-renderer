#!/bin/bash
# Starts Blender Batch Renderer (Linux).
cd "$(dirname "$0")" || exit 1
[ -x .venv/bin/python ] || ./setup.sh || exit 1
exec .venv/bin/python -m bbr "$@"
