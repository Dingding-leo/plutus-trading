"""
Data loaders for the Panopticon Dashboard.
Cached with @st.cache_data when running inside Streamlit,
falls back to @functools.lru_cache when run standalone.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import List, Dict

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ─── Cache decorator (Streamlit-aware) ──────────────────────────────────────

try:
    import streamlit as st
    _cache = functools.lru_cache(maxsize=4)
    # Wrap @lru_cache so the signature matches @st.cache_data(ttl=...)
    def _cached(ttl=3600):
        return _cache
    _USE_STREAMLIT = True
except ModuleNotFoundError:
    _USE_STREAMLIT = False


def _cache_decorator(ttl=3600):
    """@st.cache_data when in Streamlit, @functools.lru_cache otherwise."""
    if _USE_STREAMLIT:
        return st.cache_data(ttl=ttl)
    return functools.lru_cache(maxsize=4)


# ─── Core loaders ─────────────────────────────────────────────────────────────


@_cache_decorator(ttl=3600)
def load_chronos_trades() -> tuple[List[dict], List[dict], dict]:
    """
    Load logs/chronos_trades.json and return (events, trades, config).
    Returns empty lists if file not found.
    """
    path = PROJECT_ROOT / "logs" / "chronos_trades.json"
    if not path.exists():
        return [], [], {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("events", []), data.get("trades", []), data.get("config", {})


@_cache_decorator(ttl=3600)
def load_btc_ohlcv(symbol: str = "BTCUSDT") -> pd.DataFrame:
    """
    Load hourly OHLCV data from data/historical/{symbol}_1h.csv.
    Returns DataFrame with ts (datetime) column.

    The CSV has timestamp as a datetime string (not ms integer),
    so parse with pd.to_datetime directly.
    """
    path = PROJECT_ROOT / "data" / "historical" / f"{symbol}_1h.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    # Timestamp is already a datetime string, not ms integer
    df["ts"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("ts").reset_index(drop=True)


@_cache_decorator(ttl=3600)
def load_llm_decisions() -> pd.DataFrame:
    """
    Load logs/llm_decisions.json (newline-delimited JSON).
    Returns DataFrame with all LLM decision entries.
    """
    path = PROJECT_ROOT / "logs" / "llm_decisions.json"
    if not path.exists():
        return pd.DataFrame()
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return pd.DataFrame(records)


@_cache_decorator(ttl=3600)
def load_btcusdt_backtest() -> tuple[List[dict], List[dict], dict]:
    """Load the larger 2021-2024 backtest."""
    path = PROJECT_ROOT / "logs" / "btcusdt_2021_2025_backtest.json"
    if not path.exists():
        return [], [], {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("events", []), data.get("trades", []), data.get("config", {})


# ─── Derived data builders ────────────────────────────────────────────────────


def build_equity_curve(
    events: List[dict], initial_equity: float = 10_000.0
) -> pd.DataFrame:
    """
    Calculate cumulative equity curve from events.

    Returns DataFrame with:
    timestamp, equity, peak, drawdown_pct, pnl,
    trade_result, event_num, direction, anomaly, confidence
    """
    rows = []
    equity = initial_equity
    peak = initial_equity

    for e in sorted(events, key=lambda x: x["timestamp"]):
        pnl = e.get("pnl", 0.0) or 0.0
        if e.get("trade_result") in ("WIN", "LOSS"):
            equity += pnl
        peak = max(peak, equity)
        drawdown = (equity - peak) / peak * 100 if peak > 0 else 0.0

        rows.append(
            {
                "event_num": e["event_num"],
                "timestamp": e["timestamp"],
                "equity": round(equity, 2),
                "peak": round(peak, 2),
                "drawdown_pct": round(drawdown, 3),
                "pnl": round(pnl, 2),
                "trade_result": e.get("trade_result", "HOLD"),
                "direction": e.get("direction", "?"),
                "anomaly": e.get("anomaly", "?"),
                "confidence": e.get("confidence", 0),
            }
        )

    return pd.DataFrame(rows)


def compute_kpis(events: List[dict], initial_equity: float = 10_000.0) -> dict:
    """
    Compute key performance indicators from events.

    Returns dict with:
    nav, win_rate, profit_factor, max_drawdown,
    total_trades, total_pnl, wins, losses, holds
    """
    closed = [e for e in events if e.get("trade_result") in ("WIN", "LOSS")]
    wins = [e for e in closed if e.get("trade_result") == "WIN"]
    losses = [e for e in closed if e.get("trade_result") == "LOSS"]
    holds = [e for e in events if e.get("trade_result") == "HOLD"]

    equity_curve = build_equity_curve(events, initial_equity)
    max_dd = float(equity_curve["drawdown_pct"].min()) if len(equity_curve) else 0.0

    gross_profit = sum(e.get("pnl", 0) or 0 for e in wins)
    gross_loss = abs(sum(e.get("pnl", 0) or 0 for e in losses))

    if gross_loss > 0:
        pf = gross_profit / gross_loss
    elif gross_profit > 0:
        pf = float("inf")
    else:
        pf = 0.0

    return {
        "nav": round(
            float(equity_curve["equity"].iloc[-1]) if len(equity_curve) else initial_equity,
            2,
        ),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "profit_factor": round(pf, 2),
        "max_drawdown": round(max_dd, 2),
        "total_trades": len(closed),
        "total_pnl": round(sum(e.get("pnl", 0) or 0 for e in closed), 2),
        "wins": len(wins),
        "losses": len(losses),
        "holds": len(holds),
    }


def build_trade_table(trades: List[dict]) -> pd.DataFrame:
    """
    Build a display table from the raw trades list.
    Columns: #, Event, Timestamp, Anomaly, Direction, Entry($), SL($), TP($), RR, PnL($), Result, Confidence
    All numeric columns are floats or ints for sorting; display columns are formatted strings.
    """
    rows = []
    for i, t in enumerate(trades):
        rows.append({
            "#": i + 1,
            "Event": t.get("event_idx", "?"),
            "Timestamp": t.get("timestamp", "?"),
            "Anomaly": t.get("anomaly_type", "?"),
            "Direction": t.get("direction", "?"),
            "Entry ($)": round(float(t.get("entry_price") or 0), 2),
            "SL ($)": round(float(t.get("stop_loss") or 0), 2),
            "TP ($)": round(float(t.get("take_profit") or 0), 2),
            "RR": round(float(t.get("rr_ratio") or 0), 2),
            "PnL ($)": round(float(t.get("pnl") or 0), 2),
            "Result": t.get("trade_result", "HOLD"),
            "Confidence": int(t.get("confidence") or 0),
            # Hidden raw floats for sorting
            "_pnl_raw": float(t.get("pnl") or 0),
            "_confidence_raw": int(t.get("confidence") or 0),
        })
    return pd.DataFrame(rows)
