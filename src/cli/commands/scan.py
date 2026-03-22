"""
scan command — intraday multi-timeframe scanner.
"""

import argparse

from src import config
from src.data import binance_client
from src.data.coin_tiers import normalize_symbol
from src.analysis import volume_profile
from src.data.llm_client import get_llm_macro_context


def add_flags(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = subparsers.add_parser("scan", help="Run intraday scanner")
    p.add_argument("--symbols", type=str, default=None,
                   help="Comma-separated symbols (default: all from config)")
    p.add_argument("--market", type=str, default="futures", choices=["futures", "spot"])
    p.add_argument("--use-llm", action="store_true",
                   help="Enable LLM Macro Risk Officer")
    p.add_argument("--llm-provider", type=str, default="minimax")
    return p


def cmd(args: argparse.Namespace) -> None:
    """Execute the scan command."""
    print("=" * 60)
    print("PLUTUS - Intraday Scanner")
    print("=" * 60)
    print()

    symbols = (
        [normalize_symbol(s.strip()) for s in args.symbols.split(",")]
        if args.symbols
        else config.TRADING_PAIRS
    )
    market = args.market
    timeframes = ["5m", "15m", "30m"]

    # ── LLM Macro Risk Officer (fetched once for all symbols) ────────────────
    llm_ctx = None
    if getattr(args, "use_llm", False):
        print("Fetching LLM Macro Risk Officer context...")
        try:
            btc_candles = binance_client.fetch_klines("BTCUSDT", "1h", 200, market=market)
            from ..analysis import indicators as ind
            btc_analysis = ind.analyze_symbol("BTCUSDT", btc_candles)
            llm_ctx = get_llm_macro_context(
                btc_analysis=btc_analysis,
                target_symbol="BTCUSDT",
                target_analysis=btc_analysis,
                market_overview={},
                provider=getattr(args, "llm_provider", "minimax"),
            )
            print(f"  LLM → Macro: {llm_ctx.get('macro_regime','?')}  |  "
                  f"BTC: {llm_ctx.get('btc_strength','?')}  |  "
                  f"Vol: {llm_ctx.get('volatility_warning','?')}")
        except Exception as e:
            print(f"  LLM Error: {e} — proceeding without LLM gate")
        print()

    # ── Scan each symbol ────────────────────────────────────────────────────
    for symbol in symbols:
        print(f"Scanning {symbol}...")
        try:
            levels_by_tf = {}
            for tf in timeframes:
                candles = binance_client.fetch_klines(symbol, tf, 200, market=market)
                levels = volume_profile.get_key_levels(candles)
                levels_by_tf[tf] = levels

            resonance = volume_profile.check_multi_timeframe_resonance(
                levels_by_tf["5m"],
                levels_by_tf["15m"],
                levels_by_tf["30m"],
            )
            print(f"  - Resonance: {resonance['resonance_strength']}")
            print(f"  - Matching levels: {resonance['level_count']}")

            if resonance["resonance_strength"] != "NONE":
                if llm_ctx is not None:
                    macro_regime = llm_ctx.get("macro_regime", "NEUTRAL")
                    btc_strength = llm_ctx.get("btc_strength", "NEUTRAL")
                    is_alt = not any(s in symbol.upper() for s in ["BTCUSDT", "ETHUSDT"])
                    if is_alt and macro_regime == "RISK_OFF":
                        print(f"  - LLM GATE: RISK_OFF → ALT LONG blocked")
                        continue
                    if btc_strength == "WEAK":
                        print(f"  - LLM GATE: btc_strength=WEAK → LONG blocked")
                        continue
                    vol = llm_ctx.get("volatility_warning", "LOW")
                    if vol == "HIGH":
                        print(f"  - LLM GATE: volatility=HIGH → conservative sizing enforced")
                print(f"  - ** TRADING OPPORTUNITY **")
        except Exception as e:
            print(f"  - Error: {e}")
        print()
