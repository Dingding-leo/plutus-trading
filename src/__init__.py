"""
Plutus Trading System.

A systematic cryptocurrency trading platform combining market data collection,
technical analysis, decision engine, position sizing, and backtesting.

Usage:
    from src import cli
    cli.main()

Or use the CLI directly:
    python -m src.cli analyze
    python -m src.cli scan --symbols BTCUSDT,ETHUSDT
    python -m src.cli backtest --start 2025-01-01 --end 2026-01-01
"""

__version__ = "1.0.0"

# Core modules available at package level
from . import config
from . import analysis
from . import data
from . import execution
from . import storage
from . import backtest
