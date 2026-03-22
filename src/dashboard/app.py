"""
PANOPTICON — Ghost Sniper
Institutional-grade trading dashboard with live terminal + forensics.
Run with: streamlit run src/dashboard/app.py
"""

from __future__ import annotations

import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import streamlit as st

# ── Authentication ────────────────────────────────────────────────────────────
_REQUIRED_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")
_QUERY_TOKEN    = None   # will be set on first request

def _authenticate():
    """Gate all dashboard access behind DASHBOARD_TOKEN."""
    global _QUERY_TOKEN
    if not _REQUIRED_TOKEN:
        return   # auth disabled when env var is absent
    if st.query_params.get("token") == _REQUIRED_TOKEN:
        _QUERY_TOKEN = _REQUIRED_TOKEN
        return
    st.set_page_config(page_title="GHOST SNIPER — Locked", layout="wide")
    st.error("🔒 Dashboard locked — provide the correct `?token=` query parameter.")
    st.info(f"Set the `DASHBOARD_TOKEN` environment variable to protect this dashboard.")
    st.stop()

_authenticate()
# ──────────────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

st.set_page_config(
    page_title="GHOST SNIPER — Panopticon",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
html, body, .stApp { background: #0a0e17 !important; color: #c9d1d9 !important; font-family: 'Courier New', monospace !important; }
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: #0a0e17; }
::-webkit-scrollbar-thumb { background: #21262d; border-radius: 2px; }
[data-testid="stMetricValue"] { font-size: 2.2rem !important; font-weight: 800 !important; }
[data-testid="stMetricLabel"] { font-size: 0.6rem !important; text-transform: uppercase; letter-spacing: 0.1em; color: #484f58 !important; }
.stButton > button[kind="primary"] { background: #238636 !important; color: white !important; border: none !important; border-radius: 8px !important; font-size: 1rem !important; font-weight: 700 !important; padding: 0.6rem 2rem !important; }
.stButton > button:hover { opacity: 0.85 !important; }
thead tr th { background: #161b22 !important; color: #8b949e !important; font-family: 'Courier New' !important; text-transform: uppercase; font-size: 0.6rem !important; }
tbody tr:hover { background: #161b22 !important; }
td { border-bottom: 1px solid #161b22 !important; font-family: 'Courier New' !important; font-size: 0.78rem !important; }
.ok  { color: #3fb950; font-weight: bold; }
.err { color: #f85149; font-weight: bold; }
.warn { color: #d29922; font-weight: bold; }
.info { color: #58a6ff; }
.bold { color: #e6edf3; font-weight: bold; }
.cmd  { color: #79c0ff; }
</style>
""", unsafe_allow_html=True)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _hl(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    u = line.upper()
    cls = ("err" if any(x in u for x in [
        "ERROR", "FAILED", "EXCEPTION", "TRACEBACK", "CRITICAL"])
        else "warn" if any(x in u for x in ["WARNING", "WARN", "⚠"])
        else "ok" if any(x in u for x in ["OK", "DONE", "✅", "WIN", "EXECUTED",
                                            "TRADING OPPORTUNITY", "SYNCED"])
        else "bold" if line.startswith("$") or u.startswith("NAV")
        else "info" if any(x in u for x in ["INFO", "STEP", "EVENT"])
        else "cmd" if line.startswith("$") or line.strip().startswith("python")
        else "")
    cls_attr = f" class='{cls}'" if cls else ""
    return f"<span{cls_attr}>{_esc(line)}</span>"


# ── Session state ─────────────────────────────────────────────────────────────
for _key, _val in [
    ("proc", None),
    ("term_lines", []),
    ("terminated", False),
    ("exec_start", None),
    ("live_events", []),
    ("live_trades", []),
    ("backtest_clicked", False),
]:
    if _key not in st.session_state:
        st.session_state[_key] = _val


# ── Forensics ────────────────────────────────────────────────────────────────

def _render_forensics(events, trades, equity):
    try:
        from src.dashboard.data_loader import (build_equity_curve, build_trade_table,
                                              compute_kpis)
        from src.dashboard.charts import build_equity_chart
    except Exception as exc:
        st.warning(f"Could not load forensics modules: {exc}")
        return

    if not events:
        return st.info("No forensics data yet — switch back to the **Terminal** tab to watch the backtest run.")

    kris = compute_kpis(events, initial_equity=equity)
    eq_df = build_equity_curve(events, initial_equity=equity)
    table_df = build_trade_table(trades)

    for col in ["Direction", "Result", "Anomaly"]:
        if col in table_df.columns:
            table_df[col] = table_df[col].apply(
                lambda x: re.sub(r"<[^>]+>", "", str(x)) if isinstance(x, str) else x
            )

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("NAV", f"${kris['nav']:,.2f}", delta=f"{kris['total_pnl']:+.2f}")
    k2.metric("WIN RATE",
              f"{kris['win_rate']:.1f}%",
              delta=f"{kris['wins']}W / {kris['losses']}L / {kris['holds']}H")
    pf = kris["profit_factor"]
    k3.metric("PROFIT FACTOR", f"{pf:.2f}" if pf != float("inf") else "∞")
    k4.metric("MAX DRAWDOWN",
              f"{kris['max_drawdown']:.2f}%",
              delta=f"{kris['max_drawdown']:.2f}%",
              delta_color="inverse")

    st.markdown("---")
    st.markdown("### EQUITY CURVE")
    fig = build_equity_chart(eq_df.to_dict("records"))
    fig.update_yaxes(autorange=True, rangemode="tozero", tickformat="$,.0f")
    fig.update_xaxes(autorange=True)
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("### TRADE LOG")
    cols = ["#", "Event", "Timestamp", "Anomaly", "Direction", "Entry ($)", "PnL ($)", "Result", "Confidence"]
    present = [c for c in cols if c in table_df.columns]
    st.dataframe(table_df[present], use_container_width=True, hide_index=True)

    st.markdown("### ANOMALY BREAKDOWN")
    c1, c2 = st.columns(2)
    with c1:
        try:
            from src.dashboard.charts import build_trade_distribution
            st.plotly_chart(build_trade_distribution(trades), use_container_width=True)
        except Exception:
            st.caption("Distribution unavailable")
    with c2:
        try:
            from src.dashboard.charts import build_correlation_heatmap
            weights = [
                {"SMC_ICT": t.get("weights", {}).get("SMC_ICT", 0),
                 "ORDER_FLOW": t.get("weights", {}).get("ORDER_FLOW", 0),
                 "MACRO_ONCHAIN": t.get("weights", {}).get("MACRO_ONCHAIN", 0)}
                for t in trades
            ]
            results = [{"trade_result": t.get("trade_result", "HOLD")} for t in trades]
            st.plotly_chart(build_correlation_heatmap(weights, results), use_container_width=True)
        except Exception:
            st.caption("Heatmap unavailable")


# ── Background threads ─────────────────────────────────────────────────────────

# Thread-safe queue for passing forensics updates from daemon threads to main thread.
# Writing session_state directly from daemon threads is unsafe; we push events onto
# this queue and drain it from the main thread instead.
_update_queue: queue.Queue = queue.Queue()
_logger = logging.getLogger("panopticon")


def _stream_stdout(proc):
    while True:
        line = proc.stdout.readline()
        if not line:
            break
        # Capture output lines via the queue so session_state is only touched
        # from the main thread.
        _update_queue.put(("stdout", line.rstrip()))
    proc.stdout.close()
    _update_queue.put(("terminated", True))


def _poll_json(proc):
    """Poll for new forensics data and push updates via the queue (thread-safe)."""
    from src.dashboard.data_loader import load_chronos_trades
    prev_len = 0
    while True:
        time.sleep(2.0)
        if proc.poll() is not None:
            break
        try:
            ev, tr, _ = load_chronos_trades()
            if ev and len(ev) != prev_len:
                prev_len = len(ev)
                _update_queue.put(("forensics", ev, tr))
        except Exception as exc:
            _logger.warning("Forensics poll failed: %s", exc)


def _drain_update_queue():
    """
    Drain all pending updates from _update_queue and apply them to session_state.
    Must be called from the main Streamlit thread on every script re-run.
    """
    try:
        while True:
            item = _update_queue.get_nowait()
            if item[0] == "stdout":
                _, line = item
                lines = st.session_state.term_lines
                lines.append(line)
                if len(lines) > 800:
                    lines[:] = lines[-800:]
            elif item[0] == "terminated":
                st.session_state.terminated = True
            elif item[0] == "forensics":
                _, ev, tr = item
                st.session_state.live_events = ev
                st.session_state.live_trades = tr
    except queue.Empty:
        pass


# ── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### ⚙ CONFIGURATION")

    symbol = st.selectbox("SYMBOL", [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT", "AVAXUSDT"
    ])

    st.markdown("**DATE RANGE**")
    c1, c2 = st.columns(2)
    with c1:
        start_date = st.date_input("START", value=date(2022, 5, 1), max_value=date.today())
    with c2:
        end_date   = st.date_input("END",   value=date(2022, 5, 3,))

    st.markdown("**CAPITAL**")
    initial_equity = st.number_input(
        "Equity ($)", min_value=10.0, max_value=10_000_000.0,
        value=50.0, step=10.0, format="%.2f",
    )

    st.markdown("**ENGINE**")
    min_conf    = st.slider("MIN CONFIDENCE", 0, 100, 60, 5)
    engine_mode = st.selectbox("MODE", ["dry_run", "live"])
    market     = st.selectbox("MARKET", ["futures", "spot"])

    st.markdown("---")
    st.button("🚀 EXECUTE GHOST SNIPER", type="primary", use_container_width=True,
              on_click=lambda: st.session_state.update(backtest_clicked=True))

    st.markdown("---")
    ev = len(st.session_state.get("live_events", []))
    tr = len(st.session_state.get("live_trades", []))
    st.caption(f"📁 {PROJECT_ROOT.name}/")
    st.caption(f"📊 {ev} events · {tr} trades")

# ── Backtest start (one-shot, on button click rerun) ────────────────────────────
if st.session_state.get("backtest_clicked"):
    st.session_state.backtest_clicked = False   # consume the click

    # Reset state
    st.session_state.term_lines   = []
    st.session_state.terminated   = False
    st.session_state.exec_start   = None
    st.session_state.live_events  = []
    st.session_state.live_trades  = []
    st.session_state.proc         = None

    # Sanitize symbol — only allow alphanumeric characters and common separators.
    # Symbol comes from a selectbox, so this is belt-and-suspenders against future changes.
    safe_symbol = re.sub(r"[^A-Za-z0-9,._-]", "", symbol).strip()
    if not safe_symbol:
        safe_symbol = "BTCUSDT"

    cmd = [
        sys.executable, "-m", "src.cli", "backtest",
        "--v3-chronos",
        "--v3-mode", engine_mode,
        "--symbols", safe_symbol,
        "--start", start_date.strftime("%Y-%m-%d"),
        "--end",   end_date.strftime("%Y-%m-%d"),
        "--v3-equity", str(initial_equity),
        "--v3-min-confidence", str(min_conf),
        "--market", market,
    ]

    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    st.session_state.proc       = proc
    st.session_state.exec_start = time.time()

    t1 = threading.Thread(target=_stream_stdout, args=(proc,), daemon=True, name="stdout")
    t2 = threading.Thread(target=_poll_json,        args=(proc,), daemon=True, name="json-poll")
    t1.start()
    t2.start()
    # Small sleep so threads have time to grab the proc reference
    time.sleep(0.3)

# ── Main layout ────────────────────────────────────────────────────────────────
# Drain any updates pushed by background threads — must run on every script re-run.
_drain_update_queue()

st.markdown("### GHOST SNIPER — PANOPTICON")

# ── D1: Dry-run warning banner ───────────────────────────────────────────────────
if engine_mode == "dry_run":
    st.warning(
        "⚠️  **DRY-RUN MODE** — Signals are synthetic and generated by deterministic "
        "mock personas. No real orders are placed. Results do **not** reflect live performance. "
        "Do not use these numbers to assess strategy viability.",
        icon="🚫",
    )

# Status row
proc     = st.session_state.get("proc")
running  = proc is not None and proc.poll() is None
elapsed  = f" ({time.time() - st.session_state.exec_start:.1f}s elapsed)" if st.session_state.get("exec_start") else ""
status_val = (
    "✅ DONE"
    if st.session_state.get("terminated")
    else f"⏳ RUNNING{elapsed}"
    if running
    else "⏸ IDLE"
)
st.metric("STATUS", status_val)

# Forensics tiles (mini KPIs, always visible below status)
live_ev = st.session_state.get("live_events", [])
live_tr = st.session_state.get("live_trades", [])
if live_ev:
    _render_forensics(live_ev, live_tr, initial_equity)
else:
    # No forensics data yet — show instructions
    if running:
        st.info(
            "⏳ Backtest is running in the terminal below — output streams live.\n\n"
            "Switch tabs to see forensics populate as trades are detected."
        )
    else:
        st.info("Configure parameters in the sidebar and click **🚀 EXECUTE GHOST SNIPER** to begin.")

st.markdown("---")
st.markdown("### LIVE TERMINAL")
st.markdown(
    "_Scroll inside for history. Output streams automatically while the backtest runs._"
)

# Terminal output block
lines = st.session_state.get("term_lines", [])
display = lines[-500:] if len(lines) > 500 else lines
st.markdown(
    "<pre style='background:#0d1117;border:1px solid #21262d;border-radius:6px;"
    "padding:12px;min-height:400px;max-height:70vh;overflow-y:auto;font-size:12px;'>"
    + "<br>".join(_hl(l) for l in display)
    + "</pre>",
    unsafe_allow_html=True,
)

# Footer
st.caption(
    f"Backtest logs → `logs/chronos_trades.json` · "
    f"Poll interval: 2s · "
    f"Ctrl+C in terminal to abort Streamlit."
)
