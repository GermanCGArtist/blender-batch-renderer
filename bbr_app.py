# Entry point used when building the stand-alone app (build_app.command).
import sys

from bbr.gui.app import main

if __name__ == "__main__":
    sys.exit(main())
