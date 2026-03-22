"""
Main CLI entry point for Plutus Trading System.
"""

import argparse
import sys
from datetime import datetime
from typing import Optional

import pandas as pd

# Import modules
from . import config
from .data import binance_client, coingecko_client
from .data.coin_tiers import normalize_symbol
from .analysis import indicators, volume_profile, market_context
from .execution import position_sizer, decision_engine, trade_plan
from .storage import daily_logger, feedback_logger

# LLM client for Macro Risk Officer (Plutus V2)
from .data.llm_client import get_llm_macro_context
from .backtest.hybrid_strategy import HybridWorkflowStrategy


def cmd_analyze(args):
    """Run full market analysis."""
    print("=" * 60)
    print("PLUTUS - Daily Market Analysis")
    print("=" * 60)
    print()

    use_llm = getattr(args, 'use_llm', False)
    llm_provider = getattr(args, 'llm_provider', 'minimax')

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

    print("## Market Context (Rule-Based)")
    print(f"- Risk Level: {risk_level}")
    print(f"- Macro: {macro_state.upper()}")
    print(f"- BTC: {btc_strength.upper()}")
    print()

    # ── Plutus V2: LLM Macro Risk Officer ───────────────────────────────────
    if use_llm:
        print("## LLM Macro Risk Officer (Plutus V2)")
        print(f"- Provider: {llm_provider}")
        try:
            llm_ctx = get_llm_macro_context(
                btc_analysis=btc_analysis,
                target_symbol="BTCUSDT",
                target_analysis=btc_analysis,
                market_overview=market_overview,
                provider=llm_provider,
            )
            print(f"- Macro Regime: {llm_ctx.get('macro_regime', 'UNKNOWN')}")
            print(f"- BTC Strength:  {llm_ctx.get('btc_strength', 'UNKNOWN')}")
            print(f"- Volatility:    {llm_ctx.get('volatility_warning', 'UNKNOWN')}")
            if llm_ctx.get('_block_reason'):
                print(f"- Block Reason:  {llm_ctx['_block_reason']}")
            if llm_ctx.get('_error'):
                print(f"- LLM Warning:  {llm_ctx['_error']}")
        except Exception as e:
            print(f"- LLM Error: {e}")
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

    use_llm = getattr(args, 'use_llm', False)
    llm_provider = getattr(args, 'llm_provider', 'minimax')

    # Get all symbols to scan (normalize each)
    symbols = [normalize_symbol(s.strip()) for s in args.symbols.split(",")] if args.symbols else config.TRADING_PAIRS
    market = getattr(args, 'market', 'futures')

    # ── Plutus V2: Fetch LLM macro context once for the whole scan ─────────
    llm_ctx = None
    if use_llm:
        print("Fetching LLM Macro Risk Officer context...")
        try:
            btc_candles = binance_client.fetch_klines("BTCUSDT", "1h", 200, market=market)
            btc_analysis = indicators.analyze_symbol("BTCUSDT", btc_candles)
            llm_ctx = get_llm_macro_context(
                btc_analysis=btc_analysis,
                target_symbol="BTCUSDT",
                target_analysis=btc_analysis,
                market_overview={},
                provider=llm_provider,
            )
            print(f"  LLM → Macro: {llm_ctx.get('macro_regime','?')} | "
                  f"BTC: {llm_ctx.get('btc_strength','?')} | "
                  f"Vol: {llm_ctx.get('volatility_warning','?')}")
        except Exception as e:
            print(f"  LLM Error: {e} — proceeding without LLM gate")
        print()

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
                    # ── LLM Execution Gate ─────────────────────────────────
                    if llm_ctx is not None:
                        macro_regime = llm_ctx.get("macro_regime", "NEUTRAL")
                        btc_strength = llm_ctx.get("btc_strength", "NEUTRAL")
                        is_alt = not any(
                            s in symbol.upper() for s in ["BTCUSDT", "ETHUSDT"]
                        )
                        if is_alt and macro_regime == "RISK_OFF":
                            print(f"  - ⚠️ LLM GATE: macro_regime=RISK_OFF → ALT LONG blocked")
                            continue
                        if btc_strength == "WEAK":
                            print(f"  - ⚠️ LLM GATE: btc_strength=WEAK → LONG blocked")
                            continue
                        vol = llm_ctx.get("volatility_warning", "LOW")
                        if vol == "HIGH":
                            print(f"  - ⚠️ LLM GATE: volatility=HIGH → conservative sizing enforced")

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
    """Run backtest — routes to V1/V2 legacy or V3 Chronos Engine."""
    # ── Plutus V3: Chronos Event-Driven Engine ───────────────────────────
    if getattr(args, 'v3_chronos', False):
        from .backtest.chronos_engine import ChronosBacktester, BacktestMode
        from .data import binance_client

        mode = BacktestMode.DRY_RUN if args.v3_mode == "dry_run" else BacktestMode.LIVE
        symbols = [
            normalize_symbol(s.strip())
            for s in args.symbols.split(",")
        ] if args.symbols else ["BTCUSDT"]

        # Default dates: last 90 days for Chronos (faster scan)
        from datetime import timedelta as td
        end_dt   = datetime.now()
        start_dt = end_dt - td(days=90)
        start_str = args.start or start_dt.strftime("%Y-%m-%d")
        end_str   = args.end   or end_dt.strftime("%Y-%m-%d")

        print("=" * 60)
        print("PLUTUS V3: CHRONOS ENGINE (Event-Driven MoE Wakelock)")
        print("=" * 60)
        print(f"Symbols: {symbols}")
        print(f"Period: {start_str} to {end_str}")
        print(f"Mode: {mode.value.upper()}")
        print(f"Initial Equity: ${args.v3_equity:,.2f}")
        print(f"Min Confidence: {args.v3_min_confidence}")
        print("=" * 60)
        print()
        print("Fetching historical data...")

        # Convert date strings to millisecond timestamps
        start_ms = int(datetime.strptime(start_str, "%Y-%m-%d").timestamp() * 1000)
        end_ms   = int((datetime.strptime(end_str, "%Y-%m-%d") + td(days=1)).timestamp() * 1000)

        # Fetch candles for each symbol
        from .backtest.chronos_engine import ChronosBacktester as CB
        # Run Chronos on each symbol
        for sym in symbols:
            print(f"\nSymbol: {sym}")
            try:
                market = args.market
                candles = binance_client.fetch_klines(
                    sym, "1h",
                    limit=2000,
                    market=market,
                    start_time=start_ms,
                    end_time=end_ms,
                )
                if not candles:
                    print(f"  No data for {sym}")
                    continue
                df = pd.DataFrame(candles)
                if "timestamp" not in df.columns:
                    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms")
                result = CB(
                    mode=mode,
                    initial_equity=args.v3_equity,
                    min_confidence=args.v3_min_confidence,
                ).run_backtest(df)
            except Exception as e:
                print(f"  Error: {e}")
        return

    # ── Plutus V1/V2: Legacy sequential backtest ──────────────────────
    from datetime import timedelta
    from .backtest.strategy import run_backtest, DEFAULT_SYMBOLS, StrategyConfig

    use_llm = getattr(args, 'use_llm', False)
    llm_provider = getattr(args, 'llm_provider', 'minimax')

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
    print("PLUTUS BACKTESTER" + (" (LLM Macro Gate ENABLED — Plutus V2)" if use_llm else ""))
    print("=" * 60)
    print(f"Symbols: {len(symbols)} coins")
    print(f"Period: {start_date} to {end_date}")
    print(f"Market: {args.market}")
    print(f"Initial Equity: ${args.equity:,.2f}")
    print(f"Risk: {args.risk}%")
    print(f"Max Leverage: {args.leverage}x")
    if use_llm:
        print(f"LLM Provider: {llm_provider}")
        print(f"LLM Mode: Macro Risk Officer (Execution Gate)")
    print("=" * 60)
    print()

    result = run_backtest(
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        initial_equity=args.equity,
        config=strategy_config,
        market=args.market,
        use_llm=use_llm,
        llm_provider=llm_provider,
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
    # Plutus V2: LLM Macro Risk Officer flags
    analyze_parser.add_argument(
        "--use-llm", action="store_true",
        help="Enable LLM Macro Risk Officer (Plutus V2)"
    )
    analyze_parser.add_argument(
        "--llm-provider", type=str, default="minimax",
        help="LLM provider (default: minimax)"
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
    # Plutus V2: LLM Macro Risk Officer flags
    scan_parser.add_argument(
        "--use-llm", action="store_true",
        help="Enable LLM Macro Risk Officer (Plutus V2)"
    )
    scan_parser.add_argument(
        "--llm-provider", type=str, default="minimax",
        help="LLM provider (default: minimax)"
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
    # Plutus V2: LLM Macro Risk Officer flags
    backtest_parser.add_argument(
        "--use-llm", action="store_true",
        help="Enable LLM Macro Risk Officer in backtest (Plutus V2)"
    )
    backtest_parser.add_argument(
        "--llm-provider", type=str, default="minimax",
        help="LLM provider (default: minimax)"
    )
    # Plutus V3: Chronos Event-Driven Wakelock flags
    backtest_parser.add_argument(
        "--v3-chronos", action="store_true",
        help="Enable Chronos Engine (Plutus V3): event-driven MoE backtest — Scanner → Personas → Allocator"
    )
    backtest_parser.add_argument(
        "--v3-mode", type=str, default="dry_run",
        choices=["dry_run", "live"],
        help="Chronos persona mode: dry_run (mock LLM) or live (real API calls)"
    )
    backtest_parser.add_argument(
        "--v3-equity", type=float, default=10000.0,
        help="Chronos Engine initial equity (default: $10,000)"
    )
    backtest_parser.add_argument(
        "--v3-min-confidence", type=int, default=40,
        help="Chronos Engine minimum blended confidence to execute trade (default: 40)"
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
