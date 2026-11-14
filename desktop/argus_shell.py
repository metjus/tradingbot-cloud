"""ARGUS Windows desktop shell.

Hosts the terminal UI (served by the local engine API) in a native window
with a system-tray icon. The ENGINE RUNS AS A SEPARATE PROCESS: closing or
minimising the window never stops trading - only "Ukončiť ARGUS" from the
tray menu shuts the engine down.

Packaged (PyInstaller) build is ONE executable in two modes:
    ARGUS.exe             -> this GUI shell (spawns the engine below)
    ARGUS.exe --engine    -> the trading engine process

Dev run:    pip install -e ".[desktop]"
            python desktop/argus_shell.py
Package:    built automatically by .github/workflows/build-windows.yml;
            manual steps in desktop/BUILD-WINDOWS.md
"""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import time
import urllib.request

API = os.environ.get("ARGUS_API", "http://127.0.0.1:7421")
FROZEN = bool(getattr(sys, "frozen", False))


def run_engine() -> None:
    """Engine mode (``--engine``): run the trading engine in this process.

    Windowed builds have sys.stdout/stderr = None, so anything that writes
    to the console (uvicorn, warnings, tracebacks) would crash the process
    invisibly. Route stdio to a file first and capture any startup crash."""
    import traceback
    log_dir = os.path.dirname(log_path())
    os.makedirs(log_dir, exist_ok=True)
    stream = open(os.path.join(log_dir, "engine-stdio.log"), "a",
                  buffering=1, encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    try:
        import faulthandler
        faulthandler.enable(stream)
    except Exception:
        pass
    try:
        from argus.__main__ import main as engine_main
        engine_main()
    except SystemExit:
        raise
    except BaseException:
        stream.write("\n=== ENGINE CRASHED ===\n")
        traceback.print_exc(file=stream)
        stream.flush()
        raise


def engine_running() -> bool:
    try:
        urllib.request.urlopen(f"{API}/api/health", timeout=1.5)
        return True
    except Exception:
        return False


def engine_command() -> list[str]:
    if FROZEN:
        return [sys.executable, "--engine"]
    return [sys.executable, "-m", "argus"]


def start_engine() -> subprocess.Popen | None:
    if engine_running():
        return None
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(engine_command(), creationflags=flags)
    for _ in range(90):
        if engine_running():
            break
        if proc.poll() is not None:  # engine died during startup
            break
        time.sleep(1)
    return proc


def log_path() -> str:
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA", ""), "ARGUS", "logs", "argus.log")
    return os.path.join(os.environ.get("ARGUS_HOME", os.path.join(os.getcwd(), "data")),
                        "logs", "argus.log")


def run_shell() -> None:
    from PySide6.QtCore import Qt, QUrl
    from PySide6.QtGui import (QAction, QColor, QIcon, QLinearGradient, QPainter,
                               QPen, QPixmap)
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import (QApplication, QMainWindow, QMenu, QMessageBox,
                                   QSystemTrayIcon)

    def make_icon() -> QIcon:
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor("#11151F"))
        p.setPen(QPen(QColor("#242C40"), 2))
        p.drawRoundedRect(2, 2, 60, 60, 15, 15)
        grad = QLinearGradient(0, 0, 64, 64)
        grad.setColorAt(0, QColor("#5B8CFF"))
        grad.setColorAt(1, QColor("#8F7BFF"))
        pen = QPen()
        pen.setBrush(grad)
        pen.setWidth(3)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawArc(10, 14, 44, 36, 0, 180 * 16)
        p.drawArc(10, 14, 44, 36, 180 * 16, 180 * 16)
        p.setBrush(grad)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(27, 22, 10, 20, 3, 3)
        p.end()
        return QIcon(pm)

    class Shell(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("ARGUS — Autonomous Trading Intelligence")
            self.setWindowIcon(make_icon())
            self.resize(1440, 900)
            self.view = QWebEngineView()
            self.view.load(QUrl(f"{API}/app/"))
            self.setCentralWidget(self.view)

            self.tray = QSystemTrayIcon(make_icon(), self)
            menu = QMenu()
            show = QAction("Otvoriť ARGUS", self)
            show.triggered.connect(self.show_window)
            quit_ = QAction("Ukončiť ARGUS (zastaví engine)", self)
            quit_.triggered.connect(self.quit_all)
            menu.addAction(show)
            menu.addSeparator()
            menu.addAction(quit_)
            self.tray.setContextMenu(menu)
            self.tray.setToolTip("ARGUS beží — obchodný engine je aktívny")
            self.tray.activated.connect(
                lambda r: self.show_window() if r == QSystemTrayIcon.Trigger else None)
            self.tray.show()
            self._engine_proc: subprocess.Popen | None = None

        def show_window(self) -> None:
            self.showNormal()
            self.raise_()
            self.activateWindow()

        def closeEvent(self, event) -> None:  # minimise to tray, engine keeps running
            event.ignore()
            self.hide()
            self.tray.showMessage(
                "ARGUS", "Beží ďalej na pozadí. Ukončenie: pravý klik na ikonu.",
                QSystemTrayIcon.Information, 4000)

        def quit_all(self) -> None:
            if self._engine_proc is not None:
                self._engine_proc.terminate()
            QApplication.quit()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    shell = Shell()
    shell._engine_proc = start_engine()
    if not engine_running():
        QMessageBox.critical(
            None, "ARGUS — engine sa nespustil",
            "Obchodný engine sa nepodarilo spustiť.\n\n"
            f"Podrobnosti sú v logu:\n{log_path()}\n\n"
            "Skúste aplikáciu spustiť znova; ak problém pretrvá, "
            "pošlite obsah logu na analýzu.")
    shell.show()
    shell.view.reload()  # engine is up now; make sure the UI loaded
    sys.exit(app.exec())


def main() -> None:
    multiprocessing.freeze_support()
    if "--engine" in sys.argv:
        run_engine()
        return
    run_shell()


if __name__ == "__main__":
    main()
