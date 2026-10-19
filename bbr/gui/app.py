import sys
from pathlib import Path

ICON = Path(__file__).resolve().parent.parent / "assets" / "icon.png"


def main() -> int:
    try:
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        print("PySide6 is not installed. Run the setup script first (setup.command / setup.bat / setup.sh).")
        return 1
    from .. import APP_NAME
    from ..storage import InstanceLock

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("BlenderBatchRenderer")
    if ICON.exists():
        app.setWindowIcon(QIcon(str(ICON)))

    lock = InstanceLock()
    if not lock.acquire():
        QMessageBox.information(None, APP_NAME, f"{APP_NAME} is already running.\n\n"
                                "Only one window can work on the queue at a time.")
        return 0

    from .main_window import MainWindow
    w = MainWindow()
    w.show()
    try:
        return app.exec()
    finally:
        lock.release()
