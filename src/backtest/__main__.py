"""
Backtest CLI - Run backtests from command line.
"""

import argparse
from datetime import datetime, timedelta

from .strategy import run_backtest, DEFAULT_SYMBOLS, StrategyConfig
from .engine import format_results


def main():
    parser = argparse.ArgumentParser(description="Plutus Backtester")

    parser.add_argument(
        "--symbols",
        type=str,
        default="BTCUSDT,ETHUSDT,SOLUSDT",
        help="Comma-separated symbols (no hyphens, e.g. BTCUSDT,ETHUSDT)"
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="Start date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="End date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--equity",
        type=float,
        default=10000,
        help="Initial equity"
    )
    parser.add_argument(
        "--risk",
        type=float,
        default=0.01,
        help="Risk per trade (default 1%%)"
    )
    parser.add_argument(
        "--leverage",
        type=float,
        default=50,
        help="Max leverage"
    )
    parser.add_argument(
        "--pos-mult",
        type=float,
        default=1.0,
        help="Position multiplier"
    )
    parser.add_argument(
        "--min-rr",
        type=float,
        default=1.5,
        help="Minimum risk-reward ratio"
    )

    args = parser.parse_args()

    # Parse symbols (normalize each to standard format)
    from ..data.coin_tiers import normalize_symbol
    symbols = [normalize_symbol(s.strip()) for s in args.symbols.split(",")]

    # Default dates
    if args.start:
        start_date = args.start
    else:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=365)
        start_date = start_dt.strftime("%Y-%m-%d")

    if args.end:
        end_date = args.end
    else:
        end_date = datetime.now().strftime("%Y-%m-%d")

    # Create strategy config
    config = StrategyConfig(
        base_risk_pct=args.risk,
        max_leverage=args.leverage,
        pos_mult=args.pos_mult,
        min_rr=args.min_rr,
    )

    print("=" * 60)
    print("PLUTUS BACKTESTER")
    print("=" * 60)
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Period: {start_date} to {end_date}")
    print(f"Initial Equity: ${args.equity:,.2f}")
    print(f"Risk: {args.risk*100}%")
    print(f"Max Leverage: {args.leverage}x")
    print(f"Min RR: {args.min_rr}")
    print("=" * 60)
    print()

    # Run backtest
    result = run_backtest(
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        initial_equity=args.equity,
        config=config,
    )

    print(result["output"])


if __name__ == "__main__":
    main()
