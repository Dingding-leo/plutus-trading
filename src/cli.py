"""
Main CLI entry point for Plutus Trading System.
"""

import argparse
import sys
from datetime import datetime
from typing import Optional

# Import modules
from . import config
from .data import binance_client, coingecko_client
from .data.coin_tiers import normalize_symbol
from .analysis import indicators, volume_profile, market_context
from .execution import position_sizer, decision_engine, trade_plan
from .storage import daily_logger, feedback_logger


def cmd_analyze(args):
    """Run full market analysis."""
    print("=" * 60)
    print("PLUTUS - Daily Market Analysis")
    print("=" * 60)
    print()

    # Get market type from config
    market = getattr(args, 'market', 'futures')

    # Get market data for each symbol
    analysis_results = {}

    for symbol in config.TRADING_PAIRS:
        print(f"Fetching {symbol}...")
        try:
            candles = binance_client.fetch_klines(symbol, "1h", 200, market=market)
            analysis = indicators.analyze_symbol(symbol, candles)
            analysis_results[symbol] = analysis
            print(f"  - Price: ${analysis['current_price']:,.2f}")
            print(f"  - Trend: {analysis['trend']}")
            print(f"  - Signal: {analysis['signal']}")
        except Exception as e:
            print(f"  - Error: {e}")
        print()

    # Get market overview
    print("Fetching market overview...")
    try:
        market_overview = coingecko_client.get_market_overview()
        print(f"  - Total Cap: ${market_overview.get('total_market_cap', 0)/1e12:.2f}T")
        print(f"  - Fear & Greed: {market_overview.get('fear_greed_index', 'N/A')}")
    except Exception as e:
        print(f"  - Error: {e}")
        market_overview = {}
    print()

    # Get market context
    btc_analysis = analysis_results.get("BTCUSDT", {})
    eth_analysis = analysis_results.get("ETHUSDT", {})

    risk_level = market_context.classify_risk_level(
        fear_greed_index=market_overview.get("fear_greed_index")
    )
    macro_state = market_context.determine_macro_state(btc_analysis, market_overview)
    btc_strength = market_context.assess_btc_strength(btc_analysis)

    print("## Market Context")
    print(f"- Risk Level: {risk_level}")
    print(f"- Macro: {macro_state.upper()}")
    print(f"- BTC: {btc_strength.upper()}")
    print()

    # Get trading rules
    trading_rules = market_context.get_valid_trading_answers(macro_state, btc_strength)
    print("## Valid Trading Answers")
    print(f"- Allowed: {', '.join(trading_rules['valid_answers'])}")
    if trading_rules['forbidden']:
        print(f"- Forbidden: {', '.join(trading_rules['forbidden'])}")
    print(f"- Recommendation: {trading_rules['recommendation']}")
    print()

    # Ask for feedback
    print(feedback_logger.ask_feedback_template())

    # Save to file if requested
    if args.save:
        content = f"## Technical Analysis\n\n"
        for symbol, analysis in analysis_results.items():
            content += f"### {symbol}\n"
            content += f"- Price: ${analysis['current_price']:,.2f}\n"
            content += f"- Trend: {analysis['trend']}\n"
            content += f"- Signal: {analysis['signal']}\n"
            content += f"- Support: ${analysis.get('support', 0):,.2f}\n"
            content += f"- Resistance: ${analysis.get('resistance', 0):,.2f}\n\n"

        daily_logger.save_daily_analysis(content)
        print(f"Saved to {daily_logger.get_daily_file_path()}")


def cmd_scan(args):
    """Run intraday scanning."""
    print("=" * 60)
    print("PLUTUS - Intraday Scanner")
    print("=" * 60)
    print()

    # Get all symbols to scan (normalize each)
    symbols = [normalize_symbol(s.strip()) for s in args.symbols.split(",")] if args.symbols else config.TRADING_PAIRS
    market = getattr(args, 'market', 'futures')

    # Timeframes to check
    timeframes = ["5m", "15m", "30m"]

    for symbol in symbols:
        print(f"Scanning {symbol}...")

        try:
            levels_by_tf = {}
            for tf in timeframes:
                candles = binance_client.fetch_klines(symbol, tf, 200, market=market)
                levels = volume_profile.get_key_levels(candles)
                levels_by_tf[tf] = levels

            # Check resonance
            if len(levels_by_tf) >= 3:
                resonance = volume_profile.check_multi_timeframe_resonance(
                    levels_by_tf["5m"],
                    levels_by_tf["15m"],
                    levels_by_tf["30m"],
                )
                print(f"  - Resonance: {resonance['resonance_strength']}")
                print(f"  - Matching levels: {resonance['level_count']}")

                if resonance['resonance_strength'] != "NONE":
                    print(f"  - **TRADING OPPORTUNITY**")

        except Exception as e:
            print(f"  - Error: {e}")
        print()


def cmd_trade(args):
    """Generate trade plan."""
    print("=" * 60)
    print("PLUTUS - Trade Plan Generator")
    print("=" * 60)
    print()

    symbol = normalize_symbol(args.symbol.upper())
    direction = args.direction.upper()
    equity = args.equity
    risk_level = args.risk_level.upper()

    print(f"Input:")
    print(f"- Symbol: {symbol}")
    print(f"- Direction: {direction}")
    print(f"- Equity: ${equity:,.2f}")
    print(f"- Risk Level: {risk_level}")
    print()

    # Get current price and candles
    market = getattr(args, 'market', 'futures')
    try:
        current_price = binance_client.get_current_price(symbol, market=market)
        candles = binance_client.fetch_klines(symbol, "1h", 200, market=market)
        print(f"Current Price: ${current_price:,.2f}")
    except Exception as e:
        print(f"Error fetching data: {e}")
        return

    # Get coin type
    coin_type = config.SYMBOLS.get(symbol, {}).get("type", "small")

    # Get key levels
    levels = volume_profile.get_key_levels(candles)
    entry_target = volume_profile.find_entry_target(
        current_price, levels, "long" if direction == "BUY" else "short"
    )

    entry = entry_target["entry"]
    stop = entry_target["stop"]
    target = entry_target["target"]

    # Get position multiplier
    pos_mult = position_sizer.get_position_multiplier(risk_level)

    # Calculate position
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

    # Create trade plan
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

    # Print trade plan
    print(trade_plan.format_trade_plan(plan))

    # Validate
    validation = trade_plan.validate_trade_plan(plan)
    if validation["errors"]:
        print("## Errors")
        for err in validation["errors"]:
            print(f"  - {err}")
    if validation["warnings"]:
        print("## Warnings")
        for warn in validation["warnings"]:
            print(f"  - {warn}")


def cmd_feedback(args):
    """Log feedback from Austin."""
    print("=" * 60)
    print("PLUTUS - Feedback Logger")
    print("=" * 60)
    print()

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    date = datetime.strptime(date_str, "%Y-%m-%d")

    my_analysis = args.analysis
    reality = args.reality
    correction = args.correction
    lessons = args.lessons.split(",") if args.lessons else []

    try:
        path = feedback_logger.save_feedback(
            date=date,
            my_analysis=my_analysis,
            reality=reality,
            correction=correction,
            lessons=lessons,
        )
        print(f"Feedback saved to {path}")
    except Exception as e:
        print(f"Error saving feedback: {e}")


def cmd_backtest(args):
    """Run backtest."""
    from datetime import timedelta
    from .backtest.strategy import run_backtest, DEFAULT_SYMBOLS, StrategyConfig
    from .backtest.engine import format_results

    # Parse symbols (normalize each to standard format)
    symbols = [normalize_symbol(s.strip()) for s in args.symbols.split(",")] if args.symbols else DEFAULT_SYMBOLS

    # Default dates - 12 months
    if args.start:
        start_date = args.start
    else:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=365)
        start_date = start_dt.strftime("%Y-%m-%d")

    end_date = args.end or datetime.now().strftime("%Y-%m-%d")

    # Config
    strategy_config = StrategyConfig(
        base_risk_pct=args.risk / 100,
        max_leverage=args.leverage,
        pos_mult=args.pos_mult,
        min_rr=args.min_rr,
    )

    print("=" * 60)
    print("PLUTUS BACKTESTER")
    print("=" * 60)
    print(f"Symbols: {len(symbols)} coins")
    print(f"Period: {start_date} to {end_date}")
    print(f"Market: {args.market}")
    print(f"Initial Equity: ${args.equity:,.2f}")
    print(f"Risk: {args.risk}%")
    print(f"Max Leverage: {args.leverage}x")
    print("=" * 60)
    print()

    result = run_backtest(
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        initial_equity=args.equity,
        config=strategy_config,
        market=args.market,
    )

    print(result["output"])


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Plutus - Trading Analysis System"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Analyze command
    analyze_parser = subparsers.add_parser(
        "analyze", help="Run full market analysis"
    )
    analyze_parser.add_argument(
        "--save", action="store_true", help="Save to daily analysis file"
    )
    analyze_parser.add_argument(
        "--market", type=str, default="futures", choices=["futures", "spot"],
        help="Market data source (default: futures)"
    )

    # Scan command
    scan_parser = subparsers.add_parser(
        "scan", help="Run intraday scanner"
    )
    scan_parser.add_argument(
        "--symbols", type=str, help="Comma-separated symbols (default: BTC,ETH,SOL)"
    )
    scan_parser.add_argument(
        "--market", type=str, default="futures", choices=["futures", "spot"],
        help="Market data source (default: futures)"
    )

    # Trade command
    trade_parser = subparsers.add_parser(
        "trade", help="Generate trade plan"
    )
    trade_parser.add_argument(
        "--symbol", type=str, required=True, help="Trading pair (e.g., BTCUSDT)"
    )
    trade_parser.add_argument(
        "--direction", type=str, required=True, help="BUY or SELL"
    )
    trade_parser.add_argument(
        "--equity", type=float, default=10000, help="Account equity"
    )
    trade_parser.add_argument(
        "--risk-level", type=str, default="MODERATE", help="Risk level (LOW/MODERATE/HIGH)"
    )
    trade_parser.add_argument(
        "--market", type=str, default="futures", choices=["futures", "spot"],
        help="Market data source (default: futures)"
    )

    # Feedback command
    feedback_parser = subparsers.add_parser(
        "feedback", help="Log feedback"
    )
    feedback_parser.add_argument(
        "--date", type=str, help="Date (YYYY-MM-DD)"
    )
    feedback_parser.add_argument(
        "--analysis", type=str, required=True, help="What you said"
    )
    feedback_parser.add_argument(
        "--reality", type=str, required=True, help="What actually happened"
    )
    feedback_parser.add_argument(
        "--correction", type=str, required=True, help="Correction"
    )
    feedback_parser.add_argument(
        "--lessons", type=str, help="Lessons learned (comma-separated)"
    )

    # Backtest command
    backtest_parser = subparsers.add_parser(
        "backtest", help="Run backtest"
    )
    backtest_parser.add_argument(
        "--symbols", type=str, default="BTCUSDT,ETHUSDT,SOLUSDT", help="Comma-separated symbols (e.g. BTCUSDT,ETHUSDT)"
    )
    backtest_parser.add_argument(
        "--start", type=str, default=None, help="Start date (YYYY-MM-DD)"
    )
    backtest_parser.add_argument(
        "--end", type=str, default=None, help="End date (YYYY-MM-DD)"
    )
    backtest_parser.add_argument(
        "--market", type=str, default="futures", choices=["futures", "spot"],
        help="Market data source (default: futures)"
    )
    backtest_parser.add_argument(
        "--equity", type=float, default=10000, help="Initial equity"
    )
    backtest_parser.add_argument(
        "--risk", type=float, default=1.0, help="Risk per trade %%"
    )
    backtest_parser.add_argument(
        "--leverage", type=float, default=50, help="Max leverage"
    )
    backtest_parser.add_argument(
        "--pos-mult", type=float, default=1.0, help="Position multiplier"
    )
    backtest_parser.add_argument(
        "--min-rr", type=float, default=1.5, help="Minimum RR"
    )

    args = parser.parse_args()

    if args.command == "analyze":
        cmd_analyze(args)
    elif args.command == "scan":
        cmd_scan(args)
    elif args.command == "trade":
        cmd_trade(args)
    elif args.command == "feedback":
        cmd_feedback(args)
    elif args.command == "backtest":
        cmd_backtest(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
