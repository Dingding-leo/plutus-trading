"""
PANOPTICON — Plutus Ghost Sniper
Institutional-grade trading dashboard.
Run with: streamlit run src/dashboard/app.py
"""

import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import streamlit as st

# ── Ensure project root is on sys.path before any src.* import ─────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ── Page config ─────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="GHOST SNIPER — Panopticon Terminal",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── CSS injection ───────────────────────────────────────────────────────────────
st.markdown(
    """
<style>
/* ── Reset ── */
html, body, .stApp { background: #0a0e17 !important; color: #c9d1d9 !important; font-family: 'Courier New', monospace !important; }

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: #0a0e17; }
::-webkit-scrollbar-thumb { background: #21262d; border-radius: 2px; }

/* ── Metric labels ── */
div[data-testid="stMetricValue"] { font-size: 2.2rem !important; font-weight: 800 !important; letter-spacing: -0.02em; }
div[data-testid="stMetricLabel"] { font-size: 0.65rem !important; text-transform: uppercase; letter-spacing: 0.1em; color: #484f58 !important; }

/* ── Metric cards ── */
div[data-testid="stHorizontalBlock"] > div {
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 6px;
    padding: 16px 20px;
}

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {
    background: #161b22;
    border-radius: 6px;
    padding: 4px;
    border: 1px solid #21262d;
}
button[data-testid="stTab"] {
    font-size: 0.8rem !important;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-family: 'Courier New', monospace !important;
}

/* ── Buttons ── */
.stButton > button[kind="primary"] {
    background: #238636 !important;
    color: white !important;
    border: none !important;
    border-radius: 6px !important;
    font-family: 'Courier New', monospace !important;
    font-size: 1rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.05em !important;
    padding: 0.6rem 1.5rem !important;
}
.stButton > button:hover { opacity: 0.9 !important; }
.stButton > button:active { opacity: 0.7 !important; }

/* ── Code blocks ── */
pre, code { background: #0d1117 !important; color: #79c0ff !important; }
.stCodeBlock { background: #0d1117 !important; border: 1px solid #21262d !important; border-radius: 6px !important; }

/* ── Sidebar ── */
[data-testid="stSidebar"] { background: #0d1117 !important; border-right: 1px solid #21262d; }

/* ── Tables ── */
thead tr th {
    background: #161b22 !important;
    color: #8b949e !important;
    font-family: 'Courier New' !important;
    text-transform: uppercase;
    font-size: 0.65rem !important;
    letter-spacing: 0.08em;
    border-bottom: 1px solid #21262d !important;
}
tbody tr:hover { background: #161b22 !important; }
td { border-bottom: 1px solid #161b22 !important; font-family: 'Courier New' !important; font-size: 0.8rem !important; }

/* ── WIN/LOSS/NEUTRAL badges ── */
.badge-win  { color: #3fb950; font-weight: 700; }
.badge-loss { color: #f85149; font-weight: 700; }
.badge-hold { color: #d29922; font-weight: 700; }
.badge-long  { color: #3fb950; }
.badge-short { color: #f85149; }
.badge-neutral { color: #8b949e; }

/* ── Spinners / progress ── */
.stSpinner > div { border-top-color: #238636 !important; }

/* ── Success / error banners ── */
.stAlert { border-radius: 6px !important; }

/* ── Terminal line ── */
.terminal-line { font-family: 'Courier New', monospace; font-size: 0.75rem; color: #8b949e; line-height: 1.4; }
.terminal-line .ok   { color: #3fb950; }
.terminal-line .warn { color: #d29922; }
.terminal-line .err  { color: #f85149; }
.terminal-line .info { color: #58a6ff; }
.terminal-line .bold { color: #e6edf3; font-weight: 700; }

/* ── Plot background ── */
.js-plotly-plot .plotly { background: #0d1117 !important; }
</style>
""",
    unsafe_allow_html=True,
)

# ── Header ─────────────────────────────────────────────────────────────────────
st.markdown(
    """
<div style="border-bottom: 1px solid #21262d; padding-bottom: 12px; margin-bottom: 20px;">
    <span style="color:#3fb950; font-size:1.4rem; font-weight:800; letter-spacing:0.1em;">⬡ GHOST SNIPER</span>
    <span style="color:#484f58; font-size:0.9rem; margin-left:16px;">PANOPTICON TERMINAL v3.1</span>
    <span style="color:#21262d; float:right; font-size:0.7rem;">AUSTIN LIU | PLUTUS TRADING SYSTEM</span>
</div>
""",
    unsafe_allow_html=True,
)

# ── Helper: ANSI-inspired highlighting ─────────────────────────────────────────


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _highlight(line: str) -> str:
    """Apply ANSI-inspired color classes to a terminal output line."""
    line = line.strip()
    if any(x in line.upper() for x in ["ERROR", "FAILED", "EXCEPTION", "TRACE", "CRITICAL"]):
        return f"<span class='err'>{_escape_html(line)}</span>"
    if any(x in line.upper() for x in ["WARNING", "WARN", "⚠"]):
        return f"<span class='warn'>{_escape_html(line)}</span>"
    if any(
        x in line.upper()
        for x in ["OK", "DONE", "SUCCESS", "✅", "WIN", "TRADING OPPORTUNITY", "EXECUTED"]
    ):
        return f"<span class='ok'>{_escape_html(line)}</span>"
    if "→" in line or "│" in line or "━" in line or "═" in line:
        return f"<span class='info'>{_escape_html(line)}</span>"
    if line.startswith("$") or line.startswith("NAV:") or line.startswith("P&L"):
        return f"<span class='bold'>{_escape_html(line)}</span>"
    return _escape_html(line)


# ── Forensics tab ──────────────────────────────────────────────────────────────


def _render_forensics():
    """Render post-trade forensics tab using existing data_loader and charts."""
    import pandas as pd

    try:
        from src.dashboard.data_loader import (
            build_equity_curve,
            build_trade_table,
            compute_kpis,
            load_chronos_trades,
        )
        from src.dashboard.charts import build_equity_chart
    except Exception as exc:
        st.warning(f"Could not load forensics modules: {exc}")
        return

    events, trades, cfg = load_chronos_trades()
    if not events:
        st.info(
            "⬡ No trade history found. Run a backtest first using the "
            "**Live Execution Terminal** tab."
        )
        return

    # ── KRI bar ──────────────────────────────────────────────────────────────
    initial_equity = cfg.get("initial_equity", 50.0)
    kris = compute_kpis(events, initial_equity=initial_equity)
    eq_df = build_equity_curve(events, initial_equity=initial_equity)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric(
        "NAV",
        f"${kris['nav']:,.2f}",
        delta=f"{kris['total_pnl']:+.2f}",
    )
    k2.metric(
        "WIN RATE",
        f"{kris['win_rate']:.1f}%",
        delta=f"{kris['wins']}W / {kris['losses']}L",
    )
    pf = kris["profit_factor"]
    k3.metric(
        "PROFIT FACTOR",
        f"{pf:.2f}" if pf != float("inf") else "∞",
    )
    k4.metric(
        "MAX DRAWDOWN",
        f"{kris['max_drawdown']:.2f}%",
        delta=f"{kris['max_drawdown']:.2f}%",
        delta_color="inverse",
    )

    st.markdown("---")

    # ── Equity curve ─────────────────────────────────────────────────────────
    st.markdown("### EQUITY CURVE")
    eq_records = eq_df.to_dict("records")
    eq_fig = build_equity_chart(eq_records)
    # Force autorange so Y-axis zooms to actual data range
    eq_fig.update_yaxes(autorange=True)
    eq_fig.update_xaxes(autorange=True)
    st.plotly_chart(eq_fig, use_container_width=True)

    # ── Trade log table ──────────────────────────────────────────────────────
    st.markdown("### TRADE LOG")
    table_df = build_trade_table(trades)
    # Strip any HTML tags from display columns before rendering
    for col in ["Direction", "Result", "Anomaly"]:
        if col in table_df.columns:
            table_df[col] = table_df[col].apply(
                lambda x: re.sub(r"<[^>]+>", "", str(x)) if isinstance(x, str) else x
            )
    st.dataframe(table_df, use_container_width=True, hide_index=True)


# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙ CONFIGURATION")

    # ── Symbol ──────────────────────────────────────────────────────────────
    symbol = st.selectbox(
        "SYMBOL",
        ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT", "AVAXUSDT"],
    )

    # ── Date range ──────────────────────────────────────────────────────────
    st.markdown("**DATE RANGE**")
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input(
            "START", value=date(2022, 5, 1), max_value=date.today()
        )
    with col2:
        end_date = st.date_input(
            "END", value=date.today(), max_value=date.today()
        )

    # ── Capital ─────────────────────────────────────────────────────────────
    st.markdown("**CAPITAL**")
    initial_equity = st.number_input(
        "Initial Equity ($)",
        min_value=10.0,
        max_value=1_000_000.0,
        value=50.0,
        step=10.0,
        format="%.2f",
    )

    # ── Engine settings ────────────────────────────────────────────────────
    st.markdown("**ENGINE**")
    min_confidence = st.slider("MIN CONFIDENCE", 0, 100, 60, 5)
    engine_mode = st.selectbox("MODE", ["dry_run", "live"], index=0)
    market = st.selectbox("MARKET", ["futures", "spot"], index=0)

    # ── Execute ─────────────────────────────────────────────────────────────
    st.markdown("")
    RUN = st.button(
        "🚀 EXECUTE GHOST SNIPER", type="primary", use_container_width=True
    )

    # ── Status footer ───────────────────────────────────────────────────────
    st.markdown("---")
    st.caption(f"📁 {os.getcwd()}")


# ── Two-tab layout ─────────────────────────────────────────────────────────────
tab_terminal, tab_forensics = st.tabs(
    ["⚡ Live Execution Terminal", "📊 Post-Trade Forensics"]
)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Live Execution Terminal
# ══════════════════════════════════════════════════════════════════════════════
with tab_terminal:
    st.markdown("#### TERMINAL OUTPUT")

    terminal_display = st.empty()

    if RUN:
        # ── Build CLI command ────────────────────────────────────────────────
        cmd = [
            sys.executable,
            "-m",
            "src.cli",
            "backtest",
            "--v3-chronos",
            "--v3-mode",
            engine_mode,
            "--symbols",
            symbol,
            "--start",
            start_date.strftime("%Y-%m-%d"),
            "--end",
            end_date.strftime("%Y-%m-%d"),
            "--v3-equity",
            str(initial_equity),
            "--v3-min-confidence",
            str(min_confidence),
            "--market",
            market,
        ]

        terminal_lines = []

        # ── Status bar ───────────────────────────────────────────────────────
        status_col1, status_col2, status_col3 = st.columns(3)
        status_col1.metric("STATUS", "⏳ RUNNING")
        status_col2.metric("MODE", engine_mode.upper())
        status_col3.metric("EQUITY", f"${initial_equity:,.0f}")

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(PROJECT_ROOT),
                env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
            )

            # Stream output line-by-line
            for raw_line in iter(process.stdout.readline, ""):
                if not raw_line:
                    break
                terminal_lines.append(raw_line)
                # Keep last 300 lines so the DOM doesn't grow unbounded
                window = terminal_lines[-300:]
                terminal_display.markdown(
                    "<div class='terminal-line'>"
                    + "".join(f"<div>{_highlight(line.rstrip())}</div>" for line in window)
                    + "</div>",
                    unsafe_allow_html=True,
                )

            process.stdout.close()
            return_code = process.wait()

            final_status = "✅ DONE" if return_code == 0 else "❌ FAILED"
            status_col1.metric("STATUS", final_status)

            if return_code != 0:
                st.error(f"Process exited with code {return_code}")

        except FileNotFoundError:
            st.error(f"Python interpreter not found: `{sys.executable}`")
        except Exception as e:
            st.error(f"Execution failed: {e}")

    else:
        terminal_display.info(
            "⬡ Configure parameters in the sidebar and click "
            "**EXECUTE GHOST SNIPER** to begin."
        )


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Post-Trade Forensics
# ══════════════════════════════════════════════════════════════════════════════
with tab_forensics:
    _render_forensics()
