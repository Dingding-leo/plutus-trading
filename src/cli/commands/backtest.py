"""
backtest command — V1/V2 legacy + V3 Chronos Engine.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pandas as pd


def add_flags(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = subparsers.add_parser("backtest", help="Run backtest")
    p.add_argument("--symbols", type=str, default="BTCUSDT,ETHUSDT,SOLUSDT",
                   help="Comma-separated symbols")
    p.add_argument("--start", type=str, default=None, help="Start date (YYYY-MM-DD)")
    p.add_argument("--end", type=str, default=None, help="End date (YYYY-MM-DD)")
    p.add_argument("--market", type=str, default="futures", choices=["futures", "spot"])
    p.add_argument("--equity", type=float, default=10000.0)
    p.add_argument("--risk", type=float, default=1.0, help="Risk per trade %%")
    p.add_argument("--leverage", type=float, default=50.0)
    p.add_argument("--pos-mult", type=float, default=1.0)
    p.add_argument("--min-rr", type=float, default=1.5)
    # Plutus V2
    p.add_argument("--use-llm", action="store_true")
    p.add_argument("--llm-provider", type=str, default="minimax")
    # Plutus V3 Chronos
    p.add_argument("--v3-chronos", action="store_true")
    p.add_argument("--v3-mode", type=str, default="dry_run", choices=["dry_run", "live"])
    p.add_argument("--v3-equity", type=float, default=10000.0)
    p.add_argument("--v3-min-confidence", type=int, default=40)
    return p


def cmd(args: argparse.Namespace) -> None:
    """Execute the backtest command."""
    if getattr(args, "v3_chronos", False):
        _cmd_chronos(args)
    else:
        _cmd_legacy(args)


def _cmd_chronos(args: argparse.Namespace) -> None:
    from ..backtest.chronos_engine import ChronosBacktester, BacktestMode
    from ..data import binance_client

    mode = BacktestMode.DRY_RUN if args.v3_mode == "dry_run" else BacktestMode.LIVE
    symbols = [
        s.strip().upper().replace("-", "")
        for s in args.symbols.split(",")
    ] if args.symbols else ["BTCUSDT"]

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=90)
    start_str = args.start or start_dt.strftime("%Y-%m-%d")
    end_str = args.end or end_dt.strftime("%Y-%m-%d")

    print("=" * 60)
    print("PLUTUS V3: CHRONOS ENGINE (Event-Driven MoE Wakelock)")
    print(f"Symbols: {symbols}")
    print("=" * 60)
    print(f"Period: {start_str} → {end_str}  |  Mode: {mode.value.upper()}")
    print(f"Initial Equity: ${args.v3_equity:,.2f}  |  Min Confidence: {args.v3_min_confidence}")
    print()

    start_ms = int(datetime.strptime(start_str, "%Y-%m-%d").timestamp() * 1000)
    end_ms = int((datetime.strptime(end_str, "%Y-%m-%d") + timedelta(days=1)).timestamp() * 1000)

    def _fetch_symbol(sym: str):
        try:
            candles = binance_client.fetch_klines(
                sym, "1h", limit=2000,
                market=args.market, start_time=start_ms, end_time=end_ms,
            )
            if not candles:
                return (sym, None, "no data")
            df = pd.DataFrame(candles)
            if "timestamp" not in df.columns:
                df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms")
            return (sym, df, None)
        except Exception as e:
            return (sym, None, str(e))

    print(f"Fetching {len(symbols)} symbols in parallel...")
    dfs = {}
    with ThreadPoolExecutor(max_workers=min(len(symbols), 8)) as pool:
        for sym, df_sym, err in pool.map(_fetch_symbol, symbols):
            if err:
                print(f"  {sym}: {err}")
            else:
                dfs[sym] = df_sym
                print(f"  {sym}: {len(df_sym)} candles loaded")

    if not dfs:
        print("No data — aborting.")
        return

    try:
        engine = ChronosBacktester(
            universe=symbols, mode=mode,
            initial_equity=args.v3_equity,
            min_confidence=args.v3_min_confidence,
        )
        result = engine.run_backtest(dfs)
        print(result)
    except Exception as e:
        import traceback
        traceback.print_exc()


def _cmd_legacy(args: argparse.Namespace) -> None:
    from ..backtest.strategy import run_backtest, DEFAULT_SYMBOLS, StrategyConfig

    symbols = [
        s.strip().upper().replace("-", "")
        for s in args.symbols.split(",")
    ] if args.symbols else DEFAULT_SYMBOLS

    if args.start:
        start_date = args.start
    else:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=365)
        start_date = start_dt.strftime("%Y-%m-%d")

    end_date = args.end or datetime.now().strftime("%Y-%m-%d")

    config = StrategyConfig(
        base_risk_pct=args.risk / 100,
        max_leverage=args.leverage,
        pos_mult=args.pos_mult,
        min_rr=args.min_rr,
    )

    v2_tag = " (LLM Macro Gate ENABLED — Plutus V2)" if args.use_llm else ""

    print("=" * 60)
    print(f"PLUTUS BACKTESTER{v2_tag}")
    print("=" * 60)
    print(f"Symbols : {len(symbols)} coins")
    print(f"Period  : {start_date} → {end_date}")
    print(f"Market  : {args.market}")
    print(f"Equity  : ${args.equity:,.2f}")
    print(f"Risk    : {args.risk}%  |  Max Lev: {args.leverage}x")
    if args.use_llm:
        print(f"LLM     : {args.llm_provider} — Macro Risk Officer (Execution Gate)")
    print()

    result = run_backtest(
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        initial_equity=args.equity,
        config=config,
        market=args.market,
        use_llm=args.use_llm,
        llm_provider=args.llm_provider,
    )
    print(result["output"])
