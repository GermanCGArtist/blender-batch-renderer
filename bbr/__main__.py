import sys

if len(sys.argv) > 1 and sys.argv[1] in ("add", "list", "run", "scan"):
    from .cli import main
else:
    from .gui.app import main

sys.exit(main())
