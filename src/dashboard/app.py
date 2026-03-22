"""
PANOPTICON — Plutus Ghost Sniper
Institutional-grade trading dashboard.
Run with: streamlit run src/dashboard/app.py
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

# ── Project root ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOGS_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data" / "historical"


# ── Helper functions ──────────────────────────────────────────────────────────

def format_currency(val: float | None) -> str:
    """Format a numeric value as USD currency string."""
    if val is None or (isinstance(val, float) and not math.isfinite(val)):
        return "—"
    return f"${val:,.2f}"


def format_pct(val: float | None) -> str:
    """Format a numeric value as a signed percentage string."""
    if val is None or (isinstance(val, float) and not math.isfinite(val)):
        return "—"
    return f"{val:+.2f}%"


def get_result_badge(result: str | None) -> str:
    """Return an HTML <span> with the appropriate CSS class for a trade result."""
    if result == "WIN":
        cls = "win"
        label = "WIN"
    elif result == "LOSS":
        cls = "loss"
        label = "LOSS"
    else:
        cls = "hold"
        label = result or "—"
    return f'<span class="{cls}">{label}</span>'


def get_direction_badge(direction: str | None) -> str:
    """Return an HTML <span> with the appropriate CSS class for a direction."""
    if direction == "LONG":
        cls = "signal-long"
        label = "LONG"
    elif direction == "SHORT":
        cls = "signal-short"
        label = "SHORT"
    else:
        cls = "signal-neutral"
        label = direction or "—"
    return f'<span class="{cls}">{label}</span>'


def calculate_equity_curve(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Build an equity curve from a list of trade events.

    Returns a list of dicts with keys:
        timestamp, equity, drawdown_pct
    Starting equity is read from config (default $10,000).
    """
    if not events:
        return []

    initial_equity = 10_000.0  # fallback

    rows: list[dict[str, Any]] = []
    equity = initial_equity
    peak = initial_equity

    for ev in events:
        pnl = ev.get("pnl") or 0.0
        equity += pnl
        peak = max(peak, equity)
        dd_pct = ((peak - equity) / peak * 100) if peak > 0 else 0.0
        rows.append(
            {
                "timestamp": ev.get("timestamp") or ev.get("event_time", ""),
                "equity": equity,
                "drawdown_pct": dd_pct,
            }
        )

    return rows


# ── Data loaders (cached) ─────────────────────────────────────────────────────

@st.cache_data
def load_chronos_data() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Load chronos_trades.json.
    Returns (raw_data, events, trades).
    """
    path = LOGS_DIR / "chronos_trades.json"
    if not path.exists():
        return {}, [], []

    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    config = raw.get("config", {})
    events = raw.get("events", [])
    trades = raw.get("trades", [])

    return config, events, trades


@st.cache_data
def load_llm_decisions() -> list[dict[str, Any]]:
    """Load llm_decisions.json (newline-delimited JSON)."""
    path = LOGS_DIR / "llm_decisions.json"
    if not path.exists():
        return []

    decisions: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    decisions.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return decisions


@st.cache_data
def load_btc_ohlcv(symbol: str = "BTCUSDT") -> pd.DataFrame:
    """
    Load OHLCV CSV for the given symbol.

    Expected columns (Binance export format):
        timestamp, open, high, low, close, volume,
        close_time, quote_asset_volume, number_of_trades,
        taker_buy_base_asset_volume, taker_buy_quote_asset_volume, ignore

    The CSV may store timestamps as ISO strings or as millisecond integers
    in a column named 'ts' or 'timestamp'.
    """
    path = DATA_DIR / f"{symbol}_1h.csv"
    if not path.exists():
        return pd.DataFrame()

    df = pd.read_csv(path)

    # Normalise timestamp column
    ts_col = None
    for col in ("ts", "timestamp", "open_time"):
        if col in df.columns:
            ts_col = col
            break

    if ts_col is not None:
        # Try parsing as ms-since-epoch first, fall back to ISO string
        try:
            df["ts"] = pd.to_datetime(df[ts_col], unit="ms")
        except Exception:
            df["ts"] = pd.to_datetime(df[ts_col], errors="coerce")

    numeric_cols = ["open", "high", "low", "close", "volume"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df.sort_values("ts").reset_index(drop=True)


# ── KRI metric helpers ────────────────────────────────────────────────────────

def compute_kris(events: list[dict[str, Any]], initial_equity: float = 10_000.0):
    """
    Compute the four Key Risk Indicator (KRI) metrics from trade events.

    Returns dict:
        nav, win_rate, profit_factor, max_drawdown
    """
    if not events:
        return {
            "nav": initial_equity,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
        }

    equity = initial_equity
    peak = initial_equity
    max_dd = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    wins = 0
    total_closed = 0

    for ev in events:
        pnl = ev.get("pnl") or 0.0
        result = ev.get("trade_result")

        equity += pnl
        peak = max(peak, equity)
        dd = ((peak - equity) / peak * 100) if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

        if result in ("WIN", "LOSS"):
            total_closed += 1
            if result == "WIN":
                wins += 1
                gross_profit += pnl
            else:
                gross_loss += abs(pnl)

    nav = equity
    win_rate = (wins / total_closed * 100) if total_closed > 0 else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_profit > 0 else 0.0
    )

    return {
        "nav": nav,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "max_drawdown": max_dd,
    }


# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="PANOPTICON — Plutus Ghost Sniper",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Custom CSS ────────────────────────────────────────────────────────────────

st.markdown(
    """
<style>
/* ── Reset ── */
html, body, .stApp { background: #0b0f19 !important; color: #e2e8f0 !important;
                     font-family: 'JetBrains Mono', 'Courier New', monospace !important; }

/* ── Metric cards ── */
div[data-testid="stMetricValue"] { font-size: 2rem !important; font-weight: 700 !important; }
div[data-testid="stMetricLabel"] { font-size: 0.75rem !important; text-transform: uppercase;
                                    letter-spacing: 0.05em; color: #64748b !important; }
[data-testid="stHorizontalBlock"] > div { background: #111827; border: 1px solid #1f2937;
                                           border-radius: 8px; padding: 12px 16px; margin: 4px; }

/* ── Tabs ── */
button[data-testid="stTab"] { font-size: 0.85rem !important; font-family: monospace !important;
                              text-transform: uppercase; letter-spacing: 0.05em; }
.stTabs [data-baseweb="tab-list"] { gap: 4px; background: #111827; border-radius: 6px;
                                    padding: 4px; border: 1px solid #1f2937; }
.stTabs [data-baseweb="tab"] { border-radius: 4px; }

/* ── Tables ── */
table { font-size: 0.8rem !important; }
thead tr th { background: #1f2937 !important; color: #94a3b8 !important;
              font-family: monospace !important; text-transform: uppercase; font-size: 0.7rem !important; }
tbody tr:hover { background: #1f2937 !important; }

/* ── Sidebar ── */
[data-testid="stSidebar"] { background: #0d1117 !important; border-right: 1px solid #1f2937; }

/* ── Expanders ── */
details { background: #111827; border: 1px solid #1f2937; border-radius: 6px;
          padding: 8px; margin-bottom: 8px; }

/* ── WIN/LOSS badges ── */
.win  { color: #10b981 !important; font-weight: 700; }
.loss { color: #ef4444 !important; font-weight: 700; }
.hold { color: #f59e0b !important; font-weight: 700; }

/* ── Signal pills ── */
.signal-long    { background: #10b98122; color: #10b981; border: 1px solid #10b981;
                  border-radius: 4px; padding: 2px 8px; font-size: 0.75rem; }
.signal-short   { background: #ef444422; color: #ef4444; border: 1px solid #ef4444;
                  border-radius: 4px; padding: 2px 8px; font-size: 0.75rem; }
.signal-neutral { background: #64748b22; color: #94a3b8; border: 1px solid #64748b;
                  border-radius: 4px; padding: 2px 8px; font-size: 0.75rem; }

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: #0b0f19; }
::-webkit-scrollbar-thumb { background: #1f2937; border-radius: 3px; }

/* ── Section headers ── */
.section-header { color: #94a3b8; font-size: 0.7rem; text-transform: uppercase;
                  letter-spacing: 0.1em; margin-bottom: 4px; }
</style>
""",
    unsafe_allow_html=True,
)


# ── Data loading ──────────────────────────────────────────────────────────────

config, events, trades = load_chronos_data()
llm_decisions = load_llm_decisions()
initial_equity = config.get("initial_equity", 10_000.0)

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("### PANOPTICON")
    st.markdown("---")

    symbol = st.selectbox("Symbol", ["BTCUSDT", "ETHUSDT"])
    st.caption(f"Loaded: {symbol} 1h OHLCV")

    anomaly_filter = st.selectbox(
        "Anomaly Filter",
        ["ALL", "LIQUIDITY_SWEEP", "EXTREME_DEVIATION", "VOLATILITY_SQUEEZE"],
    )

    confidence_threshold = st.slider("Confidence Threshold", 0, 100, 40, step=5)

    st.markdown("---")
    st.markdown("**Session Info**")
    st.text(f"Mode:          {config.get('mode', 'N/A').upper()}")
    st.text(f"Initial Equity:{format_currency(initial_equity)}")
    st.text(f"Lookback:      {config.get('lookback', 'N/A')} bars")
    st.text(f"Min Confidence:{config.get('min_confidence', 'N/A')}")

    st.markdown("---")
    st.markdown("**Data Availability**")
    st.text(f"Trade Events:  {len(events)}")
    st.text(f"Trades:         {len(trades)}")
    st.text(f"LLM Decisions: {len(llm_decisions)}")

    st.markdown("---")
    if st.button("Reload Data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()


# ── Guard: AWAITING DEPLOYMENT ─────────────────────────────────────────────────

if not events:
    st.error(
        "⚠  AWAITING DEPLOYMENT — No trade history found. Run a backtest first."
    )
    st.stop()


# ── Apply filters ─────────────────────────────────────────────────────────────

filtered_events = [
    ev
    for ev in events
    if (anomaly_filter == "ALL" or ev.get("anomaly") == anomaly_filter)
    and ev.get("confidence", 0) >= confidence_threshold
]

filtered_trades = [
    tr
    for tr in trades
    if (anomaly_filter == "ALL" or tr.get("anomaly_type") == anomaly_filter)
    and tr.get("confidence", 0) >= confidence_threshold
]


# ── Top HUD — KRI Bar ──────────────────────────────────────────────────────────

st.markdown("### KEY RISK INDICATORS")

kris = compute_kris(filtered_events, initial_equity=initial_equity)

col_nav, col_wr, col_pf, col_dd = st.columns(4)

# NAV
nav_val = kris["nav"]
nav_delta = nav_val - initial_equity
col_nav.metric(
    label="NAV",
    value=format_currency(nav_val),
    delta=format_pct(nav_delta / initial_equity * 100) if initial_equity else None,
)

# Win Rate
wr_val = kris["win_rate"]
col_wr.metric(
    label="Win Rate",
    value=f"{wr_val:.1f}%",
    delta=f"{len([e for e in filtered_events if e.get('trade_result')=='WIN'])} / {len([e for e in filtered_events if e.get('trade_result') in ('WIN','LOSS')])}",
)

# Profit Factor
pf_val = kris["profit_factor"]
pf_display = f"{pf_val:.2f}x" if math.isfinite(pf_val) else "∞"
col_pf.metric(label="Profit Factor", value=pf_display)

# Max Drawdown
dd_val = kris["max_drawdown"]
col_dd.metric(label="Max Drawdown", value=f"{dd_val:.2f}%")


# ── Equity Curve (mini sparkline via altair) ───────────────────────────────────

equity_curve = calculate_equity_curve(filtered_events)
if equity_curve:
    eq_df = pd.DataFrame(equity_curve)
    eq_df["ts"] = pd.to_datetime(eq_df["timestamp"], errors="coerce")
    eq_df = eq_df.dropna(subset=["ts"])

    st.markdown("#### Equity Curve")
    # Simple line chart via st.line_chart
    st.line_chart(
        data=eq_df.set_index("ts")["equity"],
        height=200,
        use_container_width=True,
    )


# ── Main Tabs ─────────────────────────────────────────────────────────────────

tab1, tab2 = st.tabs(["📊 Market Forensics", "📈 Risk & Portfolio"])

# ── Tab 1: Market Forensics ────────────────────────────────────────────────────

with tab1:
    sub_tab_events, sub_tab_trades, sub_tab_llm, sub_tab_ohlcv = st.tabs(
        ["Events", "Trades", "LLM Decisions", "OHLCV"]
    )

    # ── Events sub-tab ────────────────────────────────────────────────────────

    with sub_tab_events:
        st.markdown(f"Showing **{len(filtered_events)}** of **{len(events)}** events")

        if filtered_events:
            ev_df = pd.DataFrame(filtered_events)

            # Build display dataframe
            disp_rows = []
            for ev in filtered_events:
                disp_rows.append(
                    {
                        "#": ev.get("event_num", ""),
                        "Timestamp": ev.get("timestamp", "")[:19],
                        "Anomaly": ev.get("anomaly", ""),
                        "Direction": get_direction_badge(ev.get("direction")),
                        "Confidence": ev.get("confidence", ""),
                        "PnL": format_currency(ev.get("pnl", 0)),
                        "Result": get_result_badge(ev.get("trade_result")),
                        "Lev": f"{ev.get('leverage', 0)}x",
                        "Pos Size": format_currency(ev.get("position_value", 0)),
                        "Weights": str(
                            {k: round(v, 3) for k, v in (ev.get("weights") or {}).items()}
                        ),
                    }
                )
            ev_display = pd.DataFrame(disp_rows)

            st.dataframe(
                ev_display,
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No events match the current filters.")

        # Anomaly distribution
        if filtered_events:
            st.markdown("#### Anomaly Distribution")
            anomaly_counts = pd.Series(
                [e.get("anomaly", "UNKNOWN") for e in filtered_events]
            ).value_counts()
            st.bar_chart(anomaly_counts, height=180)

    # ── Trades sub-tab ─────────────────────────────────────────────────────────

    with sub_tab_trades:
        st.markdown(f"Showing **{len(filtered_trades)}** of **{len(trades)}** trades")

        if filtered_trades:
            tr_rows = []
            for tr in filtered_trades:
                sigs = tr.get("signals") or {}
                primary_sig = None
                if sigs:
                    # Show first available signal
                    for key in ("SMC_ICT", "ORDER_FLOW", "MACRO_ONCHAIN"):
                        if key in sigs:
                            primary_sig = sigs[key]
                            break

                tr_rows.append(
                    {
                        "#": tr.get("event_idx", ""),
                        "Timestamp": tr.get("timestamp", "")[:19],
                        "Anomaly": tr.get("anomaly_type", ""),
                        "Direction": get_direction_badge(tr.get("direction")),
                        "Confidence": tr.get("confidence", ""),
                        "Entry": format_currency(tr.get("entry_price", 0)),
                        "Stop": format_currency(tr.get("stop_loss", 0)),
                        "TP": format_currency(tr.get("take_profit", 0)),
                        "RR": f"{tr.get('rr_ratio', 0):.2f}" if tr.get("rr_ratio") else "—",
                        "Lev": f"{tr.get('leverage', 0)}x",
                        "PnL": format_currency(tr.get("pnl", 0)),
                        "Result": get_result_badge(tr.get("trade_result")),
                        "Thesis": (
                            (primary_sig.get("thesis") or "")[:120] + "…"
                            if primary_sig and primary_sig.get("thesis")
                            else "—"
                        ),
                    }
                )
            tr_display = pd.DataFrame(tr_rows)

            st.dataframe(
                tr_display,
                use_container_width=True,
                hide_index=True,
            )

            # Signal breakdown
            st.markdown("#### Signal Confidence Scores")
            sig_rows = []
            for tr in filtered_trades:
                sigs = tr.get("signals") or {}
                for sig_name, sig_data in sigs.items():
                    sig_rows.append(
                        {
                            "trade": tr.get("event_idx", ""),
                            "signal": sig_name,
                            "direction": get_direction_badge(sig_data.get("direction", "")),
                            "confidence": sig_data.get("confidence_score", ""),
                            "lev": f"{sig_data.get('recommended_leverage', 0)}x",
                            "thesis": (sig_data.get("thesis") or "")[:80] + "…",
                        }
                    )
            if sig_rows:
                sig_df = pd.DataFrame(sig_rows)
                st.dataframe(
                    sig_df,
                    use_container_width=True,
                    hide_index=True,
                )
        else:
            st.info("No trades match the current filters.")

    # ── LLM Decisions sub-tab ──────────────────────────────────────────────────

    with sub_tab_llm:
        st.markdown(f"**{len(llm_decisions)}** LLM decisions on record")

        if llm_decisions:
            # Summary stats
            llm_wins = sum(1 for d in llm_decisions if d.get("result") == "WIN")
            llm_losses = sum(1 for d in llm_decisions if d.get("result") == "LOSS")
            llm_no_trade = sum(1 for d in llm_decisions if d.get("result") not in ("WIN", "LOSS"))

            c1, c2, c3 = st.columns(3)
            c1.metric("Decisions", len(llm_decisions))
            c2.metric("Wins", llm_wins)
            c3.metric("Losses", llm_losses)

            # Decision breakdown
            decision_counts = pd.Series(
                [d.get("decision", "UNKNOWN") for d in llm_decisions]
            ).value_counts()
            st.bar_chart(decision_counts, height=180)

            st.markdown("#### Recent LLM Decisions")
            llm_rows = []
            for d in llm_decisions[-50:]:  # last 50
                llm_rows.append(
                    {
                        "Timestamp": d.get("timestamp", "")[:19],
                        "Symbol": d.get("symbol", ""),
                        "Decision": get_direction_badge(d.get("decision", "")),
                        "Order": d.get("order_type", ""),
                        "Entry": str(d.get("entry_price", "")),
                        "SL": str(d.get("stop_loss", "")),
                        "TP": str(d.get("take_profit", "")),
                        "RR": f"{d.get('rr', 0):.2f}" if d.get("rr") else "—",
                        "Risk": d.get("risk_level", ""),
                        "Result": get_result_badge(d.get("result")),
                        "Reason": (d.get("reason") or "")[:100] + "…",
                    }
                )
            llm_df = pd.DataFrame(llm_rows)
            st.dataframe(
                llm_df,
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No LLM decisions found.")

    # ── OHLCV sub-tab ─────────────────────────────────────────────────────────

    with sub_tab_ohlcv:
        btc_ohlcv = load_btc_ohlcv(symbol=symbol)

        if btc_ohlcv.empty:
            st.warning(f"No OHLCV data found for {symbol}. Check data path.")
        else:
            st.markdown(
                f"**{len(btc_ohlcv):,} candles** loaded — "
                f"{btc_ohlcv['ts'].min()} → {btc_ohlcv['ts'].max()}"
            )

            # Price chart
            st.markdown("#### Price Chart")
            price_chart_df = btc_ohlcv[["ts", "open", "high", "low", "close", "volume"]].copy()
            st.dataframe(
                price_chart_df.tail(100),
                use_container_width=True,
                hide_index=True,
            )

            # Basic stats
            st.markdown("#### Recent Price Statistics")
            recent = btc_ohlcv.tail(200).copy()
            if "close" in recent.columns and len(recent) > 0:
                latest_close = recent["close"].iloc[-1]
                prev_close = recent["close"].iloc[-2] if len(recent) > 1 else latest_close
                change = latest_close - prev_close
                change_pct = (change / prev_close * 100) if prev_close else 0
                high_200 = recent["high"].max()
                low_200 = recent["low"].min()

                sc1, sc2, sc3, sc4 = st.columns(4)
                sc1.metric("Latest Close", format_currency(latest_close), format_pct(change_pct))
                sc2.metric("200h High", format_currency(high_200))
                sc3.metric("200h Low", format_currency(low_200))
                vol_sum = recent["volume"].sum()
                sc4.metric("200h Volume", f"{vol_sum:,.0f}")


# ── Tab 2: Risk & Portfolio ───────────────────────────────────────────────────

with tab2:
    rk_col1, rk_col2 = st.columns(2)

    with rk_col1:
        st.markdown("#### Anomaly Performance Breakdown")

        if filtered_events:
            anomaly_stats: dict[str, dict[str, float]] = {}
            for ev in filtered_events:
                key = ev.get("anomaly", "UNKNOWN")
                if key not in anomaly_stats:
                    anomaly_stats[key] = {"pnl": 0.0, "wins": 0, "losses": 0, "count": 0}
                anomaly_stats[key]["pnl"] += ev.get("pnl", 0)
                anomaly_stats[key]["count"] += 1
                if ev.get("trade_result") == "WIN":
                    anomaly_stats[key]["wins"] += 1
                elif ev.get("trade_result") == "LOSS":
                    anomaly_stats[key]["losses"] += 1

            anomaly_rows = []
            for anomaly, stats in anomaly_stats.items():
                total = stats["count"]
                wins = stats["wins"]
                wr = (wins / total * 100) if total > 0 else 0.0
                anomaly_rows.append(
                    {
                        "Anomaly": anomaly,
                        "Count": total,
                        "Wins": wins,
                        "Losses": stats["losses"],
                        "Win Rate": f"{wr:.1f}%",
                        "Total PnL": format_currency(stats["pnl"]),
                        "Avg PnL": format_currency(stats["pnl"] / total if total else 0),
                    }
                )
            st.dataframe(
                pd.DataFrame(anomaly_rows),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No data for breakdown.")

    with rk_col2:
        st.markdown("#### Directional Performance")

        if filtered_events:
            dir_stats: dict[str, dict[str, float]] = {}
            for ev in filtered_events:
                key = ev.get("direction", "UNKNOWN")
                if key not in dir_stats:
                    dir_stats[key] = {"pnl": 0.0, "wins": 0, "losses": 0, "count": 0}
                dir_stats[key]["pnl"] += ev.get("pnl", 0)
                dir_stats[key]["count"] += 1
                if ev.get("trade_result") == "WIN":
                    dir_stats[key]["wins"] += 1
                elif ev.get("trade_result") == "LOSS":
                    dir_stats[key]["losses"] += 1

            dir_rows = []
            for direction, stats in dir_stats.items():
                total = stats["count"]
                wr = (stats["wins"] / total * 100) if total > 0 else 0.0
                dir_rows.append(
                    {
                        "Direction": get_direction_badge(direction),
                        "Count": total,
                        "Wins": stats["wins"],
                        "Losses": stats["losses"],
                        "Win Rate": f"{wr:.1f}%",
                        "Total PnL": format_currency(stats["pnl"]),
                    }
                )
            st.dataframe(
                pd.DataFrame(dir_rows),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No data for breakdown.")

    # ── Drawdown analysis ──────────────────────────────────────────────────────

    st.markdown("#### Drawdown Analysis")

    if equity_curve:
        dd_df = pd.DataFrame(equity_curve)
        dd_df["ts"] = pd.to_datetime(dd_df["timestamp"], errors="coerce")
        dd_df = dd_df.dropna(subset=["ts"])

        st.line_chart(
            data=dd_df.set_index("ts")["drawdown_pct"],
            height=200,
            use_container_width=True,
        )

        max_dd_idx = dd_df["drawdown_pct"].idxmax()
        if max_dd_idx is not None:
            max_dd_row = dd_df.loc[max_dd_idx]
            st.caption(
                f"Worst drawdown: **{max_dd_row['drawdown_pct']:.2f}%** "
                f"at {max_dd_row['timestamp']}"
            )
    else:
        st.info("No equity curve data available.")

    # ── LLM decision risk level breakdown ────────────────────────────────────

    if llm_decisions:
        st.markdown("#### LLM Risk Level Breakdown")
        risk_counts = pd.Series(
            [d.get("risk_level", "UNKNOWN") for d in llm_decisions]
        ).value_counts()
        st.bar_chart(risk_counts, height=180)

        # Result by risk level
        risk_result_rows = []
        for risk in ("LOW", "MODERATE", "HIGH"):
            sub = [d for d in llm_decisions if d.get("risk_level") == risk]
            wins = sum(1 for d in sub if d.get("result") == "WIN")
            losses = sum(1 for d in sub if d.get("result") == "LOSS")
            total = len(sub)
            wr = (wins / total * 100) if total > 0 else 0.0
            risk_result_rows.append(
                {
                    "Risk Level": risk,
                    "Count": total,
                    "Wins": wins,
                    "Losses": losses,
                    "Win Rate": f"{wr:.1f}%",
                }
            )
        st.dataframe(
            pd.DataFrame(risk_result_rows),
            use_container_width=True,
            hide_index=True,
        )

    # ── Position size / leverage analysis ─────────────────────────────────────

    if filtered_trades:
        st.markdown("#### Position & Leverage Analysis")

        lev_df = pd.DataFrame(
            [
                {
                    "trade": tr.get("event_idx", ""),
                    "direction": get_direction_badge(tr.get("direction", "")),
                    "leverage": tr.get("leverage", 0),
                    "position_value": tr.get("position_value", 0),
                    "pnl": tr.get("pnl", 0),
                    "result": get_result_badge(tr.get("trade_result")),
                }
                for tr in filtered_trades
            ]
        )
        st.dataframe(
            lev_df.sort_values("pnl", ascending=False),
            use_container_width=True,
            hide_index=True,
        )


# ── Footer ─────────────────────────────────────────────────────────────────────

st.markdown(
    "<hr style='border-color:#1f2937; margin-top:2rem;'/>"
    "<div style='text-align:center; color:#64748b; font-size:0.7rem;'>"
    "PANOPTICON v1.0 — Plutus Ghost Sniper | "
    f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    "</div>",
    unsafe_allow_html=True,
)
