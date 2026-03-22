"""
Plutus CLI — Modular command structure.

Subcommands:
  analyze   — full market analysis
  scan      — intraday scanner
  trade     — trade plan generator
  feedback  — feedback logger
  backtest  — backtester (V1/V2 legacy + V3 Chronos)

Usage:
  python -m src.cli.main analyze
  python -m src.cli.main backtest --v3-chronos
"""

from src.cli.main import main

__all__ = ["main"]
