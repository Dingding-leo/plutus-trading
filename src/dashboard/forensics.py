"""
Post-Trade Forensics Tab.
Renders market forensics and AI reasoning audit for the Ghost Sniper dashboard.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
import sys
sys.path.insert(0, str(PROJECT_ROOT))

# ─── Data loading (lazy to avoid hard dependency) ───────────────────────────

def _load_trades():
    """Load chronos trades. Returns (events, trades, config) or (None, None, None)."""
    try:
        from src.dashboard.data_loader import load_chronos_trades, build_equity_curve, compute_kpis
        events, trades, cfg = load_chronos_trades()
        return events, trades, cfg, build_equity_curve, compute_kpis
    except Exception:
        return None, None, None, None, None


def _load_charts():
    """Load chart builders."""
    try:
        from src.dashboard.charts import (
            build_equity_chart,
            build_trade_distribution,
            build_correlation_heatmap,
        )
        return build_equity_chart, build_trade_distribution, build_correlation_heatmap
    except Exception:
        return None, None, None


def render_forensics_tab(tab):
    """
    Render the forensics tab inside an st.tab() container.
    Requires the tab object passed in (e.g., tab2 from st.tabs([..., tab2])).
    """
    # ── Section 1: Guard ──────────────────────────────────────────────────────
    events, trades, cfg, eq_builder, kpi_builder = _load_trades()
    if events is None:
        with tab:
            st.info("⬡ No trade history. Run a backtest first.")
        return

    # ── Section 2: KRI Bar ────────────────────────────────────────────────────
    kris = kpi_builder(events)
    eq_df = eq_builder(events)

    with tab:
        k1, k2, k3, k4 = st.columns(4)
        k1.metric(
            "NAV ($)",
            f"${kris['nav']:,.2f}",
            delta=f"{kris['total_pnl']:+.2f} total",
        )
        k2.metric(
            "WIN RATE",
            f"{kris['win_rate']:.1f}%",
            delta=f"{kris['wins']}W / {kris['losses']}L / {kris['holds']}H",
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

        # ── Section 3: Equity Curve ───────────────────────────────────────────
        st.markdown("### EQUITY CURVE")
        eq_fig = build_equity_chart(eq_df.to_dict("records"))
        eq_fig.update_yaxes(autorange=True, rangemode="tozero", tickformat="$,.0f")
        eq_fig.update_xaxes(autorange=True)
        st.plotly_chart(eq_fig, use_container_width=True)

        # ── Section 4: Trades Table ────────────────────────────────────────────
        st.markdown("### TRADE LOG")
        from src.dashboard.data_loader import build_trade_table

        table_df = build_trade_table(trades)
        st.dataframe(table_df, use_container_width=True, hide_index=True)

        # ── Section 5: Anomaly Breakdown ──────────────────────────────────────
        st.markdown("### ANOMALY BREAKDOWN")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**WIN / LOSS DISTRIBUTION**")
            dist_fig = build_trade_distribution(trades)
            st.plotly_chart(dist_fig, use_container_width=True)
        with c2:
            st.markdown("**PERSONA WEIGHT × OUTCOME**")
            weights = [
                {
                    "SMC_ICT": t.get("weights", {}).get("SMC_ICT", 0),
                    "ORDER_FLOW": t.get("weights", {}).get("ORDER_FLOW", 0),
                    "MACRO_ONCHAIN": t.get("weights", {}).get("MACRO_ONCHAIN", 0),
                }
                for t in trades
            ]
            results = [{"trade_result": t.get("trade_result", "HOLD")} for t in trades]
            heat_fig = build_correlation_heatmap(weights, results)
            st.plotly_chart(heat_fig, use_container_width=True)

        # ── Section 6: Streak & Performance Metrics ───────────────────────────
        st.markdown("### PERFORMANCE SUMMARY")

        streak_data = []
        streak, max_win, max_loss = 0, 0, 0
        for e in sorted(events, key=lambda x: x.get("timestamp", "")):
            r = e.get("trade_result", "HOLD")
            if r == "WIN":
                streak = max(streak, 0) + 1
                max_win = max(max_win, streak)
            elif r == "LOSS":
                streak = min(streak, 0) - 1
                max_loss = min(max_loss, streak)
            else:
                streak = 0
            streak_data.append(
                {"timestamp": e.get("timestamp", ""), "result": r, "streak": streak}
            )

        m1, m2, m3, m4, m5, m6 = st.columns(6)
        closed = [e for e in events if e.get("trade_result") in ("WIN", "LOSS")]
        wins   = [e for e in closed if e.get("trade_result") == "WIN"]
        losses = [e for e in closed if e.get("trade_result") == "LOSS"]

        m1.metric("Total Trades", len(closed))
        m2.metric("Wins", len(wins))
        m3.metric("Losses", len(losses))
        m4.metric(
            "Best Win",
            f"${max((e.get('pnl', 0) for e in wins), default=0):.2f}",
        )
        m5.metric(
            "Worst Loss",
            f"${min((e.get('pnl', 0) for e in losses), default=0):.2f}",
        )
        m6.metric(
            "Gross P&L",
            f"${sum(e.get('pnl', 0) for e in closed):+.2f}",
        )
