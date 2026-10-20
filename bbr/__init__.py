"""Blender Batch Renderer - queue, resume and monitor Blender renders."""

__version__ = "1.1.0-alpha.nodes"
PROTOCOL = 2          # worker <-> coordinator protocol; both must match
SHELL_REQUIRED = 1    # the built app (Python, Qt, libraries) this code needs; see bbr_launcher.py
APP_NAME = "Blender Batch Renderer"
