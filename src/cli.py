"""
Main CLI entry point for Plutus Trading System.

This module is a thin shim — the actual implementation lives in src/cli/.
Keeping this file here preserves the public API: `from src.cli import main`.

Usage:
    python3 -m src.cli.main analyze
    python3 -m src.cli.main backtest --v3-chronos
    python3 src/cli.py analyze           (also works)
"""

from src.cli.main import main

if __name__ == "__main__":
    main()
