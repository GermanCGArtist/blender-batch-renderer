# Entry point of the built app (build_app.command / build_windows.bat).
# The launcher picks the newest installed app code (see bbr_launcher.py), then the app starts.
import sys

import bbr_launcher

bbr_launcher.activate()

from bbr.gui.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
