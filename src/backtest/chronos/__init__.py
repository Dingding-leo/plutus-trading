"""
src/backtest/chronos/ — Chronos Event-Driven Backtest Engine
=============================================================

Module structure:

  engine.py    — ChronosBacktester (main orchestrator)
  personas.py  — DryRunPersonaSignal
  backtest.py  — run_chronos_backtest() top-level function

This __init__.py re-exports everything from chronos_engine.py
for backward compatibility.

Backward-compatible import:
    from src.backtest.chronos_engine import ChronosBacktester
    from src.backtest.chronos import ChronosBacktester       # same thing
"""

from src.backtest.chronos_engine import (
    BacktestMode,
    BlendedTrade,
    DryRunPersonaSignal,
    ChronosBacktester,
    run_chronos_backtest,
)

__all__ = [
    "BacktestMode",
    "BlendedTrade",
    "DryRunPersonaSignal",
    "ChronosBacktester",
    "run_chronos_backtest",
]
