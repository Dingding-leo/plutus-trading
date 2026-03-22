"""
src/dashboard/forensics.py
Forensics Lead — Market Forensics tab for the Plutus Streamlit dashboard.

This module renders the trade inspector where the PM audits AI reasoning.
It shows:
  1. Trade selector (top of tab)
  2. Trade summary row (direction / entry / PnL)
  3. Candlestick chart with entry / SL / TP overlays
  4. Scanner trigger — the anomaly that woke the system
  5. AI reasoning audit — 3-column persona cards
  6. Trade metrics grid (position value, leverage, SL, TP, RR)
  7. Win / Loss streak analysis
  8. Full event timeline
  9. Anomaly distribution (count + win rate)
 10. Trade distribution chart (from charts.py)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from src.dashboard.charts import (
    build_candlestick_chart,
    build_correlation_heatmap,
    build_trade_distribution,
)

# ─── Persona metadata ─────────────────────────────────────────────────────────

PERSONA_META: Dict[str, Dict[str, str]] = {
    "SMC_ICT": {
        "name": "Smart Money Concepts",
        "short": "SMC / ICT",
        "emoji": "🧠",
        "color": "#818cf8",  # indigo
        "tagline": "Liquidity · FVG · Order Blocks",
    },
    "ORDER_FLOW": {
        "name": "Order Flow",
        "short": "Order Flow",
        "emoji": "📊",
        "color": "#34d399",  # emerald
        "tagline": "OI · Funding · Liquidations",
    },
    "MACRO_ONCHAIN": {
        "name": "Macro & On-Chain",
        "short": "Macro / On-Chain",
        "emoji": "🌐",
        "color": "#fbbf24",  # amber
        "tagline": "ETF Flows · Whale Wallets · DXY",
    },
}
PERSONA_ORDER = ["SMC_ICT", "ORDER_FLOW", "MACRO_ONCHAIN"]

# ─── Style helpers ─────────────────────────────────────────────────────────────

RESULT_COLORS = {
    "WIN":  "#10b981",
    "LOSS": "#ef4444",
    "HOLD": "#64748b",
}

DIRECTION_COLORS = {
    "LONG":  "#10b981",
    "SHORT": "#ef4444",
}


def _result_badge(result: str) -> str:
    color = RESULT_COLORS.get(result.upper(), "#64748b")
    return f"<span style='color:{color}; font-weight:700'>{result.upper()}</span>"


def _direction_badge(direction: str) -> str:
    color = DIRECTION_COLORS.get(direction.upper(), "#64748b")
    arrow = "▲" if direction.upper() == "LONG" else "▼"
    return f"<span style='color:{color}; font-weight:700'>{arrow} {direction.upper()}</span>"


def _confidence_bar(score: int, max_score: int = 100) -> str:
    """Return an HTML coloured confidence bar."""
    pct = min(score / max_score, 1.0)
    if score >= 65:
        bar_color = "#10b981"
    elif score >= 50:
        bar_color = "#fbbf24"
    else:
        bar_color = "#ef4444"
    bar_html = (
        f"<div style='background:#1f2937; border-radius:4px; height:8px; width:100%'>"
        f"<div style='background:{bar_color}; width:{pct*100:.1f}%; height:8px; border-radius:4px;"
        f" transition:width 0.4s'></div>"
        f"</div>"
    )
    return f"<div style='display:flex; align-items:center; gap:8px'>" \
           f"<span style='color:{bar_color}; font-size:1.1rem; font-weight:700; min-width:30px'>{score}</span>" \
           f"{bar_html}" \
           f"</div>"


def _style_result_cell(result: str) -> Dict[str, str]:
    color = RESULT_COLORS.get(result.upper(), "#64748b")
    return f"color:{color}; font-weight:700"


# ─── Streak analysis ─────────────────────────────────────────────────────────

def _compute_streaks(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Walk through executed events in time order and compute:
      - current streak (length + direction)
      - longest winning streak
      - longest losing streak
      - total / average per category
    """
    executed = [e for e in events if e.get("executed", False)]
    executed.sort(key=lambda x: x.get("timestamp", ""))

    stats = {
        "current_streak_len": 0,
        "current_streak_dir": None,
        "max_win_streak": 0,
        "max_loss_streak": 0,
        "total_wins": 0,
        "total_losses": 0,
        "total_holds": 0,
        "win_rate": 0.0,
        "rows": [],
    }

    cur_len = 0
    cur_dir = None

    for e in executed:
        result = e.get("trade_result", "HOLD").upper()
        stats["rows"].append({
            "timestamp": e.get("timestamp", ""),
            "anomaly":  e.get("anomaly", ""),
            "direction": e.get("direction", ""),
            "result":   result,
        })

        if result == "WIN":
            stats["total_wins"] += 1
            if cur_dir == "WIN":
                cur_len += 1
            else:
                cur_dir = "WIN"
                cur_len = 1
            stats["max_win_streak"] = max(stats["max_win_streak"], cur_len)
            stats["current_streak_len"] = cur_len
            stats["current_streak_dir"] = cur_dir
        elif result == "LOSS":
            stats["total_losses"] += 1
            if cur_dir == "LOSS":
                cur_len += 1
            else:
                cur_dir = "LOSS"
                cur_len = 1
            stats["max_loss_streak"] = max(stats["max_loss_streak"], cur_len)
            stats["current_streak_len"] = cur_len
            stats["current_streak_dir"] = cur_dir
        else:
            stats["total_holds"] += 1
            cur_len = 0
            cur_dir = None
            stats["current_streak_len"] = 0
            stats["current_streak_dir"] = None

    total_closed = stats["total_wins"] + stats["total_losses"]
    if total_closed > 0:
        stats["win_rate"] = stats["total_wins"] / total_closed * 100

    return stats


# ─── Main render function ─────────────────────────────────────────────────────

def render_forensics_tab(
    tab,
    events: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    btc_ohlcv: pd.DataFrame,
) -> None:
    """
    Render the Market Forensics tab inside the provided Streamlit tab container.

    Parameters
    ----------
    tab : streamlit.container
        The tab container returned by st.tabs().
    events : list of dict
        Event records from chronos_trades.json (executed + skipped).
    trades : list of dict
        Trade records (with 'signals' dict per persona).
    btc_ohlcv : pd.DataFrame
        OHLCV data with a 'ts' (datetime) column.
    """
    with tab:
        st.markdown("---")
        _render_header_meta(events, trades)

        # ── 1. Trade Selector ──────────────────────────────────────────────────
        st.markdown("### 🔍 SELECT TRADE TO AUDIT")
        selected_idx = _render_trade_selector(trades)

        if selected_idx is None or not trades:
            st.info("No trades available.")
            return

        selected_trade = trades[selected_idx]

        # ── 2. Trade Summary Row ───────────────────────────────────────────────
        st.markdown("")
        _render_summary_row(selected_trade, selected_idx)

        # ── 3. Candlestick Chart ───────────────────────────────────────────────
        st.markdown("")
        _render_candlestick(selected_trade, btc_ohlcv)

        # ── 4. Scanner Trigger ────────────────────────────────────────────────
        _render_scanner_trigger(selected_trade, events)

        # ── 5. AI Reasoning Audit (3 persona cards) ───────────────────────────
        _render_ai_audit(selected_trade)

        # ── 6. Trade Metrics Grid ─────────────────────────────────────────────
        _render_metrics_grid(selected_trade)

        # ── 7. Win / Loss Streak Analysis ────────────────────────────────────
        _render_streak_analysis(events)

        # ── 8. Full Event Timeline ───────────────────────────────────────────
        _render_event_timeline(events)

        # ── 9. Anomaly Distribution ──────────────────────────────────────────
        _render_anomaly_distribution(events, trades)

        # ── 10. Trade Distribution Chart ──────────────────────────────────────
        _render_trade_distribution(trades, events)

        # ── 11. Correlation Heatmap ────────────────────────────────────────────
        _render_correlation_heatmap(trades)


# ─── Sub-renderers ─────────────────────────────────────────────────────────────

def _render_header_meta(events, trades):
    """Top-of-tab KPIs: total trades, win rate, total PnL, total events."""
    executed = [e for e in events if e.get("executed", False)]
    wins  = sum(1 for t in trades if t.get("trade_result", "").upper() == "WIN")
    losses = sum(1 for t in trades if t.get("trade_result", "").upper() == "LOSS")
    total_closed = wins + losses
    wr = wins / total_closed * 100 if total_closed > 0 else 0.0
    total_pnl = sum(t.get("pnl", 0.0) for t in trades)

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("📋 Total Events", f"{len(events)}")
    with col2:
        st.metric("🎯 Executed Trades", f"{len(executed)}")
    with col3:
        st.metric("📈 Win Rate", f"{wr:.1f}%", delta=f"{wins}W / {losses}L")
    with col4:
        color = "#10b981" if total_pnl >= 0 else "#ef4444"
        st.markdown(
            f"<div style='text-align:center'>"
            f"<div style='font-size:0.75rem; color:#64748b; margin-bottom:4px'>"
            f"Total PnL</div>"
            f"<div style='font-size:1.5rem; font-weight:700; color:{color}'>"
            f"${total_pnl:+,.2f}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )


def _render_trade_selector(trades: List[Dict[str, Any]]) -> Optional[int]:
    """Render the trade selector and return the selected index."""
    trade_options = []
    for i, t in enumerate(trades):
        ts        = t.get("timestamp", "")[:16]
        direction = t.get("direction", "").upper()
        anomaly   = t.get("anomaly_type", "UNKNOWN").replace("_", " ")
        entry     = t.get("entry_price", 0)
        pnl       = t.get("pnl", 0.0)
        result    = t.get("trade_result", "HOLD").upper()
        confidence = t.get("confidence", 0)
        trade_options.append(
            f"#{i+1:02d}  [{result:4s}]  {direction:5s}  "
            f"${entry:>10,.0f}  |  {anomaly:<22s}  "
            f"|  PnL {pnl:+.2f}  |  CF {confidence}"
        )

    selected_idx = st.selectbox(
        "Choose a trade to inspect:",
        options=range(len(trade_options)),
        format_func=lambda i: trade_options[i],
        label_visibility="collapsed",
    )
    return selected_idx


def _render_summary_row(selected_trade: Dict[str, Any], selected_idx: int) -> None:
    """Four metric boxes: Trade #, Direction, Entry Price, PnL."""
    direction = selected_trade.get("direction", "NEUTRAL").upper()
    entry    = selected_trade.get("entry_price", 0.0)
    pnl      = selected_trade.get("pnl", 0.0)
    result   = selected_trade.get("trade_result", "HOLD").upper()
    confidence = selected_trade.get("confidence", 0)
    anomaly  = selected_trade.get("anomaly_type", "").replace("_", " ")
    rr       = selected_trade.get("rr_ratio", 0.0)

    dir_color = DIRECTION_COLORS.get(direction, "#64748b")
    dir_arrow = "▲" if direction == "LONG" else "▼"
    pnl_color = "#10b981" if pnl >= 0 else "#ef4444"
    result_color = RESULT_COLORS.get(result, "#64748b")

    cols = st.columns([1, 1, 1, 1])
    labels = ["Trade #", "Direction", "Confidence", "Result"]
    values = [
        f"#{selected_idx + 1}",
        f"{dir_arrow} {direction}",
        f"{confidence}",
        result,
    ]
    colors = ["#e2e8f0", dir_color, "#e2e8f0", result_color]

    for col, label, val, clr in zip(cols, labels, values, colors):
        with col:
            st.markdown(
                f"<div style='background:#111827; border-radius:8px; padding:16px 12px; "
                f"text-align:center; border:1px solid #1f2937'>"
                f"<div style='font-size:0.7rem; color:#64748b; text-transform:uppercase; "
                f"letter-spacing:0.08em; margin-bottom:6px'>{label}</div>"
                f"<div style='font-size:1.3rem; font-weight:700; color:{clr}'>{val}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

    # Second row: Entry, PnL, SL, TP
    entry_price  = selected_trade.get("entry_price", 0.0)
    stop_loss    = selected_trade.get("stop_loss", 0.0)
    take_profit  = selected_trade.get("take_profit", 0.0)
    lev          = selected_trade.get("leverage", 1)

    cols2 = st.columns([1, 1, 1, 1])
    meta = [
        ("Entry Price", f"${entry_price:,.2f}", "#e2e8f0"),
        ("PnL",         f"${pnl:+.2f}",        pnl_color),
        ("Stop Loss",   f"${stop_loss:,.2f}",   "#ef4444"),
        ("Take Profit", f"${take_profit:,.2f}", "#10b981"),
    ]
    for col, (label, val, clr) in zip(cols2, meta):
        with col:
            st.markdown(
                f"<div style='background:#111827; border-radius:8px; padding:12px; "
                f"text-align:center; border:1px solid #1f2937'>"
                f"<div style='font-size:0.65rem; color:#64748b; text-transform:uppercase; "
                f"letter-spacing:0.08em; margin-bottom:4px'>{label}</div>"
                f"<div style='font-size:1rem; font-weight:700; color:{clr}'>{val}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )


def _render_candlestick(
    selected_trade: Dict[str, Any],
    btc_ohlcv: pd.DataFrame,
) -> None:
    """Render the candlestick chart with trade overlays."""
    entry_ts    = selected_trade.get("timestamp", "")
    direction   = selected_trade.get("direction", "LONG")
    entry_price = selected_trade.get("entry_price", 0.0)
    stop_loss   = selected_trade.get("stop_loss", 0.0)
    take_profit = selected_trade.get("take_profit", 0.0)

    try:
        fig = build_candlestick_chart(
            df=btc_ohlcv,
            trade_entry_ts=entry_ts,
            trade_direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        st.warning(f"Could not render chart: {e}")


def _render_scanner_trigger(
    selected_trade: Dict[str, Any],
    events: List[Dict[str, Any]],
) -> None:
    """Expandable section showing the anomaly that triggered the trade."""
    with st.expander("📡 SCANNER TRIGGER", expanded=True):
        # Match the trade to its event via timestamp (most reliable key)
        trade_ts = selected_trade.get("timestamp", "")
        matched = [
            e for e in events
            if e.get("timestamp", "") == trade_ts
        ]
        if matched:
            ev = matched[0]
            anomaly  = ev.get("anomaly", "UNKNOWN")
            ts       = ev.get("timestamp", "")
            executed = ev.get("executed", False)
            confidence = ev.get("confidence", 0)
            direction  = ev.get("direction", "")
            fitnesses  = ev.get("fitness", {})

            anomaly_colors = {
                "LIQUIDITY_SWEEP":    "#f59e0b",
                "EXTREME_DEVIATION":  "#ef4444",
                "VOLATILITY_SQUEEZE": "#818cf8",
            }
            a_color = anomaly_colors.get(anomaly.upper(), "#64748b")

            fitness_rows = ""
            for p, f in fitnesses.items():
                fitness_rows += (
                    f"<tr>"
                    f"<td style='color:#94a3b8; padding:2px 8px'>{p}</td>"
                    f"<td style='color:#e2e8f0; font-family:monospace; padding:2px 8px; "
                    f"text-align:right'>{f:.4f}</td>"
                    f"</tr>"
                )

            st.markdown(
                f"""
                <div style='display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-top:8px'>
                    <div>
                        <div style='font-size:0.7rem; color:#64748b; text-transform:uppercase;
                                     letter-spacing:0.08em; margin-bottom:4px'>Anomaly Type</div>
                        <div style='font-size:1.1rem; font-weight:700; color:{a_color}'>{anomaly}</div>
                    </div>
                    <div>
                        <div style='font-size:0.7rem; color:#64748b; text-transform:uppercase;
                                     letter-spacing:0.08em; margin-bottom:4px'>Triggered At</div>
                        <div style='font-size:0.9rem; color:#e2e8f0; font-family:monospace'>{ts}</div>
                    </div>
                    <div>
                        <div style='font-size:0.7rem; color:#64748b; text-transform:uppercase;
                                     letter-spacing:0.08em; margin-bottom:4px'>Confidence</div>
                        <div style='font-size:1.1rem; font-weight:700; color:#e2e8f0'>{confidence}</div>
                    </div>
                    <div>
                        <div style='font-size:0.7rem; color:#64748b; text-transform:uppercase;
                                     letter-spacing:0.08em; margin-bottom:4px'>System Decision</div>
                        <div style='font-size:1rem; font-weight:700; color:{"#10b981" if executed else "#ef4444"}'>
                            {'✅ EXECUTED' if executed else '❌ SKIPPED'}
                        </div>
                    </div>
                </div>

                <div style='margin-top:16px'>
                    <div style='font-size:0.7rem; color:#64748b; text-transform:uppercase;
                                 letter-spacing:0.08em; margin-bottom:8px'>Persona Fitness Scores</div>
                    <table style='width:100%; border-collapse:collapse; background:#0f1117;
                                  border-radius:6px; overflow:hidden'>
                        <thead>
                            <tr style='background:#1f2937'>
                                <th style='color:#64748b; font-size:0.65rem; text-align:left; padding:4px 8px;
                                           text-transform:uppercase'>Persona</th>
                                <th style='color:#64748b; font-size:0.65rem; text-align:right; padding:4px 8px;
                                           text-transform:uppercase'>Fitness</th>
                            </tr>
                        </thead>
                        <tbody>{fitness_rows}</tbody>
                    </table>
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f"**Anomaly:** `{selected_trade.get('anomaly_type', 'UNKNOWN')}`  \n"
                f"**Triggered at:** `{trade_ts}`  \n"
                f"**Confidence:** `{selected_trade.get('confidence', 0)}`"
            )


def _render_ai_audit(selected_trade: Dict[str, Any]) -> None:
    """3-column persona reasoning cards."""
    st.markdown("### 🤖 AI REASONING AUDIT — PERSONA SCORECARD")
    st.caption("Each persona independently analysed the anomaly and submitted a trade thesis.")

    cols = st.columns(3)
    for col, persona_name in zip(cols, PERSONA_ORDER):
        meta = PERSONA_META.get(persona_name, {})
        sig  = selected_trade.get("signals", {}).get(persona_name, {})

        direction  = sig.get("direction", "NEUTRAL").upper()
        confidence = sig.get("confidence_score", 0)
        thesis     = sig.get("thesis", "No thesis recorded.")
        lev        = sig.get("recommended_leverage", 0)
        weights    = selected_trade.get("weights", {})
        fitnesses  = selected_trade.get("fitnesses", {})

        weight_v   = weights.get(persona_name, 0.0)
        fitness_v  = fitnesses.get(persona_name, 0.0)

        if direction == "LONG":
            dir_icon  = "▲"
            dir_color = "#10b981"
        elif direction == "SHORT":
            dir_icon  = "▼"
            dir_color = "#ef4444"
        else:
            dir_icon  = "—"
            dir_color = "#64748b"

        persona_color = meta.get("color", "#818cf8")

        with col:
            st.markdown(
                f"""
                <div style='background:#111827; border-radius:12px; padding:16px;
                            border:1px solid #1f2937; height:100%'>
                    <!-- Persona header -->
                    <div style='display:flex; align-items:center; gap:8px; margin-bottom:12px'>
                        <span style='font-size:1.4rem'>{meta.get("emoji", "🧠")}</span>
                        <div>
                            <div style='font-size:0.95rem; font-weight:700; color:{persona_color}'>
                                {persona_name}
                            </div>
                            <div style='font-size:0.65rem; color:#64748b'>{meta.get("tagline", "")}</div>
                        </div>
                    </div>

                    <!-- Direction badge -->
                    <div style='display:inline-block; background:{dir_color}22; border:1px solid {dir_color}55;
                                border-radius:6px; padding:4px 10px; margin-bottom:10px'>
                        <span style='color:{dir_color}; font-weight:800; font-size:1rem'>
                            {dir_icon} {direction}
                        </span>
                    </div>

                    <!-- Confidence bar -->
                    <div style='margin-bottom:12px'>
                        <div style='display:flex; justify-content:space-between; margin-bottom:4px'>
                            <span style='font-size:0.7rem; color:#64748b; text-transform:uppercase;
                                         letter-spacing:0.06em'>Confidence</span>
                        </div>
                        {_confidence_bar(confidence)}
                    </div>

                    <!-- Leverage -->
                    <div style='margin-bottom:12px'>
                        <div style='font-size:0.7rem; color:#64748b; text-transform:uppercase;
                                     letter-spacing:0.06em; margin-bottom:4px'>Rec. Leverage</div>
                        <div style='font-size:1.2rem; font-weight:700; color:#e2e8f0'>
                            {lev}×
                        </div>
                    </div>

                    <!-- Thesis -->
                    <div style='margin-bottom:12px'>
                        <div style='font-size:0.7rem; color:#64748b; text-transform:uppercase;
                                     letter-spacing:0.06em; margin-bottom:6px'>Thesis</div>
                        <div style='font-size:0.8rem; color:#cbd5e1;
                                    border-left:2px solid {persona_color}66; padding-left:8px;
                                    font-style:italic; line-height:1.5'>
                            {thesis}
                        </div>
                    </div>

                    <!-- Weight & Fitness -->
                    <div style='display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:8px'>
                        <div style='background:#0f1117; border-radius:6px; padding:8px; text-align:center'>
                            <div style='font-size:0.6rem; color:#64748b; text-transform:uppercase;
                                        margin-bottom:2px'>Weight</div>
                            <div style='font-size:0.9rem; font-weight:700; color:#e2e8f0;
                                        font-family:monospace'>{weight_v:.3f}</div>
                        </div>
                        <div style='background:#0f1117; border-radius:6px; padding:8px; text-align:center'>
                            <div style='font-size:0.6rem; color:#64748b; text-transform:uppercase;
                                        margin-bottom:2px'>Fitness</div>
                            <div style='font-size:0.9rem; font-weight:700; color:#e2e8f0;
                                        font-family:monospace'>{fitness_v:.4f}</div>
                        </div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def _render_metrics_grid(selected_trade: Dict[str, Any]) -> None:
    """Five metric boxes: Position Value, Leverage, Stop Loss, Take Profit, RR Ratio."""
    st.markdown("")
    st.markdown("#### ⚙️ TRADE PARAMETERS")

    position_value = selected_trade.get("position_value", 0.0)
    leverage      = selected_trade.get("leverage", 1)
    stop_loss      = selected_trade.get("stop_loss", 0.0)
    take_profit    = selected_trade.get("take_profit", 0.0)
    rr_ratio       = selected_trade.get("rr_ratio", 0.0)
    entry_price    = selected_trade.get("entry_price", 0.0)

    # Calculate distances
    if leverage > 0 and stop_loss > 0 and entry_price > 0:
        is_long = selected_trade.get("direction", "LONG").upper() == "LONG"
        if is_long:
            sl_dist = abs(entry_price - stop_loss) / entry_price * 100
            tp_dist = abs(take_profit - entry_price) / entry_price * 100
        else:
            sl_dist = abs(entry_price - stop_loss) / entry_price * 100
            tp_dist = abs(entry_price - take_profit) / entry_price * 100
    else:
        sl_dist = tp_dist = 0.0

    cols = st.columns(5)
    metrics = [
        ("Position Value", f"${position_value:,.2f}", "#e2e8f0"),
        ("Leverage", f"{leverage}×", "#e2e8f0"),
        ("Stop Loss", f"${stop_loss:,.2f}\n({sl_dist:.2f}%)", "#ef4444"),
        ("Take Profit", f"${take_profit:,.2f}\n({tp_dist:.2f}%)", "#10b981"),
        ("RR Ratio", f"{rr_ratio:.2f}", "#818cf8"),
    ]
    for col, (label, val, clr) in zip(cols, metrics):
        with col:
            st.markdown(
                f"<div style='background:#0f1117; border-radius:8px; padding:14px 10px; "
                f"text-align:center; border:1px solid #1f2937'>"
                f"<div style='font-size:0.65rem; color:#64748b; text-transform:uppercase; "
                f"letter-spacing:0.08em; margin-bottom:6px'>{label}</div>"
                f"<div style='font-size:1.1rem; font-weight:700; color:{clr}; "
                f"white-space:pre-line; line-height:1.3'>{val}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )


def _render_streak_analysis(events: List[Dict[str, Any]]) -> None:
    """Expandable streak analysis section."""
    with st.expander("📉 WIN / LOSS STREAK ANALYSIS", expanded=False):
        stats = _compute_streaks(events)

        total_closed = stats["total_wins"] + stats["total_losses"]
        wr = stats["win_rate"]

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Wins",  stats["total_wins"])
        with col2:
            st.metric("Total Losses", stats["total_losses"])
        with col3:
            st.metric("Win Rate", f"{wr:.1f}%")
        with col4:
            st.metric("Max Win Streak", stats["max_win_streak"])

        col5, col6, col7, col8 = st.columns(4)
        with col5:
            st.metric("Max Loss Streak", stats["max_loss_streak"])
        with col6:
            st.metric("Current Streak", stats["current_streak_len"],
                      delta=stats["current_streak_dir"] or "—")
        with col7:
            st.metric("Holds", stats["total_holds"])
        with col8:
            st.metric("Total Closed", total_closed)

        if stats["rows"]:
            df = pd.DataFrame(stats["rows"])
            df.columns = ["Timestamp", "Anomaly", "Direction", "Result"]
            st.markdown("**Streak Timeline**")
            st.dataframe(
                df.style.applymap(_style_result_cell, subset=["Result"]),
                use_container_width=True,
                hide_index=True,
            )


def _render_event_timeline(events: List[Dict[str, Any]]) -> None:
    """Expandable full event timeline as a styled DataFrame."""
    with st.expander("🕐 FULL EVENT TIMELINE", expanded=False):
        if not events:
            st.info("No events to display.")
            return

        df = pd.DataFrame(events)

        # Normalise columns (events use 'anomaly', trades use 'anomaly_type')
        if "anomaly" in df.columns:
            df = df.rename(columns={"anomaly": "anomaly_type"})
        if "anomaly_type" not in df.columns:
            df["anomaly_type"] = "UNKNOWN"

        # Pick the columns we want
        display_cols = ["event_num", "timestamp", "anomaly_type", "direction",
                        "confidence", "executed", "trade_result", "pnl"]
        available = [c for c in display_cols if c in df.columns]
        display_df = df[available].copy()

        # Format booleans
        if "executed" in display_df.columns:
            display_df["executed"] = display_df["executed"].map(
                {True: "✅", False: "❌"}
            )

        # Format pnl
        if "pnl" in display_df.columns:
            display_df["pnl"] = display_df["pnl"].apply(
                lambda x: f"${x:+.2f}" if isinstance(x, (int, float)) else str(x)
            )

        st.dataframe(
            display_df.style.applymap(_style_result_cell, subset=["trade_result"]),
            use_container_width=True,
            hide_index=True,
        )


def _render_anomaly_distribution(
    events: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
) -> None:
    """Left: anomaly count bar chart. Right: win rate by anomaly type."""
    st.markdown("")
    st.markdown("#### 📊 ANOMALY DISTRIBUTION")

    # Build a combined DataFrame from events
    ev_df = pd.DataFrame(events)
    if "anomaly" in ev_df.columns:
        ev_df = ev_df.rename(columns={"anomaly": "anomaly_type"})
    if "anomaly_type" not in ev_df.columns:
        ev_df["anomaly_type"] = "UNKNOWN"

    anomaly_counts = (
        ev_df["anomaly_type"]
        .value_counts()
        .reset_index()
    )
    anomaly_counts.columns = ["Anomaly Type", "Count"]

    # Win rate per anomaly from trades
    tr_df = pd.DataFrame(trades)
    if "anomaly_type" in tr_df.columns and "trade_result" in tr_df.columns:
        tr_df["trade_result"] = tr_df["trade_result"].str.upper().str.strip()
        win_rate_df = (
            tr_df.groupby("anomaly_type")["trade_result"]
            .apply(lambda x: (x == "WIN").sum() / max((x.isin(["WIN", "LOSS"])).sum(), 1) * 100)
            .reset_index()
        )
        win_rate_df.columns = ["Anomaly Type", "Win Rate (%)"]
    else:
        win_rate_df = pd.DataFrame(columns=["Anomaly Type", "Win Rate (%)"])

    left_col, right_col = st.columns(2)

    with left_col:
        st.markdown("**Event Count by Anomaly Type**")
        st.bar_chart(
            anomaly_counts.set_index("Anomaly Type"),
            color=["#818cf8"],
        )

    with right_col:
        st.markdown("**Win Rate by Anomaly Type**")
        if not win_rate_df.empty:
            st.bar_chart(
                win_rate_df.set_index("Anomaly Type"),
                color=["#10b981"],
            )
        else:
            st.info("No trade data available for win rate analysis.")


def _render_trade_distribution(
    trades: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
) -> None:
    """Build and render the trade distribution chart from charts.py."""
    st.markdown("")
    st.markdown("#### 📈 TRADE DISTRIBUTION")
    # Normalise trades to the column names expected by build_trade_distribution:
    # it expects 'anomaly', but our trades have 'anomaly_type'.
    normalised = []
    for t in trades:
        row = dict(t)
        row["anomaly"] = row.pop("anomaly_type", "UNKNOWN")
        normalised.append(row)
    try:
        fig = build_trade_distribution(normalised)
        st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        st.warning(f"Could not render distribution chart: {e}")


def _render_correlation_heatmap(trades: List[Dict[str, Any]]) -> None:
    """Build and render the persona × trade correlation heatmap."""
    st.markdown("")
    with st.expander("🗺️ PERSONA WEIGHT × TRADE OUTCOME HEATMAP", expanded=False):
        weights_list = [t.get("weights", {}) for t in trades]
        try:
            fig = build_correlation_heatmap(weights_list, trades)
            st.plotly_chart(fig, use_container_width=True)
        except Exception as e:
            st.warning(f"Could not render heatmap: {e}")
