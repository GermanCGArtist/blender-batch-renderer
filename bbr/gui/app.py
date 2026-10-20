import os
import sys
from pathlib import Path

ICON = Path(__file__).resolve().parent.parent / "assets" / "icon.png"
QUEUE_EXT = ".bbrqueue"


def _queue_args(argv):
    return [a for a in argv[1:] if a.lower().endswith(QUEUE_EXT) and os.path.isfile(a)]


def main() -> int:
    try:
        from PySide6.QtCore import QEvent, QTimer
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        print("PySide6 is not installed. Run the setup script first (setup.command / setup.bat / setup.sh).")
        return 1
    from .. import APP_NAME
    from ..models import AppSettings
    from ..storage import InstanceLock

    class App(QApplication):
        """Receives macOS 'open this file' events (double-click on a .bbrqueue)."""

        def __init__(self, argv):
            super().__init__(argv)
            self.pending = []
            self.window = None

        def event(self, e):
            if e.type() == QEvent.Type.FileOpen:
                path = e.file()
                if path.lower().endswith(QUEUE_EXT):
                    if self.window is not None:
                        self.window.open_queue_file(path)
                    else:
                        self.pending.append(path)
                return True
            return super().event(e)

    app = App(sys.argv)
    # Python's garbage collector normally runs in whichever thread happens to allocate memory.
    # With rendering on several computers many background threads are busy, and a Qt object
    # collected there is deleted later on the main thread – which crashed the app in v0.4.
    # So the collector runs only here, on the main thread, every two seconds.
    import gc
    gc.disable()
    gc_timer = QTimer()
    gc_timer.timeout.connect(lambda: gc.collect())
    gc_timer.start(2000)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("BlenderBatchRenderer")
    if ICON.exists():
        app.setWindowIcon(QIcon(str(ICON)))

    files = _queue_args(sys.argv)
    lock = InstanceLock()
    if not lock.acquire():
        # Another copy is running (Windows/Linux double-click): hand the file over to it.
        if files:
            from ..api import forward_to_running_app
            if forward_to_running_app(AppSettings.load(), files[0]):
                return 0
        QMessageBox.information(None, APP_NAME, f"{APP_NAME} is already running.\n\n"
                                "Only one window can work on the queue at a time.")
        return 0

    from .main_window import MainWindow
    smoke = os.environ.get("BBR_SMOKE_TEST") == "1"   # used by build_app.command
    w = MainWindow(first_run=not smoke)
    app.window = w
    w.show()
    for path in files + app.pending:
        QTimer.singleShot(0, lambda p=path: w.open_queue_file(p))
    app.pending = []

    def started():
        try:
            import bbr_launcher
            bbr_launcher.mark_started()
            if bbr_launcher.ACTIVE.get("fallback_note"):
                w._add_message("warn", bbr_launcher.ACTIVE["fallback_note"])
        except ImportError:
            pass
    QTimer.singleShot(5000, started)
    if smoke:
        def done():
            # exercise the parts that load extra code: charts, thumbnail, Excel report, API
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
            from .. import updater           # needs ssl/certifi in the built app
            updater.current()
            updater._ctx()
            j = Job(blend_path="smoke.blend", folder=os.path.join(os.environ.get("BBR_DATA_DIR", "."), "smoke"),
                    name="smoke")
            j.frames = {str(i): {"status": "done", "render_s": 1.0 + i, "size": 1000} for i in range(1, 4)}
            ok = write_xlsx(j)
            api_ok = w.api.running
            qf = os.path.join(os.environ.get("BBR_DATA_DIR", "."), "smoke" + QUEUE_EXT)
            w.queue.save_file(qf)
            result = "SMOKE TEST OK" if ok and api_ok else f"SMOKE TEST: report={ok} api={api_ok} {w.api.error}"
            print(result, flush=True)
            try:      # a windowed Windows .exe has no console to print to
                with open(os.path.join(os.environ.get("BBR_DATA_DIR", "."), "smoke_result.txt"), "w") as fh:
                    fh.write(result)
            except OSError:
                pass
            w.api.stop()
            app.quit()
        QTimer.singleShot(1500, done)
    try:
        return app.exec()
    finally:
        lock.release()
