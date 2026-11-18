"""CFE - Confluence Futures Engine (ported from the research repository,
metjus/trash branch claude/tradingview-futures-indicator-4qmlz9: backtest/
cfe.py, engine.py, indicators.py, sim.py - the reference logic behind
pine/CFE_indicator.pine). The engine files are copied unchanged except for
the numba import (see _jit.py); adapter.py feeds them Argus candles.

Validation in the research repository did NOT show a positive expected value
after costs (15m about -0.02 R per trade, 5m about -0.06 R on new pairs):
paper trading only.
"""
