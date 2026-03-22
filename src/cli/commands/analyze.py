"""
analyze command — full market analysis (rule-based + optional LLM).
"""

import argparse

from ..utils import (
    binance_client, coingecko_client,
    indicators, market_context,
    get_llm_macro_context, feedback_logger, daily_logger,
)
from src import config


def add_flags(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = subparsers.add_parser("analyze", help="Run full market analysis")
    p.add_argument("--save", action="store_true", help="Save to daily analysis file")
    p.add_argument("--market", type=str, default="futures", choices=["futures", "spot"])
    p.add_argument("--use-llm", action="store_true",
                   help="Enable LLM Macro Risk Officer (Plutus V2)")
    p.add_argument("--llm-provider", type=str, default="minimax")
    return p


def cmd(args: argparse.Namespace) -> None:
    """Execute the analyze command."""
    print("=" * 60)
    print("PLUTUS - Daily Market Analysis")
    print("=" * 60)
    print()

    market = args.market
    analysis_results = {}

    # ── Per-symbol technical analysis ────────────────────────────────────────
    for symbol in config.TRADING_PAIRS:
        print(f"Fetching {symbol}...")
        try:
            candles = binance_client.fetch_klines(symbol, "1h", 200, market=market)
            analysis = indicators.analyze_symbol(symbol, candles)
            analysis_results[symbol] = analysis
            print(f"  - Price:  ${analysis['current_price']:,.2f}")
            print(f"  - Trend:  {analysis['trend']}")
            print(f"  - Signal: {analysis['signal']}")
        except Exception as e:
            print(f"  - Error: {e}")
        print()

    # ── Market overview ─────────────────────────────────────────────────────
    print("Fetching market overview...")
    try:
        market_overview = coingecko_client.get_market_overview()
        print(f"  - Total Cap:   ${market_overview.get('total_market_cap', 0) / 1e12:.2f}T")
        print(f"  - Fear & Greed: {market_overview.get('fear_greed_index', 'N/A')}")
    except Exception as e:
        print(f"  - Error: {e}")
        market_overview = {}
    print()

    # ── Rule-based market context ────────────────────────────────────────────
    btc = analysis_results.get("BTCUSDT", {})
    eth = analysis_results.get("ETHUSDT", {})

    risk_level = market_context.classify_risk_level(
        fear_greed_index=market_overview.get("fear_greed_index")
    )
    macro_state = market_context.determine_macro_state(btc, market_overview)
    btc_strength = market_context.assess_btc_strength(btc)

    print("## Market Context (Rule-Based)")
    print(f"- Risk Level: {risk_level}")
    print(f"- Macro:      {macro_state.upper()}")
    print(f"- BTC:        {btc_strength.upper()}")
    print()

    # ── LLM Macro Risk Officer ──────────────────────────────────────────────
    if getattr(args, "use_llm", False):
        print("## LLM Macro Risk Officer (Plutus V2)")
        print(f"- Provider: {getattr(args, 'llm_provider', 'minimax')}")
        try:
            llm_ctx = get_llm_macro_context(
                btc_analysis=btc,
                target_symbol="BTCUSDT",
                target_analysis=btc,
                market_overview=market_overview,
                provider=getattr(args, "llm_provider", "minimax"),
            )
            print(f"- Macro Regime:  {llm_ctx.get('macro_regime', 'UNKNOWN')}")
            print(f"- BTC Strength:  {llm_ctx.get('btc_strength', 'UNKNOWN')}")
            print(f"- Volatility:   {llm_ctx.get('volatility_warning', 'UNKNOWN')}")
            if llm_ctx.get("_block_reason"):
                print(f"- Block Reason: {llm_ctx['_block_reason']}")
            if llm_ctx.get("_error"):
                print(f"- LLM Warning:  {llm_ctx['_error']}")
        except Exception as e:
            print(f"- LLM Error: {e}")
        print()

    # ── Valid trading answers ────────────────────────────────────────────────
    rules = market_context.get_valid_trading_answers(macro_state, btc_strength)
    print("## Valid Trading Answers")
    print(f"- Allowed:   {', '.join(rules['valid_answers'])}")
    if rules["forbidden"]:
        print(f"- Forbidden: {', '.join(rules['forbidden'])}")
    print(f"- Rec:       {rules['recommendation']}")
    print()

    # ── Feedback prompt ─────────────────────────────────────────────────────
    print(feedback_logger.ask_feedback_template())

    # ── Save if requested ──────────────────────────────────────────────────
    if getattr(args, "save", False):
        content = "## Technical Analysis\n\n"
        for symbol, analysis in analysis_results.items():
            content += f"### {symbol}\n"
            content += f"- Price: ${analysis['current_price']:,.2f}\n"
            content += f"- Trend: {analysis['trend']}\n"
            content += f"- Signal: {analysis['signal']}\n"
            content += f"- Support: ${analysis.get('support', 0):,.2f}\n"
            content += f"- Resistance: ${analysis.get('resistance', 0):,.2f}\n\n"
        daily_logger.save_daily_analysis(content)
        print(f"\nSaved to {daily_logger.get_daily_file_path()}")
