import os
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
    smoke = os.environ.get("BBR_SMOKE_TEST") == "1"   # used by build_app.command
    w = MainWindow(first_run=not smoke)
    w.show()
    if smoke:
        from PySide6.QtCore import QTimer

        def done():
            # exercise the parts that load extra code: charts, thumbnail, Excel report
            w._load_selected()
            w._tick()
            w.chart_time.set_data([1, 2, 3, 5], [[1.0, 2.5, 1.5, 2.0]])
            w.chart_mem.set_data([1, 2, 3, 5], [[100.0, 120.0, None, 90.0], [300.0, 310.0, 320.0, 330.0]])
            w.thumb.show_file(str(ICON))
            w.chart_time.grab()          # forces a paint of both charts right now
            w.chart_mem.grab()
            w.repaint()
            from ..models import Job
            from ..reports import write_xlsx
            import psutil  # noqa: F401
            j = Job(blend_path="smoke.blend", folder=os.path.join(os.environ.get("BBR_DATA_DIR", "."), "smoke"),
                    name="smoke")
            j.frames = {str(i): {"status": "done", "render_s": 1.0 + i, "size": 1000} for i in range(1, 4)}
            ok = write_xlsx(j)
            print("SMOKE TEST OK" if ok else "SMOKE TEST: report failed", flush=True)
            app.quit()
        QTimer.singleShot(1500, done)
    try:
        return app.exec()
    finally:
        lock.release()
