"""Regression: the engine must start without a console.

pythonw.exe and the windowed EXE run with sys.stdout/sys.stderr = None.
uvicorn's default logging config crashed on that (blank 'connection
refused' window on Windows). Start the real engine in a subprocess with
no stdio and require the API to come up.
"""

import os
import subprocess
import sys
import time
import urllib.request

PORT = 7641

LAUNCH = (
    "import sys, runpy\n"
    "sys.stdout = None\n"
    "sys.stderr = None\n"
    "runpy.run_module('argus', run_name='__main__')\n"
)


def test_engine_starts_without_console(tmp_path):
    env = dict(os.environ,
               ARGUS_HOME=str(tmp_path), ARGUS_API_PORT=str(PORT),
               ARGUS_CRYPTO_SYMBOLS="[]", ARGUS_NEWS_ENABLED="false",
               ARGUS_ML_TRAIN_ENABLED="false")
    proc = subprocess.Popen([sys.executable, "-c", LAUNCH], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ok = False
        for _ in range(60):
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health",
                                            timeout=1) as r:
                    ok = r.status == 200
                    break
            except Exception:
                time.sleep(0.5)
        log = tmp_path / "logs" / "argus.log"
        assert ok, "engine did not start without console; log:\n" + (
            log.read_text() if log.exists() else "<no log>")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
