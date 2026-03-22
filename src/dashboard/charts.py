"""
src/dashboard/charts.py
Chart Master — all Plotly chart builders for the Plutus Streamlit dashboard.
"""

from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ema(prices: np.ndarray, period: int) -> np.ndarray:
    """Calculate exponential moving average in-place."""
    multiplier = 2 / (period + 1)
    ema = np.zeros(len(prices))
    ema[: period - 1] = np.nan
    ema[period - 1] = np.mean(prices[:period])
    for i in range(period, len(prices)):
        ema[i] = (prices[i] - ema[i - 1]) * multiplier + ema[i - 1]
    return ema


# ---------------------------------------------------------------------------
# Chart 1 — Equity Curve + Underwater Drawdown
# ---------------------------------------------------------------------------

def build_equity_chart(equity_curve: List[dict]) -> go.Figure:
    """
    Two-row subplot:
      row 1 — Net Asset Value (NAV) step line with fill
      row 2 — drawdown percentage with colour bands

    Parameters
    ----------
    equity_curve : list[dict]
        Dicts with keys: timestamp, equity, drawdown_pct, trade_result

    Returns
    -------
    plotly.graph_objects.Figure
    """
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        row_heights=[0.7, 0.3],
        subplot_titles=("", ""),
    )

    timestamps = [e["timestamp"] for e in equity_curve]
    nav_values  = [e["equity"] for e in equity_curve]
    dd_values   = [e["drawdown_pct"] for e in equity_curve]

    # ── Row 1: NAV ──────────────────────────────────────────────────────────
    fig.add_trace(
        go.Scatter(
            x=timestamps,
            y=nav_values,
            mode="lines",
            name="NAV",
            line=dict(color="#10b981", width=2),
            fill="tozeroy",
            fillcolor="rgba(16,185,129,0.1)",
            hovertemplate="<b>%{x}</b><br>NAV: $%{y:.2f}<extra></extra>",
        ),
        row=1,
        col=1,
    )

    # ── Row 2: Drawdown ──────────────────────────────────────────────────────
    fig.add_trace(
        go.Scatter(
            x=timestamps,
            y=dd_values,
            mode="lines",
            name="Drawdown",
            line=dict(color="#ef4444", width=1.5),
            fill="tozeroy",
            fillcolor="rgba(239,68,68,0.15)",
            hovertemplate="<b>%{x}</b><br>DD: %{y:.2f}%<extra></extra>",
        ),
        row=2,
        col=1,
    )

    # Reference line at 0 % for drawdown subplot
    fig.add_hline(y=0, line_dash="dot", line_color="#374151", row=2)

    fig.update_layout(
        paper_bgcolor="#0b0f19",
        plot_bgcolor="#111827",
        font=dict(color="#e2e8f0", family="Courier New", size=11),
        margin=dict(l=60, r=20, t=20, b=20),
        showlegend=False,
        hovermode="x unified",
    )

    fig.update_yaxes(
        title=dict(text="NAV ($)", font=dict(color="#64748b", size=10)),
        showgrid=True,
        gridcolor="#1f2937",
        zeroline=False,
        tickformat=",.0f",
        row=1,
        col=1,
    )

    fig.update_yaxes(
        title=dict(text="Drawdown (%)", font=dict(color="#64748b", size=10)),
        showgrid=True,
        gridcolor="#1f2937",
        zeroline=False,
        tickformat=".1f",
        row=2,
        col=1,
    )

    fig.update_xaxes(showgrid=True, gridcolor="#1f2937", zeroline=False, row=2, col=1)
    fig.update_xaxes(showgrid=True, gridcolor="#1f2937", zeroline=False, row=1, col=1)

    return fig


# ---------------------------------------------------------------------------
# Chart 2 — Candlestick (Trade Window)
# ---------------------------------------------------------------------------

def build_candlestick_chart(
    df: pd.DataFrame,
    trade_entry_ts: str,
    trade_direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
) -> go.Figure:
    """
    Candlestick chart zoomed to a specific trade window with trade markers,
    EMA overlays and volume subplot.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame with columns: timestamp(ms), open, high, low, close, volume
        and a 'ts' column of dtype datetime64.
    trade_entry_ts : str
        ISO-format timestamp string of the trade entry moment.
    trade_direction : str
        'LONG' or 'SHORT'.
    entry_price : float
        Entry price level.
    stop_loss : float
        Stop-loss price level.
    take_profit : float
        Take-profit price level.

    Returns
    -------
    plotly.graph_objects.Figure
    """
    # ── Slice window around entry ───────────────────────────────────────────
    entry_dt = pd.to_datetime(trade_entry_ts)

    if "ts" not in df.columns:
        df = df.copy()
        df["ts"] = pd.to_datetime(df.iloc[:, 0], unit="ms")

    df_sorted = df.sort_values("ts").reset_index(drop=True)

    # Find the candle that contains (or is just before) the entry timestamp
    mask = df_sorted["ts"] <= entry_dt
    idx_candidates = df_sorted.index[mask]
    idx = idx_candidates.max() if len(idx_candidates) > 0 else 0

    if pd.isna(idx) or idx < 0:
        idx = 0

    window_size = 40
    start_idx = max(0, idx - window_size)
    end_idx   = min(len(df_sorted) - 1, idx + window_size)
    window = df_sorted.iloc[start_idx:end_idx + 1].copy().reset_index(drop=True)

    # ── Build subplot figure ─────────────────────────────────────────────────
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.75, 0.25],
        subplot_titles=("", ""),
    )

    # ── Candlestick trace ────────────────────────────────────────────────────
    fig.add_trace(
        go.Candlestick(
            x=window["ts"],
            open=window["open"],
            high=window["high"],
            low=window["low"],
            close=window["close"],
            increasing=dict(line_color="#10b981", fillcolor="#10b981"),
            decreasing=dict(line_color="#ef4444", fillcolor="#ef4444"),
            name="BTCUSDT",
            hovertemplate=(
                "<b>%{x}</b><br>"
                "O:%{open:.2f}  H:%{high:.2f}  L:%{low:.2f}  C:%{close:.2f}"
                "<extra></extra>"
            ),
        ),
        row=1,
        col=1,
    )

    # ── Volume bars ──────────────────────────────────────────────────────────
    vol_colors = [
        "#10b981" if window.iloc[i]["close"] >= window.iloc[i]["open"] else "#ef4444"
        for i in range(len(window))
    ]
    fig.add_trace(
        go.Bar(
            x=window["ts"],
            y=window["volume"],
            marker_color=vol_colors,
            opacity=0.5,
            name="Volume",
            hovertemplate="Vol: %{y:.0f}<extra></extra>",
        ),
        row=2,
        col=1,
    )

    # ── Entry marker ─────────────────────────────────────────────────────────
    marker_symbol = "triangle-up" if trade_direction == "LONG" else "triangle-down"
    marker_color  = "#10b981" if trade_direction == "LONG" else "#ef4444"

    fig.add_trace(
        go.Scatter(
            x=[entry_dt],
            y=[entry_price],
            mode="markers+text",
            marker=dict(
                symbol=marker_symbol,
                size=18,
                color=marker_color,
                line=dict(width=2, color="white"),
            ),
            name=f"Entry ({trade_direction})",
            text=[f"${entry_price:,.0f}"],
            textposition="top center",
            textfont=dict(color=marker_color, size=10),
            hovertemplate=(
                f"ENTRY<br>Price: ${entry_price:.2f}<br>"
                f"Direction: {trade_direction}<extra></extra>"
            ),
        ),
        row=1,
        col=1,
    )

    # ── Stop-loss horizontal line ────────────────────────────────────────────
    fig.add_hline(
        y=stop_loss,
        line_dash="dash",
        line_color="#ef4444",
        line_width=1.5,
        annotation_text=f"SL ${stop_loss:,.0f}",
        annotation_position="bottom right",
        annotation=dict(font_color="#ef4444", font_size=9),
        row=1,
        col=1,
    )

    # ── Take-profit horizontal line ──────────────────────────────────────────
    tp_color = "#10b981" if trade_direction == "LONG" else "#ef4444"
    tp_text  = "TP"

    fig.add_hline(
        y=take_profit,
        line_dash="dash",
        line_color=tp_color,
        line_width=1.5,
        annotation_text=f"{tp_text} ${take_profit:,.0f}",
        annotation_position="top right",
        annotation=dict(font_color=tp_color, font_size=9),
        row=1,
        col=1,
    )

    # ── EMA overlays ─────────────────────────────────────────────────────────
    closes = window["close"].values.astype(float)

    if len(closes) >= 50:
        ema50 = _ema(closes, 50)
        fig.add_trace(
            go.Scatter(
                x=window["ts"].values[49:],
                y=ema50[49:],
                mode="lines",
                name="EMA50",
                line=dict(color="#f59e0b", width=1.2),
                hovertemplate="EMA50: %{y:.2f}<extra></extra>",
            ),
            row=1,
            col=1,
        )

    if len(closes) >= 200:
        ema200 = _ema(closes, 200)
        fig.add_trace(
            go.Scatter(
                x=window["ts"].values[199:],
                y=ema200[199:],
                mode="lines",
                name="EMA200",
                line=dict(color="#8b5cf6", width=1.2),
                hovertemplate="EMA200: %{y:.2f}<extra></extra>",
            ),
            row=1,
            col=1,
        )

    # ── Layout ───────────────────────────────────────────────────────────────
    fig.update_layout(
        paper_bgcolor="#0b0f19",
        plot_bgcolor="#111827",
        font=dict(color="#e2e8f0", family="Courier New", size=11),
        margin=dict(l=60, r=20, t=20, b=20),
        showlegend=True,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            font=dict(size=10),
        ),
        hovermode="x unified",
    )

    fig.update_yaxes(
        showgrid=True, gridcolor="#1f2937", zeroline=False, tickformat=".0f", row=1, col=1
    )
    fig.update_yaxes(showgrid=True, gridcolor="#1f2937", row=2, col=1)
    fig.update_xaxes(
        showgrid=True, gridcolor="#1f2937", rangeslider=dict(visible=False), row=1, col=1
    )
    fig.update_xaxes(showgrid=True, gridcolor="#1f2937", row=2, col=1)

    return fig


# ---------------------------------------------------------------------------
# Chart 3 — Win / Loss Distribution by Anomaly Type
# ---------------------------------------------------------------------------

def build_trade_distribution(trades: List[dict]) -> go.Figure:
    """
    Grouped bar chart showing WIN / LOSS / HOLD counts broken down by
    anomaly type.

    Parameters
    ----------
    trades : list[dict]
        Each dict must contain keys: anomaly, trade_result
        (anomaly may be None / empty string for no anomaly).

    Returns
    -------
    plotly.graph_objects.Figure
    """
    df = pd.DataFrame(trades)

    # Normalise anomaly labels — trades use 'anomaly_type', events use 'anomaly'
    if "anomaly_type" in df.columns and "anomaly" not in df.columns:
        df = df.rename(columns={"anomaly_type": "anomaly"})

    df["anomaly"] = (
        df["anomaly"] if "anomaly" in df.columns
        else pd.Series(["NONE"] * len(df), index=df.index)
    )
    df["anomaly"] = df["anomaly"].fillna("NONE").replace("", "NONE").str.strip().str.upper()

    # Cast trade_result
    df["trade_result"] = df["trade_result"].fillna("HOLD").str.strip().str.upper()

    # Aggregate counts per anomaly × result
    grouped = (
        df.groupby(["anomaly", "trade_result"])
        .size()
        .reset_index(name="count")
    )

    all_anomalies = sorted(grouped["anomaly"].unique())
    all_results   = ["WIN", "LOSS", "HOLD"]

    # Build traces for each result type
    colors = {"WIN": "#10b981", "LOSS": "#ef4444", "HOLD": "#f59e0b"}

    fig = go.Figure()

    for result in all_results:
        sub = grouped[grouped["trade_result"] == result].set_index("anomaly")
        counts = [float(sub.loc[a, "count"]) if a in sub.index else 0.0 for a in all_anomalies]

        fig.add_trace(
            go.Bar(
                x=all_anomalies,
                y=counts,
                name=result,
                marker_color=colors.get(result, "#64748b"),
                text=counts,
                textposition="outside",
                textfont=dict(color=colors.get(result, "#e2e8f0"), size=10),
                hovertemplate=(
                    f"<b>{result}</b><br>Anomaly: %{{x}}<br>Count: %{{y:.0f}}<extra></extra>"
                ),
            )
        )

    fig.update_layout(
        paper_bgcolor="#0b0f19",
        plot_bgcolor="#111827",
        font=dict(color="#e2e8f0", family="Courier New", size=11),
        margin=dict(l=60, r=20, t=20, b=60),
        barmode="group",
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
        xaxis=dict(
            title=dict(text="Anomaly Type", font=dict(color="#64748b", size=10)),
            showgrid=True,
            gridcolor="#1f2937",
            tickangle=-30,
        ),
        yaxis=dict(
            title=dict(text="Count", font=dict(color="#64748b", size=10)),
            showgrid=True,
            gridcolor="#1f2937",
            tickformat=".0f",
        ),
    )

    return fig


# ---------------------------------------------------------------------------
# Chart 4 — Correlation Heatmap (Persona Weight vs Trade Outcome)
# ---------------------------------------------------------------------------

def build_correlation_heatmap(
    persona_weights: List[dict],
    trades: List[dict],
) -> go.Figure:
    """
    Correlation-style heatmap that visualises how each persona's weight
    correlates with trade outcomes (WIN / LOSS) across individual trades.

    X-axis  : persona dimensions (SMC_ICT, ORDER_FLOW, MACRO_ONCHAIN)
    Y-axis  : individual trade index
    Color   : WIN = green shade, LOSS = red shade, neutral = grey

    Parameters
    ----------
    persona_weights : list[dict]
        One dict per trade with keys for each persona weight (float 0–1).
        Example: [{"SMC_ICT": 0.6, "ORDER_FLOW": 0.3, "MACRO_ONCHAIN": 0.1}, ...]
    trades : list[dict]
        Parallel list of trade results; each dict must contain 'trade_result'.

    Returns
    -------
    plotly.graph_objects.Figure
    """

    pw_df = pd.DataFrame(persona_weights)
    # Fill any missing weights with 0
    pw_df = pw_df.fillna(0)

    # Ensure consistent column ordering
    persona_cols = ["SMC_ICT", "ORDER_FLOW", "MACRO_ONCHAIN"]
    for col in persona_cols:
        if col not in pw_df.columns:
            pw_df[col] = 0.0
    pw_df = pw_df[persona_cols]

    tr_df = pd.DataFrame(trades)
    tr_df["trade_result"] = tr_df["trade_result"].fillna("HOLD").str.upper().str.strip()

    # Numeric outcome: WIN=+1, LOSS=-1, HOLD=0
    outcome_map = {"WIN": 1, "LOSS": -1, "HOLD": 0}
    outcome_vals = tr_df["trade_result"].map(outcome_map).fillna(0).values

    n_trades = len(pw_df)
    trade_labels = [f"#{i+1}" for i in range(n_trades)]

    # Build a weighted score matrix:
    #   score[trade, persona] = weight[trade, persona] * outcome[trade]
    # This surfaces which personas contributed most to wins / losses.
    score_matrix = pw_df.values * outcome_vals[:, np.newaxis]  # shape (n, 3)

    score_df = pd.DataFrame(score_matrix, columns=persona_cols, index=trade_labels)

    fig = go.Figure(
        go.Heatmap(
            z=score_df.values,
            x=score_df.columns.tolist(),
            y=score_df.index.tolist(),
            colorscale=[
                [0.0, "#ef4444"],   # LOSS → red
                [0.5, "#1f2937"],   # neutral → dark grey
                [1.0, "#10b981"],   # WIN  → green
            ],
            zmid=0,
            hovertemplate=(
                "Trade: %{y}<br>Persona: %{x}<br>"
                "Score: %{z:.3f}<extra></extra>"
            ),
            colorbar=dict(
                title=dict(text="WIN → +1<br>LOSS → -1", font=dict(color="#e2e8f0", size=9)),
                tickfont=dict(color="#e2e8f0", size=9),
            ),
        )
    )

    fig.update_layout(
        paper_bgcolor="#0b0f19",
        plot_bgcolor="#111827",
        font=dict(color="#e2e8f0", family="Courier New", size=10),
        margin=dict(l=100, r=40, t=20, b=40),
        xaxis=dict(
            title=dict(text="Persona Dimension", font=dict(color="#64748b", size=10)),
            tickfont=dict(color="#e2e8f0"),
            showgrid=True,
            gridcolor="#1f2937",
        ),
        yaxis=dict(
            title=dict(text="Trade #", font=dict(color="#64748b", size=10)),
            tickfont=dict(color="#e2e8f0", size=9),
            showgrid=True,
            gridcolor="#1f2937",
            dtick=5 if n_trades > 5 else 1,
        ),
    )

    return fig
