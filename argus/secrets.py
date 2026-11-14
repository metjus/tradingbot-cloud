"""Local secret store for API keys and cloud tokens (§22).

Windows: values are encrypted with DPAPI (CryptProtectData), bound to the
current Windows user - the file is useless on another account or PC.
Other OS (development): stored base64 in a file with 0600 permissions and
clearly marked as NOT encrypted.

Secrets are never returned by the API (only whether they are set) and are
never sent to the cloud/mobile layer.
"""

from __future__ import annotations

import base64
import json
import os
import platform
from pathlib import Path

_IS_WINDOWS = platform.system() == "Windows"


if _IS_WINDOWS:  # pragma: no cover - exercised on Windows CI
    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    def _blob(data: bytes) -> _DATA_BLOB:
        buf = ctypes.create_string_buffer(data, len(data))
        return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _protect(data: bytes) -> bytes:
        out = _DATA_BLOB()
        if not _crypt32.CryptProtectData(ctypes.byref(_blob(data)), "ARGUS", None, None,
                                         None, 0, ctypes.byref(out)):
            raise OSError("CryptProtectData failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)

    def _unprotect(data: bytes) -> bytes:
        out = _DATA_BLOB()
        if not _crypt32.CryptUnprotectData(ctypes.byref(_blob(data)), None, None, None,
                                           None, 0, ctypes.byref(out)):
            raise OSError("CryptUnprotectData failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)
else:
    def _protect(data: bytes) -> bytes:
        return data

    def _unprotect(data: bytes) -> bytes:
        return data


class SecretStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "secrets.json"
        self.encrypted = _IS_WINDOWS

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        if not _IS_WINDOWS:
            os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def set(self, name: str, value: str) -> None:
        data = self._read()
        data[name] = base64.b64encode(_protect(value.encode("utf-8"))).decode("ascii")
        self._write(data)

    def get(self, name: str) -> str | None:
        raw = self._read().get(name)
        if raw is None:
            return None
        try:
            return _unprotect(base64.b64decode(raw)).decode("utf-8")
        except (OSError, ValueError):
            return None

    def delete(self, name: str) -> None:
        data = self._read()
        if data.pop(name, None) is not None:
            self._write(data)

    def configured(self) -> dict[str, bool]:
        return {k: True for k in self._read()}
