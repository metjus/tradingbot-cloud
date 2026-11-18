"""numba when available (research / tests), plain Python otherwise.

The packaged Windows app runs CFE without numba: numba's on-disk cache does
not work inside a frozen app and the compiler adds ~100 MB, while CFE only
runs once per closed 15m bar (pure Python is fast enough). Results are
identical - the decorated functions are ordinary NumPy loops.
"""
from __future__ import annotations

import os
import sys


def _identity(*args, **kwargs):
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]
    return lambda f: f


njit = _identity
if not getattr(sys, "frozen", False) and not os.environ.get("ARGUS_CFE_NO_JIT"):
    try:
        from numba import njit  # type: ignore[no-redef]  # noqa: F811
    except ImportError:  # pragma: no cover - optional speed-up only
        pass
