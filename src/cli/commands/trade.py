"""
trade command — trade plan generator.
"""

import argparse

from src import config
from ..utils import (
    binance_client, normalize_symbol,
    volume_profile, position_sizer, trade_plan,
)


def add_flags(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = subparsers.add_parser("trade", help="Generate trade plan")
    p.add_argument("--symbol", type=str, required=True, help="Trading pair (e.g. BTCUSDT)")
    p.add_argument("--direction", type=str, required=True, choices=["BUY", "SELL"],
                   help="BUY or SELL")
    p.add_argument("--equity", type=float, default=10000.0)
    p.add_argument("--risk-level", type=str, default="MODERATE",
                   choices=["LOW", "MODERATE", "HIGH"])
    p.add_argument("--market", type=str, default="futures", choices=["futures", "spot"])
    return p


def cmd(args: argparse.Namespace) -> None:
    """Execute the trade command."""
    print("=" * 60)
    print("PLUTUS - Trade Plan Generator")
    print("=" * 60)
    print()

    symbol = normalize_symbol(args.symbol.upper())
    direction = args.direction.upper()
    equity = args.equity
    risk_level = args.risk_level.upper()
    market = args.market

    print(f"Symbol:    {symbol}")
    print(f"Direction: {direction}")
    print(f"Equity:    ${equity:,.2f}")
    print(f"Risk:      {risk_level}")
    print()

    # Fetch data
    try:
        current_price = binance_client.get_current_price(symbol, market=market)
        candles = binance_client.fetch_klines(symbol, "1h", 200, market=market)
        print(f"Current Price: ${current_price:,.2f}")
    except Exception as e:
        print(f"Error fetching data: {e}")
        return

    coin_type = config.SYMBOLS.get(symbol, {}).get("type", "small")

    # Key levels
    levels = volume_profile.get_key_levels(candles)
    entry_target = volume_profile.find_entry_target(
        current_price, levels, "long" if direction == "BUY" else "short"
    )
    entry = entry_target["entry"]
    stop = entry_target["stop"]
    target = entry_target["target"]

    # Position sizing
    pos_mult = position_sizer.get_position_multiplier(risk_level)
    stop_distance = abs(entry - stop) / entry
    position = position_sizer.calculate_position_size(
        equity=equity,
        stop_distance=stop_distance,
        pos_mult=pos_mult,
        coin_type=coin_type,
    )

    if not position["valid"]:
        print(f"Error: {position['error']}")
        return

    plan = trade_plan.create_trade_plan(
        symbol=symbol,
        direction=direction,
        entry=entry,
        stop=stop,
        target=target,
        position_size=position["max_position"],
        leverage=position["recommended_leverage"],
        pos_mult=pos_mult,
        current_price=current_price,
        risk_level=risk_level,
    )

    print(trade_plan.format_trade_plan(plan))

    validation = trade_plan.validate_trade_plan(plan)
    if validation["errors"]:
        print("## Errors")
        for err in validation["errors"]:
            print(f"  - {err}")
    if validation["warnings"]:
        print("## Warnings")
        for warn in validation["warnings"]:
            print(f"  - {warn}")
