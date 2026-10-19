import sys


def main() -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("PySide6 is not installed. Run the setup script first (setup.command / setup.bat / setup.sh).")
        return 1
    from .. import APP_NAME
    from .main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("BlenderBatchRenderer")
    w = MainWindow()
    w.show()
    return app.exec()
