from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .aggressive_strategy import AggressiveStrategy
from .engine import BacktestEngine
from .simple_fetch import fetch_binance_history


@dataclass(frozen=True)
class AggressiveParams:
    risk_pct: float
    max_leverage: float
    ema_fast: int
    ema_slow: int
    rsi_period: int
    rsi_long_max: float
    rsi_short_min: float
    stop_pct: float
    tp_multiple: float


@dataclass
class TrialResult:
    i: int
    sharpe: float
    total_pnl: float
    total_pnl_pct: float
    max_drawdown_pct: float
    total_trades: int
    win_rate: float
    params: AggressiveParams


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _sample_params(rng: random.Random, center: Optional[AggressiveParams]) -> AggressiveParams:
    if center is None:
        return AggressiveParams(
            risk_pct=_clamp(rng.uniform(0.01, 0.08), 0.001, 0.2),
            max_leverage=float(rng.choice([10, 20, 30, 50, 80, 100])),
            ema_fast=int(rng.choice([10, 12, 15, 20, 25, 30])),
            ema_slow=int(rng.choice([40, 50, 60, 80, 100])),
            rsi_period=int(rng.choice([7, 10, 14, 21])),
            rsi_long_max=_clamp(rng.uniform(50, 70), 40, 80),
            rsi_short_min=_clamp(rng.uniform(30, 50), 20, 60),
            stop_pct=_clamp(rng.uniform(0.005, 0.03), 0.002, 0.05),
            tp_multiple=_clamp(rng.uniform(1.5, 6.0), 1.0, 10.0),
        )

    def jitter(val: float, pct: float) -> float:
        return val * (1 + rng.uniform(-pct, pct))

    ema_fast = int(round(_clamp(jitter(center.ema_fast, 0.4), 5, 60)))
    ema_slow = int(round(_clamp(jitter(center.ema_slow, 0.35), 20, 200)))
    if ema_fast >= ema_slow:
        ema_slow = min(200, ema_fast + rng.choice([10, 15, 20, 30, 40]))

    return AggressiveParams(
        risk_pct=_clamp(jitter(center.risk_pct, 0.6), 0.001, 0.2),
        max_leverage=float(rng.choice([10, 20, 30, 50, 80, 100])),
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        rsi_period=int(rng.choice([7, 10, 14, 21])),
        rsi_long_max=_clamp(jitter(center.rsi_long_max, 0.2), 40, 80),
        rsi_short_min=_clamp(jitter(center.rsi_short_min, 0.25), 20, 60),
        stop_pct=_clamp(jitter(center.stop_pct, 0.5), 0.002, 0.05),
        tp_multiple=_clamp(jitter(center.tp_multiple, 0.5), 1.0, 10.0),
    )


def _run_backtest_on_data(
    data: Dict[str, List[dict]],
    symbols: List[str],
    initial_equity: float,
    params: AggressiveParams,
) -> TrialResult:
    engine = BacktestEngine(initial_equity)
    strategy = AggressiveStrategy(**asdict(params))

    min_len = min(len(data[s]) for s in symbols)
    for i in range(50, min_len):
        ts = data[symbols[0]][i]["timestamp"]
        current_time = data[symbols[0]][i]["datetime"]
        for sym in symbols:
            strategy.execute(
                engine,
                sym.replace("USDT", "-USDT"),
                {"1h": data[sym][: i + 1]},
                current_time,
                ts,
            )

    result = engine.get_results(final_prices={sym.replace("USDT", "-USDT"): data[sym][-1]["close"] for sym in symbols})

    return TrialResult(
        i=-1,
        sharpe=float(result.sharpe_ratio),
        total_pnl=float(result.total_pnl),
        total_pnl_pct=float(result.total_pnl_pct),
        max_drawdown_pct=float(result.max_drawdown_pct),
        total_trades=int(result.total_trades),
        win_rate=float(result.win_rate),
        params=params,
    )


def optimize_aggressive_strategy(
    symbols: Optional[List[str]] = None,
    start_date: str = "2026-02-01",
    end_date: str = "2026-03-02",
    initial_equity: float = 10000,
    iterations: int = 60,
    sharpe_target: float = 10.0,
    seed: int = 7,
    log_dir: str = "logs/backtest",
) -> Tuple[TrialResult, List[TrialResult], Path]:
    if symbols is None:
        symbols = ["BTCUSDT"]

    rng = random.Random(seed)

    data: Dict[str, List[dict]] = {}
    for sym in symbols:
        data[sym] = fetch_binance_history(sym, "1h", start_date, end_date, 5000)

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_path = Path(log_dir) / f"aggressive_opt_{datetime.now().strftime('%Y-%m-%d')}.jsonl"

    best: Optional[TrialResult] = None
    history: List[TrialResult] = []
    center: Optional[AggressiveParams] = None

    for i in range(iterations):
        params = _sample_params(rng, center)
        trial = _run_backtest_on_data(data, symbols, initial_equity, params)
        trial.i = i
        history.append(trial)

        if best is None or trial.sharpe > best.sharpe:
            best = trial
            center = trial.params

        with open(log_path, "a") as f:
            f.write(json.dumps({
                "i": trial.i,
                "sharpe": trial.sharpe,
                "total_pnl": trial.total_pnl,
                "total_pnl_pct": trial.total_pnl_pct,
                "max_drawdown_pct": trial.max_drawdown_pct,
                "total_trades": trial.total_trades,
                "win_rate": trial.win_rate,
                "params": asdict(trial.params),
            }) + "\n")

        if best.sharpe >= sharpe_target:
            break

    if best is None:
        raise RuntimeError("No trials executed")

    return best, history, log_path


if __name__ == "__main__":
    best, history, log_path = optimize_aggressive_strategy()
    print(f"trials={len(history)}")
    print(f"log={log_path}")
    print(f"best_sharpe={best.sharpe}")
    print(f"best_total_pnl={best.total_pnl}")
    print(f"best_total_pnl_pct={best.total_pnl_pct}")
    print(f"best_max_drawdown_pct={best.max_drawdown_pct}")
    print(f"best_total_trades={best.total_trades}")
    print(f"best_win_rate={best.win_rate}")
    print(f"best_params={best.params}")
