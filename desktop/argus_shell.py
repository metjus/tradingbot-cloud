"""ARGUS Windows desktop shell.

Hosts the terminal UI (served by the local engine API) in a native window
with a system-tray icon. The ENGINE RUNS AS A SEPARATE PROCESS: closing or
minimising the window never stops trading - only "Ukončiť ARGUS" from the
tray menu shuts the engine down.

Run:        pip install PySide6
            python desktop/argus_shell.py
Package:    see desktop/BUILD-WINDOWS.md
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QAction, QIcon, QPainter, QPixmap, QColor, QLinearGradient, QPen
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QSystemTrayIcon

API = os.environ.get("ARGUS_API", "http://127.0.0.1:7421")


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


def engine_running() -> bool:
    try:
        urllib.request.urlopen(f"{API}/api/health", timeout=1.5)
        return True
    except Exception:
        return False


def start_engine() -> subprocess.Popen | None:
    if engine_running():
        return None
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen([sys.executable, "-m", "argus"], creationflags=flags)
    for _ in range(60):
        if engine_running():
            break
        time.sleep(1)
    return proc


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
        self.tray.showMessage("ARGUS", "Beží ďalej na pozadí. Ukončenie: pravý klik na ikonu.",
                              QSystemTrayIcon.Information, 4000)

    def quit_all(self) -> None:
        if self._engine_proc is not None:
            self._engine_proc.terminate()
        QApplication.quit()


def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    shell = Shell()
    shell._engine_proc = start_engine()
    shell.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
