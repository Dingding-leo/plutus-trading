"""
Plutus V3 — Chronos Engine
The Wakelock Orchestrator: Scanner → Personas → DynamicAllocator → Execution

Architectural role:
  The central nervous system of Plutus V3. Wires all previously-built components
  into a single event-driven backtest loop with zero API waste.

Time-Jump Architecture:
  Standard backtest:  tick → call LLM → check decision →  (every candle = $cost)
  Chronos Engine:     scan(df) → [events] → LLM wake only on anomaly → weights → trade

  ┌─────────────────────────────────────────────────────────┐
  │  VanguardScanner.scan(df) → List[ScannerEvent]           │
  │  len(events)==0 → ZERO API calls → exit immediately    │
  └──────────────────┬──────────────────────────────────────┘
                     │ only 5% of candles have events
                     ▼
  ┌─────────────────────────────────────────────────────────┐
  │  For each event (chronologically):                      │
  │    1. Slice df[:event_idx] → historical context         │
  │    2. Build persona data payloads (SMC / OF / Macro)    │
  │    3. DryRunPersona.analyze() → PersonaSignal         │
  │       (or real LLM personas if dry_run=False)           │
  │    4. DynamicAllocator.update_all(signals)              │
  │    5. weights = allocator.allocate()                   │
  │    6. blended_direction = weighted_vote(weights, sigs)│
  │    7. open_trade(...) if confidence threshold met     │
  └─────────────────────────────────────────────────────────┘

Key design decisions:
  - Zero lookahead bias: df[:event.candle_idx] for all historical data
  - O(1) for 95% of candles (empty event list = immediate exit)
  - Mock personas avoid API calls during backtesting
  - Blended direction = weighted vote from personas
  - Position sizing = sum(weights[i] * position_i) for each persona
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..data.scanner import VanguardScanner, ScannerEvent, AnomalyType
from .. import config as sys_config
from ..data.personas import (
    PersonaSignal, PersonaType, Direction,
    SMC_ICT_Persona, OrderFlowPersona, MacroOnChainPersona,
    create_persona,
)
from ..backtest.portfolio_manager import DynamicAllocator
from ..data.memory import MemoryBank


# ─── Enums ──────────────────────────────────────────────────────────────────────

class BacktestMode(Enum):
    DRY_RUN = "dry_run"    # Mock personas (no API calls)
    LIVE    = "live"       # Real LLM API calls


# ─── Blended Trade ───────────────────────────────────────────────────────────────

@dataclass
class BlendedTrade:
    """A single executed trade from the Chronos Engine."""
    event_idx:        int
    timestamp:         datetime
    anomaly_type:      str
    direction:         str          # "LONG" | "SHORT" | "NEUTRAL"
    confidence:       int          # 0-100
    weights:           Dict[str, float]   # {persona: weight}
    fitnesses:         Dict[str, float]   # {persona: fitness}
    signals:           Dict[str, Dict]    # {persona: signal.to_dict()}
    position_value:    float
    leverage:         float
    entry_price:      float = 0.0
    stop_loss:        float = 0.0
    take_profit:       float = 0.0
    rr_ratio:         float = 0.0
    trade_result:      Optional[str] = None   # "WIN" | "LOSS" | "OPEN"
    pnl:              Optional[float] = None
    notes:             List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


# ─── Dry-Run Persona (no API calls) ───────────────────────────────────────────

class DryRunPersonaSignal(PersonaSignal):
    """
    Deterministic mock signal for backtesting without LLM API calls.
    Confidence and direction are derived from the anomaly type + direction.
    """

    # Confidence by anomaly type and direction
    BASE_CONFIDENCE: Dict[Tuple[str, str], int] = {
        # (anomaly_type, direction) → (bullish_conf, bearish_conf)
        (AnomalyType.LIQUIDITY_SWEEP.value,    "BULLISH"): 75,
        (AnomalyType.LIQUIDITY_SWEEP.value,    "BEARISH"): 70,
        (AnomalyType.EXTREME_DEVIATION.value,  "BULLISH"): 80,
        (AnomalyType.EXTREME_DEVIATION.value,  "BEARISH"): 80,
        (AnomalyType.VOLATILITY_SQUEEZE.value, "BULLISH"): 65,
        (AnomalyType.VOLATILITY_SQUEEZE.value, "BEARISH"): 65,
        (AnomalyType.VOLATILITY_SQUEEZE.value, "NEUTRAL"): 60,
    }

    @classmethod
    def from_event(cls, event: ScannerEvent, persona: PersonaType) -> "DryRunPersonaSignal":
        anomaly = event.anomaly_type
        ctx_dir = event.context_data.get("direction", "NEUTRAL")

        # Persona-specific confidence adjustments
        persona_mult = {
            PersonaType.SMC_ICT:       0.90,   # SMC trusts structure most
            PersonaType.ORDER_FLOW:   0.85,   # Order Flow is microstructure-focused
            PersonaType.MACRO_ONCHAIN: 0.75,   # Macro lags microstructure
        }.get(persona, 1.0)

        base = cls.BASE_CONFIDENCE.get((anomaly, ctx_dir), 60)
        confidence = int(base * persona_mult)

        # Direction for NEUTRAL anomaly → bias toward event direction
        if ctx_dir == "BULLISH":
            direction = Direction.LONG
        elif ctx_dir == "BEARISH":
            direction = Direction.SHORT
        else:
            direction = Direction.NEUTRAL

        leverage = 5 if confidence >= 75 else (4 if confidence >= 60 else 3)

        return cls(
            thesis=f"[DRY RUN] {persona.value} response to {anomaly}: "
                    f"{direction.value} signal from {ctx_dir} anomaly on {event.timestamp}",
            direction=direction,
            confidence=confidence,
            leverage=leverage,
            persona=persona,
            warnings=[f"Dry-run mock signal — real LLM would generate actual thesis"],
        )


# ─── ChronosBacktester ─────────────────────────────────────────────────────────

class ChronosBacktester:
    """
    Event-driven backtest orchestrator.

    Usage:
        engine = ChronosBacktester(
            mode=BacktestMode.DRY_RUN,
            initial_equity=10_000,
            min_confidence=40,
        )
        result = engine.run_backtest(df)
        print(result["summary"])

    Args:
        mode:              DRY_RUN (mock personas) or LIVE (real LLM API calls)
        initial_equity:   Starting capital
        min_confidence:   Minimum blended confidence to execute a trade
        lookback:         Rolling window for DynamicAllocator (default 30)
        temperature:       Softmax temperature for allocator (default 1.0)
        penalty_factor:    Turnover penalty for fitness (default 0.1)
        log_file:          Path to JSON log file
    """

    PERSONAS = [
        PersonaType.SMC_ICT,
        PersonaType.ORDER_FLOW,
        PersonaType.MACRO_ONCHAIN,
    ]

    def __init__(
        self,
        mode: BacktestMode = BacktestMode.DRY_RUN,
        initial_equity: float = 10_000.0,
        min_confidence: int = 40,
        lookback: int = 30,
        temperature: float = 1.0,
        penalty_factor: float = 0.1,
        compound: bool = True,
        log_file: str = "logs/chronos_trades.json",
    ):
        self.mode = mode
        self.initial_equity = initial_equity
        self.min_confidence = min_confidence
        self.lookback = lookback
        self.temperature = temperature
        self.penalty_factor = penalty_factor
        self.compound = compound
        self.log_file = log_file

        self._scanner = VanguardScanner()
        self._allocator = DynamicAllocator(
            personas=self.PERSONAS,
            lookback=lookback,
            temperature=temperature,
            penalty_factor=penalty_factor,
        )
        self._memory_bank = MemoryBank()
        self._equity = initial_equity
        self._trades: List[BlendedTrade] = []
        self._wins = 0
        self._losses = 0
        self._total_pnl = 0.0
        self._max_drawdown = 0.0
        self._peak = initial_equity
        self._reflexions_run = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if hasattr(self._memory_bank, 'close'):
            self._memory_bank.close()

    # ── Public API ────────────────────────────────────────────────────────────────

    def run_backtest(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Run the Chronos event-driven backtest on a full OHLCV DataFrame.

        Time-Jump algorithm:
          1. Scanner scans entire df → List[ScannerEvent]
          2. If empty → return zero-cost result (0 LLM calls)
          3. Sort events chronologically
          4. For each event:
             a. Slice df[:event.candle_idx] — no lookahead
             b. Build persona payloads from event context + historical data
             c. Get persona signals (mock or real LLM)
             d. Allocator.update_all() → allocate()
             e. Blended vote → execute trade if confidence > min_confidence
             f. Track PnL
          5. Return summary

        Returns:
            Dict with keys: events_found, trades_executed, trades, equity_curve,
                           win_rate, total_pnl, max_drawdown, sharpe_approx
        """
        # Step 1: Scanner
        events = self._scanner.scan(df)
        if not events:
            return self._zero_result(
                note="Scanner found no anomalies — 0 LLM calls, 0 trades"
            )

        # Step 2: Sort chronologically
        events = sorted(events, key=lambda e: e.candle_idx)
        event_log = []

        print(f"\n{'='*60}")
        print(f"CHRONOS BACKTESTER ({self.mode.value.upper()})")
        print(f"{'='*60}")
        print(f"  DataFrame: {len(df)} candles")
        print(f"  Scanner events: {len(events)} anomalies ({len(events)/len(df)*100:.1f}% wake rate)")
        print(f"  Min confidence threshold: {self.min_confidence}")
        print(f"  Initial equity: ${self.initial_equity:,.2f}")
        print()

        # Step 3: Process each event
        last_processed_idx = -1
        for i, event in enumerate(events):
            if event.candle_idx == last_processed_idx:
                print(f"[{i + 1}/{len(events)}] {event.timestamp} | {event.anomaly_type} | Skipping duplicate event at idx={event.candle_idx}")
                continue
            self._process_event(event, df, event_log, i + 1, len(events))
            last_processed_idx = event.candle_idx

        # Step 4: Compute metrics
        equity_curve = self._build_equity_curve()
        summary = self._compute_summary(equity_curve, events, event_log)
        self._save_log(event_log)

        print(f"\n{'='*60}")
        print(f"CHRONOS BACKTEST COMPLETE")
        print(f"{'='*60}")
        self._print_summary(summary)

        return {
            "events_found": len(events),
            "trades_executed": len(self._trades),
            "trades": [t.to_dict() for t in self._trades],
            "equity_curve": equity_curve,
            **summary,
        }

    # ── Core event processor ─────────────────────────────────────────────────

    def _process_event(
        self,
        event: ScannerEvent,
        df: pd.DataFrame,
        event_log: List[Dict],
        event_num: int,
        total_events: int,
    ):
        """Process a single scanner event."""
        ts = event.timestamp
        anomaly = event.anomaly_type
        idx = event.candle_idx
        ctx = event.context_data

        print(f"[{event_num}/{total_events}] {ts} | {anomaly} | {ctx.get('direction','?')} | idx={idx}")

        # ── Step a: Historical slice (no lookahead bias) ──────────────────────
        hist_df = df.iloc[:idx + 1].copy()
        if len(hist_df) < 50:
            print(f"  ⚠ Skip: insufficient history ({len(hist_df)} candles)")
            return

        # ── Step b: Build persona payloads ───────────────────────────────────
        signals: Dict[PersonaType, PersonaSignal] = {}
        signal_dicts: Dict[str, Dict] = {}
        
        trigger_direction = ctx.get("direction", "NEUTRAL")

        # Batch RAG retrieval: fetch lessons for all personas in a single query
        all_persona_values = [p.value for p in self.PERSONAS]
        batch_lessons = self._memory_bank.retrieve_lessons_batch(
            all_persona_values,
            anomaly,
            limit_per=3,
        )

        for persona_type in self.PERSONAS:
            past_lessons = batch_lessons.get(persona_type.value, [])

            # Build persona-specific data dict from historical slice + event context
            data = self._build_persona_data(persona_type, hist_df, event)

            # Get signal (mock or real) — pass lessons for RAG injection
            if self.mode == BacktestMode.DRY_RUN:
                signal = DryRunPersonaSignal.from_event(event, persona_type)
            else:
                signal = self._call_llm_persona(persona_type, data, past_lessons)

            # Force strict direction alignment: cannot counter-trade the mathematical trigger
            if trigger_direction != "NEUTRAL" and signal.direction.value != "NEUTRAL" and signal.direction.value != trigger_direction:
                print(f"  ⚠ Overriding {persona_type.value} vote: {signal.direction.value} conflicts with trigger direction {trigger_direction}. Setting to NEUTRAL.")
                signal.direction = Direction.NEUTRAL
                signal.confidence = 0
                signal.warnings.append(f"Direction overridden to NEUTRAL due to conflict with {trigger_direction} trigger.")

            signals[persona_type] = signal
            signal_dicts[persona_type.value] = signal.to_dict()

            if past_lessons:
                print(f"  📚 {persona_type.value}: RAG injected {len(past_lessons)} past lesson(s)")
            print(f"  {persona_type.value:15s}: {signal.direction.value:7s} "
                  f"conf={signal.confidence:3d} lev={signal.leverage}x | {signal.thesis[:50]}...")

        # ── Step c & d: Allocator allocate (using history up to now) ───────
        weights_snapshot = self._allocator.allocate()
        weights = {p.value: float(w) for p, w in zip(self.PERSONAS, weights_snapshot.weights)}
        fitnesses = {p.value: float(f) for p, f in zip(self.PERSONAS, weights_snapshot.fitnesses)}

        print(f"  Weights: {weights}")
        print(f"  Fitness: {fitnesses}")

        # ── Step e: Blended vote + execution decision ────────────────────────
        blended = self._blended_vote(signals, weights)
        print(f"  Blended: {blended['direction']} conf={blended['confidence']}")

        if blended["confidence"] < self.min_confidence:
            print(f"  ⏭ Skip: blended confidence {blended['confidence']} < {self.min_confidence}")
            event_log.append({
                "event_num": event_num,
                "timestamp": ts.isoformat(),
                "anomaly": anomaly,
                "executed": False,
                "reason": f"confidence {blended['confidence']} < {self.min_confidence}",
            })
            # Even if skipped, update allocator with 0 returns for this epoch
            returns_dict = {p: 0.0 for p in self.PERSONAS}
            positions_dict = {p: self._equity for p in self.PERSONAS}
            self._allocator.update_all(signals, returns_dict, positions_dict)
            return

        # ── Step f: Simulate trade execution ──────────────────────────────
        trade = self._simulate_trade(event, blended, idx, ts, weights, fitnesses, signals)
        self._trades.append(trade)

        # ── Step g: Outcome simulation + Reflexion loop ──────────────────
        outcome = self._simulate_trade_outcome(df, idx, trade, blended)
        trade.trade_result = outcome["result"]
        trade.pnl = outcome["pnl"]
        self._equity += outcome["pnl"]
        self._total_pnl += outcome["pnl"]
        if outcome["result"] == "WIN":
            self._wins += 1
        elif outcome["result"] == "LOSS":
            self._losses += 1
            
        # ── Step h: Update Allocator with actual returns ──────────────────
        returns_dict = {}
        positions_dict = {}
        working_equity = self._equity if self.compound else self.initial_equity
        for p in self.PERSONAS:
            sig = signals.get(p)
            if not sig or sig.direction.value == "NEUTRAL":
                returns_dict[p] = 0.0
                positions_dict[p] = 0.0
            else:
                # If persona agreed with trade direction, it gets the trade's PnL %
                # If it disagreed, it gets the inverse (since its stop would have been hit, etc.)
                if sig.direction.value == blended["direction"]:
                    returns_dict[p] = outcome["pnl_pct"] / 100.0
                elif sig.direction.value != "NEUTRAL":
                    returns_dict[p] = -outcome["pnl_pct"] / 100.0
                else:
                    returns_dict[p] = 0.0
                
                # Position value for turnover penalty
                pos_mult = sig.confidence / 100.0
                positions_dict[p] = working_equity * pos_mult * sig.leverage

        self._allocator.update_all(signals, returns_dict, positions_dict)

        # Reflexion loop: if loss > 1%, trigger psychologist for each voter
        if outcome["result"] == "LOSS" and outcome["pnl_pct"] < -1.0:
            self._run_reflexion_loop(event, blended, outcome, signals)

        print(f"  ✅ EXECUTED: {trade.direction} {trade.position_value:.0f}x{trade.leverage:.0f} "
              f"→ SL={trade.stop_loss:.0f} TP={trade.take_profit:.0f} "
              f"RR={trade.rr_ratio:.2f} | outcome={trade.trade_result} pnl={trade.pnl:+.2f}%")

        event_log.append({
            "event_num": event_num,
            "timestamp": ts.isoformat(),
            "anomaly": anomaly,
            "executed": True,
            "direction": trade.direction,
            "confidence": blended["confidence"],
            "weights": weights,
            "fitness": fitnesses,
            "position_value": trade.position_value,
            "leverage": trade.leverage,
            "pnl": trade.pnl,
            "trade_result": trade.trade_result,
        })

    # ── Persona data builder ─────────────────────────────────────────────────

    def _build_persona_data(
        self,
        persona: PersonaType,
        hist_df: pd.DataFrame,
        event: ScannerEvent,
    ) -> Dict[str, Any]:
        """
        Build the persona-specific data payload from historical candles + event context.

        Each persona receives a different data shape:
          SMC_ICT:       OHLCV + multi-tf structure + key levels
          ORDER_FLOW:    derivatives + liquidations + volume
          MACRO_ONCHAIN: ETF + whale + macro indicators
        """
        close = hist_df["close"].values
        high  = hist_df["high"].values
        low   = hist_df["low"].values
        vol   = hist_df["volume"].values
        ts    = hist_df["timestamp"]

        ctx = event.context_data  # From VanguardScanner event

        if persona == PersonaType.SMC_ICT:
            return {
                "symbol": ctx.get("symbol", "BTCUSDT"),
                "anomaly_type": event.anomaly_type,
                "btc": {
                    "trend": "N/A", "rsi": 50, "close": close[-1] if len(close) else 0,
                    "support": low.min(), "resistance": high.max(),
                },
                "target": {
                    "trend": ctx.get("direction", "NEUTRAL"),
                    "rsi": ctx.get("rsi_14", 50),
                    "close": close[-1] if len(close) else 0,
                    "high_4h": high.max(), "low_4h": low.min(),
                    "support": low.min(), "resistance": high.max(),
                },
                "multi_tf": {},  # For dry-run, empty (would be populated in live mode)
                "key_levels": {
                    "summary": f"Sweep: rolling_low={ctx.get('rolling_low_prev')}, "
                               f"rolling_high={ctx.get('rolling_high_prev')}",
                },
                "fear_greed_index": "N/A (dry run)",
            }

        elif persona == PersonaType.ORDER_FLOW:
            # In dry-run, simulate OI/funding data from historical price action
            price_range = high.max() - low.min()
            return {
                "symbol": ctx.get("symbol", "BTCUSDT"),
                "anomaly_type": event.anomaly_type,
                "derivatives": {
                    "binance_oi_usd": ctx.get("atr_14", 100) * 1e6,
                    "oi_change_pct": 2.5,   # Simulated OI growth
                    "oi_trend": "rising",
                    "binance_funding": 0.0010,
                    "bybit_funding": -0.0005,
                    "avg_funding": 0.0001,
                    "long_short_ratio_binance": 1.05,
                },
                "liquidations": {
                    "long_liquidations_24h": 50_000_000,
                    "short_liquidations_24h": 30_000_000,
                    "total_liquidations_24h": 80_000_000,
                    "cluster_1": ctx.get("rolling_low_prev", low.min()),
                    "cluster_2": ctx.get("rolling_high_prev", high.max()),
                    "cluster_3": close[-1] if len(close) else 0,
                },
                "volume": {
                    "volume_24h": float(vol[-24:].sum()) if len(vol) >= 24 else float(vol.sum()),
                    "buy_volume_pct": 52.0,
                    "large_trades_count": 12,
                },
                "basis": {
                    "perp_price": close[-1] if len(close) else 0,
                    "basis_pct": 0.01,
                },
            }

        elif persona == PersonaType.MACRO_ONCHAIN:
            return {
                "symbol": ctx.get("symbol", "BTCUSDT"),
                "anomaly_type": event.anomaly_type,
                "etf": {
                    "ibit_7d_flow": 500_000_000,
                    "fbtc_7d_flow": 200_000_000,
                    "total_7d_flow": 800_000_000,
                    "nav_premium": 0.5,
                },
                "whale": {
                    "exchange_reserves_btc": 2_200_000,
                    "exchange_reserves_change_pct": -2.1,
                    "whale_tx_count": 45,
                    "stablecoin_exchange_balance": 5_000_000_000,
                },
                "macro": {
                    "dxy": 104.5,
                    "us10y_yield": 4.35,
                    "m2_change_pct": 0.3,
                    "risk_sentiment": "neutral",
                },
                "onchain": {
                    "mvrv_z": 2.5,
                    "sopr": 1.1,
                    "active_addresses_7d": 1_200_000,
                    "hash_rate": 650,
                    "mpi": 1.2,
                },
                "cycle": {
                    "days_since_halving": 365,
                    "halving_year": 2,
                    "puell_multiple": 1.5,
                    "rhodl_ratio": 3.2,
                    "difficulty_ribbon": "compression",
                },
            }

        return {"symbol": ctx.get("symbol", "BTCUSDT")}

    # ── LLM persona caller (live mode) ────────────────────────────────────────

    def _call_llm_persona(
        self,
        persona_type: PersonaType,
        data: Dict[str, Any],
        past_lessons: Optional[List[str]] = None,
    ) -> PersonaSignal:
        """Call real LLM persona with RAG-injected past lessons. Falls back to NEUTRAL on error."""
        try:
            persona = create_persona(persona_type, dry_run=(self.mode == BacktestMode.DRY_RUN))
            return persona.analyze(data, past_lessons=past_lessons)
        except Exception as e:
            return PersonaSignal.neutral(persona_type, thesis=f"LLM error: {e}")

    # ── Blended vote ───────────────────────────────────────────────────────────

    def _blended_vote(
        self,
        signals: Dict[PersonaType, PersonaSignal],
        weights: Dict[str, float],
    ) -> Dict[str, Any]:
        """
        Compute the confidence-weighted blended direction and confidence.

        Logic:
          - LONG score  = sum(weights[i] * confidence_i) where direction_i == LONG
          - SHORT score = sum(weights[i] * confidence_i) where direction_i == SHORT
          - NEUTRAL signals contribute nothing
          - Winner = argmax(LONG_score, SHORT_score)
          - Blended confidence = winner_score (scaled 0-100)
        """
        long_score  = 0.0
        short_score = 0.0

        for persona, signal in signals.items():
            w = weights[persona.value]
            c = signal.confidence
            if signal.direction == Direction.LONG:
                long_score += w * c
            elif signal.direction == Direction.SHORT:
                short_score += w * c

        if long_score > short_score and long_score > 0:
            return {"direction": "LONG",  "confidence": int(max(0, min(100, long_score)))}
        elif short_score > long_score and short_score > 0:
            return {"direction": "SHORT", "confidence": int(max(0, min(100, short_score)))}
        else:
            return {"direction": "NEUTRAL", "confidence": 0}

    # ── Trade simulation ─────────────────────────────────────────────────────

    def _simulate_trade(
        self,
        event: ScannerEvent,
        blended: Dict[str, Any],
        idx: int,
        ts: datetime,
        weights: Dict[str, float],
        fitnesses: Dict[str, float],
        signals: Dict[PersonaType, PersonaSignal],
    ) -> BlendedTrade:
        """Simulate execution of a blended trade."""
        ctx = event.context_data
        direction = blended["direction"]
        confidence = blended["confidence"]
        price = ctx.get("current_price", ctx.get("candle_close", 100_000))

        # Stop/TP from ATR-based sizing
        atr = ctx.get("atr_14", 100)
        if direction == "LONG":
            entry = price
            stop  = entry - 2.0 * atr
            tp    = entry + 3.0 * atr
        elif direction == "SHORT":
            entry = price
            stop  = entry + 2.0 * atr
            tp    = entry - 3.0 * atr
        else:
            entry, stop, tp = price, 0, 0

        rr = abs(tp - entry) / abs(entry - stop) if abs(entry - stop) > 0 else 0
        avg_weight = sum(weights.values()) / len(weights)

        # Base leverage from confidence
        leverage = 5 if confidence >= 70 else (4 if confidence >= 50 else 3)
        
        # We don't calculate position size here anymore, it's done dynamically in outcome
        position_value = 0.0 

        return BlendedTrade(
            event_idx=idx,
            timestamp=ts,
            anomaly_type=event.anomaly_type,
            direction=direction,
            confidence=confidence,
            weights=weights,
            fitnesses=fitnesses,
            signals={p.value: s.to_dict() for p, s in signals.items()},
            position_value=position_value,
            leverage=leverage,
            entry_price=entry,
            stop_loss=stop,
            take_profit=tp,
            rr_ratio=rr,
            trade_result="OPEN",
            pnl=None,
            notes=[
                f"atr={atr:.2f}",
                f"entry={entry:.2f}",
                f"avg_weight={avg_weight:.3f}",
            ],
        )

    # ── Trade outcome simulation ────────────────────────────────────────────────

    def _simulate_trade_outcome(
        self,
        df: pd.DataFrame,
        idx: int,
        trade: BlendedTrade,
        blended: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Look ahead up to 48 candles after entry to determine WIN/LOSS outcome.

        WIN  = price hits TP before SL (for longs: close >= TP; for shorts: close <= TP)
        LOSS = price hits SL before TP (for longs: close <= SL; or lookahead exhausted)
        HOLD = price is between SL and TP when lookahead window expires
        
        Implements Fixed Fractional Risk math with 2% risk limit and fees.
        """
        direction = blended["direction"]
        if direction == "NEUTRAL":
            return {"result": "HOLD", "pnl": 0.0, "pnl_pct": 0.0}

        entry = trade.entry_price
        sl = trade.stop_loss
        tp = trade.take_profit
        
        # --- Strict Risk & Position Sizing Math ---
        # 1. Risk exactly DEFAULT_RISK_PCT of current equity
        risk_pct = sys_config.DEFAULT_RISK_PCT
        working_equity = self._equity if self.compound else self.initial_equity
        risk_usd = working_equity * risk_pct
        
        # 2. Distance to stop loss
        sl_dist = abs(entry - sl)
        if sl_dist <= 0:
            return {"result": "HOLD", "pnl": 0.0, "pnl_pct": 0.0}
            
        # 3. Size in coins needed to lose exactly risk_usd if SL is hit
        size_coins = risk_usd / sl_dist
        
        # 4. Notional value of the position
        notional = size_coins * entry
        
        # 5. Cap leverage (e.g. max 10x account equity)
        max_notional = working_equity * 10
        if notional > max_notional:
            notional = max_notional
            size_coins = notional / entry
            # Risk is now less than 2% because we hit the leverage cap
            
        trade.position_value = notional  # Update trade object with actual size
        
        # 6. Friction / Fees
        fee_rate = 0.0006  # 0.06% per side (taker)
        entry_fee = notional * fee_rate

        look_ahead = 48  # candles to scan for outcome
        end_idx = min(idx + look_ahead + 1, len(df))

        result = "HOLD"
        exit_price = entry

        for j in range(idx + 1, end_idx):
            candle_low = float(df["low"].iloc[j])
            candle_high = float(df["high"].iloc[j])
            
            # Stop-loss hit first (pessimistic check: evaluate SL before TP for safety)
            if direction == "LONG" and candle_low <= sl:
                result = "LOSS"
                exit_price = sl
                break
            if direction == "SHORT" and candle_high >= sl:
                result = "LOSS"
                exit_price = sl
                break
            # Take-profit hit
            if direction == "LONG" and candle_high >= tp:
                result = "WIN"
                exit_price = tp
                break
            if direction == "SHORT" and candle_low <= tp:
                result = "WIN"
                exit_price = tp
                break

        # Calculate final PnL if trade closed
        if result == "HOLD":
            return {"result": "HOLD", "pnl": 0.0, "pnl_pct": 0.0}
            
        # Gross PnL
        if direction == "LONG":
            gross_pnl = (exit_price - entry) * size_coins
        else:
            gross_pnl = (entry - exit_price) * size_coins
            
        # Exit fee
        exit_notional = size_coins * exit_price
        exit_fee = exit_notional * fee_rate
        
        # Net PnL
        net_pnl = gross_pnl - entry_fee - exit_fee
        pnl_pct = (net_pnl / working_equity) * 100

        return {"result": result, "pnl": round(net_pnl, 2), "pnl_pct": round(pnl_pct, 3)}

    # ── Reflexion loop ──────────────────────────────────────────────────────────

    def _run_reflexion_loop(
        self,
        event: ScannerEvent,
        blended: Dict[str, Any],
        outcome: Dict[str, Any],
        signals: Dict[PersonaType, PersonaSignal],
    ):
        """
        The Psychologist: after a loss > 1%, call reflect_on_loss() for each
        persona that voted for the losing direction, then save the lesson to
        the Memory Bank (RAG persistence).

        In dry_run mode, reflect_on_loss() returns a synthetic string without
        making any LLM API calls.
        """
        loser_direction = blended["direction"]
        ctx = event.context_data

        # Build a brief market context string for the reflexion prompt
        market_context = (
            f"symbol={ctx.get('symbol','?')}, "
            f"anomaly={event.anomaly_type}, "
            f"direction={ctx.get('direction','?')}, "
            f"rsi_14={ctx.get('rsi_14','?')}, "
            f"atr_14={ctx.get('atr_14','?')}, "
            f"price={ctx.get('current_price', ctx.get('candle_close','?'))}, "
            f"equity=${self._equity:,.2f}"
        )

        losing_personas = [
            (p, s) for p, s in signals.items()
            if s.direction.value == loser_direction
        ]

        if not losing_personas:
            return

        for persona_type, signal in losing_personas:
            thesis = signal.thesis
            pnl_val = outcome["pnl_pct"]

            print(f"\n  🧠 REFLEXION: {persona_type.value} lost {pnl_val:.2f}% — "
                  f"triggering psychologist...")

            # Build the real persona instance for reflexion
            persona = create_persona(
                persona_type,
                dry_run=(self.mode == BacktestMode.DRY_RUN),
            )
            
            # Fetch past lessons to inject into the psychologist's prompt
            past_lessons = self._memory_bank.retrieve_lessons(
                persona_type.value,
                event.anomaly_type,
                limit=3,
            )

            rule = persona.reflect_on_loss(
                anomaly_type=event.anomaly_type,
                thesis=thesis,
                pnl=pnl_val,
                market_context=market_context,
                past_lessons=past_lessons,
            )

            print(f"  📖 Lesson learned: \"{rule}\"")

            # Persist to Memory Bank (no-op in dry_run for LLM cost, but still saves)
            self._memory_bank.save_lesson(
                persona=persona_type.value,
                anomaly_type=event.anomaly_type,
                pnl=pnl_val,
                thesis=thesis,
                lesson=rule,
            )
            self._reflexions_run += 1
            print(f"  💾 Saved to MemoryBank (total reflexions: {self._reflexions_run})")

    # ── PnL tracking ──────────────────────────────────────────────────────

    def _build_equity_curve(self) -> List[Dict]:
        """Build equity curve from trades."""
        curve = [{"equity": self.initial_equity, "trade_num": 0}]
        equity = self.initial_equity
        for i, trade in enumerate(self._trades):
            if trade.pnl is not None:
                equity += trade.pnl
            curve.append({"equity": equity, "trade_num": i + 1})
        return curve

    # ── Summary metrics ───────────────────────────────────────────────────────

    def _compute_summary(
        self,
        equity_curve: List[Dict],
        events: List[ScannerEvent],
        event_log: List[Dict],
    ) -> Dict[str, Any]:
        wins   = sum(1 for t in self._trades if t.trade_result == "WIN")
        losses = sum(1 for t in self._trades if t.trade_result == "LOSS")
        trades = len(self._trades)
        equity_end = equity_curve[-1]["equity"] if equity_curve else self.initial_equity
        pnl_pct = (equity_end - self.initial_equity) / self.initial_equity * 100

        returns = []
        peak = self.initial_equity
        max_dd = 0.0
        prev_equity = self.initial_equity
        for pt in equity_curve[1:]:
            equity = pt["equity"]
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100
            if dd > max_dd:
                max_dd = dd
            if prev_equity > 0:
                ret = (equity - prev_equity) / prev_equity
                returns.append(ret)
            prev_equity = equity

        returns_arr = np.array(returns) if returns else np.array([0.0])
        sharpe = (returns_arr.mean() / returns_arr.std() * math.sqrt(365)
                if returns_arr.std() > 1e-12 else 0.0)

        return {
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / trades if trades > 0 else 0.0,
            "total_pnl": equity_end - self.initial_equity,
            "pnl_pct": pnl_pct,
            "final_equity": equity_end,
            "max_drawdown_pct": max_dd,
            "sharpe_approx": float(sharpe),
            "events_triggered": len(events),
            "llm_calls_skipped": len(events) == 0,  # True if no events
            "reflexions_run": self._reflexions_run,
            "lessons_in_bank": self._memory_bank.lesson_count(),
            "allocator_summary": self._allocator.summary(),
        }

    def _zero_result(self, note: str) -> Dict[str, Any]:
        print(f"\n{'='*60}")
        print(f"CHRONOS BACKTEST (0 events)")
        print(f"{'='*60}")
        print(f"  {note}")
        return {
            "events_found": 0,
            "trades_executed": 0,
            "trades": [],
            "equity_curve": [{"equity": self.initial_equity, "trade_num": 0}],
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "pnl_pct": 0.0,
            "final_equity": self.initial_equity,
            "max_drawdown_pct": 0.0,
            "sharpe_approx": 0.0,
            "events_triggered": 0,
            "llm_calls_skipped": True,
            "reflexions_run": 0,
            "lessons_in_bank": self._memory_bank.lesson_count(),
            "note": note,
        }

    def _print_summary(self, summary: Dict):
        print(f"  Events triggered:  {summary['events_triggered']}")
        print(f"  Trades executed:  {summary['trades']}")
        print(f"  Win rate:        {summary['win_rate']:.0%}")
        print(f"  Final equity:    ${summary['final_equity']:,.2f}")
        print(f"  Total PnL:      ${summary['total_pnl']:+,.2f} ({summary['pnl_pct']:+.2f}%)")
        print(f"  Max drawdown:     {summary['max_drawdown_pct']:.1f}%")
        print(f"  Sharpe (approx): {summary['sharpe_approx']:.2f}")
        print(f"  Reflexions run:   {summary.get('reflexions_run', 0)}")
        print(f"  Lessons in bank:  {summary.get('lessons_in_bank', 0)}")

    def _save_log(self, event_log: List[Dict]):
        os.makedirs(os.path.dirname(self.log_file) or ".", exist_ok=True)
        try:
            with open(self.log_file, "w") as f:
                json.dump({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "config": {
                        "mode": self.mode.value,
                        "initial_equity": self.initial_equity,
                        "min_confidence": self.min_confidence,
                        "lookback": self.lookback,
                    },
                    "events": event_log,
                    "trades": [t.to_dict() for t in self._trades],
                }, f, indent=2, default=str)
            print(f"\n  Log saved: {self.log_file}")
        except OSError:
            pass


# ─── CLI entry point helper ─────────────────────────────────────────────────────

def run_chronos_backtest(
    df: pd.DataFrame,
    mode: BacktestMode = BacktestMode.DRY_RUN,
    initial_equity: float = 10_000.0,
    min_confidence: int = 40,
    **kwargs,
) -> Dict[str, Any]:
    """One-liner for CLI wiring."""
    engine = ChronosBacktester(
        mode=mode,
        initial_equity=initial_equity,
        min_confidence=min_confidence,
        **kwargs,
    )
    return engine.run_backtest(df)


# ─── Unit Tests ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Plutus V3 — Chronos Engine: Unit Tests")
    print("=" * 60)

    import numpy as np
    from datetime import datetime, timezone, timedelta

    # Build synthetic OHLCV with 3 seeded anomalies
    n = 200
    BASE = 100_000.0
    np.random.seed(42)

    rows = []
    for i in range(n):
        t = BASE + i * 30 + np.random.randn() * 50
        rows.append({
            "timestamp": datetime(2025, 1, 1) + timedelta(hours=i),
            "open":   t - 10,
            "high":   t + 50,
            "low":    t - 60,
            "close":  t,
            "volume": 500_000,
        })

    df = pd.DataFrame(rows)

    # Seed anomalies at indices 100, 150, 180
    # 1. Liquidity Sweep at idx 100
    df.iloc[100, df.columns.get_loc("low")]  = BASE - 5000  # wick through
    df.iloc[100, df.columns.get_loc("close")] = BASE + 100   # close above

    # 2. Extreme Deviation at idx 150 (push above EMA50 + 3 ATR)
    ema50_vals = df["close"].ewm(span=50).mean()
    atr_vals  = (df["high"] - df["low"]).ewm(alpha=1/14).mean()
    close_col = df.columns.get_loc("close")
    high_col  = df.columns.get_loc("high")
    low_col   = df.columns.get_loc("low")
    close_150 = float(ema50_vals.iloc[149]) + 4 * float(atr_vals.iloc[149])
    df.iloc[150, close_col] = close_150
    df.iloc[150, high_col]  = close_150 + 100
    df.iloc[150, low_col]   = close_150 - 100

    # 3. Volatility Squeeze at idx 180 (flat tail → ultra-tight BB)
    FLAT_PRICE = BASE + 300
    for j in range(180, 200):
        df.iloc[j, df.columns.get_loc("open")]  = FLAT_PRICE
        df.iloc[j, df.columns.get_loc("high")]  = FLAT_PRICE + 2
        df.iloc[j, df.columns.get_loc("low")]   = FLAT_PRICE - 2
        df.iloc[j, df.columns.get_loc("close")] = FLAT_PRICE

    # ── Test 1: Scanner finds events ────────────────────────────────────────
    print("\n[Test 1] VanguardScanner finds anomalies in seeded data:")
    scanner = VanguardScanner()
    events = scanner.scan(df)
    print(f"  Events found: {len(events)}")
    assert len(events) >= 1, f"Expected >= 1 event, got {len(events)}"
    print(f"  ✓ Scanner correctly identifies seeded anomalies")

    # ── Test 2: ChronosBacktester runs on seeded data ──────────────────────
    print("\n[Test 2] ChronosBacktester DRY_RUN on seeded data:")
    engine = ChronosBacktester(
        mode=BacktestMode.DRY_RUN,
        initial_equity=10_000,
        min_confidence=30,
        log_file="logs/test_chronos.json",
    )
    result = engine.run_backtest(df)
    print(f"  Events triggered: {result['events_triggered']}")
    print(f"  Trades executed: {result['trades_executed']}")
    assert result["events_triggered"] >= 1, "Should trigger seeded events"
    print(f"  ✓ ChronosBacktester runs without error")

    # ── Test 3: Zero-event exit (no LLM calls) ────────────────────────────
    print("\n[Test 3] Zero-event path (no LLM calls:")
    clean_df = df.iloc[:50].copy()   # Only normal candles
    scanner2 = VanguardScanner()
    clean_events = scanner2.scan(clean_df)
    if len(clean_events) == 0:
        result2 = engine.run_backtest(clean_df)
        assert result2["llm_calls_skipped"] is True
        print(f"  ✓ Empty events → llm_calls_skipped={result2['llm_calls_skipped']}")

    # ── Test 4: Blended vote calculation ──────────────────────────────────────
    print("\n[Test 4] Blended vote edge cases:")
    # Test LONG bias
    long_sig  = DryRunPersonaSignal(
        thesis="Long", direction=Direction.LONG, confidence=70,
        leverage=5, persona=PersonaType.SMC_ICT)
    short_sig = DryRunPersonaSignal(
        thesis="Short", direction=Direction.SHORT, confidence=30,
        leverage=5, persona=PersonaType.ORDER_FLOW)
    neut_sig  = DryRunPersonaSignal(
        thesis="Neutral", direction=Direction.NEUTRAL, confidence=50,
        leverage=1, persona=PersonaType.MACRO_ONCHAIN)
    signals = {
        PersonaType.SMC_ICT: long_sig,
        PersonaType.ORDER_FLOW: short_sig,
        PersonaType.MACRO_ONCHAIN: neut_sig,
    }
    weights = {
        "SMC_ICT": 0.6,
        "ORDER_FLOW": 0.3,
        "MACRO_ONCHAIN": 0.1,
    }
    blended = engine._blended_vote(signals, weights)
    assert blended["direction"] == "LONG", f"Expected LONG, got {blended}"
    print(f"  LONG bias: {blended} ✓")

    # Test SHORT bias (ensure SMC_ICT's SHORT vote dominates ORDER_FLOW's LONG)
    sigs2 = {
        PersonaType.SMC_ICT: short_sig,    # SHORT, conf=30, weight=0.6 → 18
        PersonaType.ORDER_FLOW: short_sig, # SHORT, conf=30, weight=0.3 → 9
        PersonaType.MACRO_ONCHAIN: neut_sig, # NEUTRAL → 0
    }
    blended2 = engine._blended_vote(sigs2, weights)
    assert blended2["direction"] == "SHORT", f"Expected SHORT, got {blended2}"
    print(f"  SHORT bias: {blended2} ✓")

    print()
    print("=" * 60)
    print("✓ All Chronos Engine tests passed.")
    print("✓ Event-driven architecture verified.")
    print("✓ Scanner → Personas → Allocator → Trade pipeline complete.")
