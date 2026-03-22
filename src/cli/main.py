"""
Main CLI entry point + argument parsing.
Delegates to command modules in commands/.

Usage:
  python -m src.cli.main analyze
  python -m src.cli.main backtest --v3-chronos
"""

import argparse
import sys


def main():
    """Parse args and dispatch to command modules."""
    parser = argparse.ArgumentParser(
        description="Plutus - Trading Analysis System"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # ── Shared flags (added by each command module via add_flags) ───────────
    from src.cli.commands import backtest, scan, analyze, trade, feedback
    backtest.add_flags(subparsers)
    scan.add_flags(subparsers)
    analyze.add_flags(subparsers)
    trade.add_flags(subparsers)
    feedback.add_flags(subparsers)

    args = parser.parse_args(sys.argv[1:] if sys.argv[1:] else ["--help"])

    if args.command is None:
        parser.print_help()
        return

    # Dispatch
    dispatch = {
        "backtest": backtest.cmd,
        "scan": scan.cmd,
        "analyze": analyze.cmd,
        "trade": trade.cmd,
        "feedback": feedback.cmd,
    }
    fn = dispatch.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()
