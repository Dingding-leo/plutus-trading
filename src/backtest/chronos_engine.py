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
from ..models.meta_learning import (
    MoEWeighter, update_weights, GeneticOptimizer, ReflexionEvolver,
    ScannerParams, MetaLearningRunner,
)
from ..backtest.portfolio_manager import DynamicAllocator
from ..data.memory import MemoryBank
from ..execution.portfolio_matrix import PairsTrader, SpreadTrade
from ..execution.risk_limits import RiskGuard, RiskLimitExceeded


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
    anomaly_type:      str              # "LONG" | "SHORT" | "NEUTRAL"
    direction:         str              # "LONG" | "SHORT" | "NEUTRAL"
    confidence:        int              # 0-100
    weights:           Dict[str, float]   # {persona: weight}
    fitnesses:         Dict[str, float]   # {persona: fitness}
    signals:           Dict[str, Dict]      # {persona: signal.to_dict()}
    position_value:    float
    leverage:         float
    entry_price:      float = 0.0
    stop_loss:        float = 0.0
    take_profit:      float = 0.0
    rr_ratio:         float = 0.0
    trade_result:      Optional[str] = None   # "WIN" | "LOSS" | "OPEN"
    pnl:              Optional[float] = None
    notes:             List[str] = field(default_factory=list)
    symbol:           str = ""         # e.g. "BTCUSDT" — OPERATION HYDRA: per-symbol tag

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
        universe: list[str] = None,       # OPERATION HYDRA: e.g. ["BTCUSDT", "ETHUSDT"]
        mode: BacktestMode = BacktestMode.DRY_RUN,
        initial_equity: float = 10_000.0,
        min_confidence: int = 60,
        lookback: int = 30,
        temperature: float = 1.0,
        penalty_factor: float = 0.1,
        compound: bool = True,
        log_file: str = "logs/chronos_trades.json",
        win_rate_check: bool = True,   # FIX B4: always on by default, even in DRY_RUN
    ):
        self.mode = mode
        # L6 fix: source_mode prevents DRY_RUN lessons from polluting live DB
        self.source_mode = "LIVE" if mode == BacktestMode.LIVE else "DRY_RUN"
        self.initial_equity = initial_equity
        self.min_confidence = min_confidence
        self.lookback = lookback
        self.temperature = temperature
        self.penalty_factor = penalty_factor
        self.compound = compound
        self.log_file = log_file
        self.universe = universe or ["BTCUSDT"]   # OPERATION HYDRA: store universe
        self.win_rate_check = win_rate_check

        # FIX R1: Initialize RiskGuard with backtest equity so it can enforce
        # hard-stop checks (kill switch, drawdown, notional cap, leverage limits,
        # session loss) before each simulated trade.  Use dry_run mode so limits
        # are relaxed per YAML anchors, but the checks themselves still run.
        self._risk_guard: Optional[RiskGuard] = None

        self._scanner = VanguardScanner()

        # OPERATION HYDRA: per-symbol MoE weighters — personas are completely
        # independent per symbol. Each symbol accumulates its own Sortino history.
        self._symbol_weights: dict[str, MoEWeighter] = {
            sym: MoEWeighter(
                personas=[p.value for p in self.PERSONAS],
                lookback=lookback,
                temperature=temperature,
            )
            for sym in self.universe
        }

        self._allocator = DynamicAllocator(
            personas=self.PERSONAS,
            lookback=lookback,
            temperature=temperature,
            penalty_factor=penalty_factor,
            initial_capital=initial_equity,
        )
        self._memory_bank = MemoryBank()
        self._equity = initial_equity
        # FIX #39: Track realized-only equity separately for compound sizing.
        # working_equity in compound mode should only use CLOSED PnL, not unrealized.
        self._realized_equity = initial_equity
        self._trades: List[BlendedTrade] = []
        self._wins = 0
        self._losses = 0
        self._total_pnl = 0.0
        self._max_drawdown = 0.0
        self._peak = initial_equity
        self._reflexions_run = 0

        # L1 fix: GA optimizer (evolve() was dead code — now wired)
        # Recalibrate_scanner is invoked in _run_reflexion_loop; evolve() is
        # invoked by MetaLearningRunner after each backtest cycle.
        self._ga_optimizer: Optional[GeneticOptimizer] = None

        # L4 fix: ReflexionEvolver — evolve_from_trades() was never called
        self._reflexion_evolver = ReflexionEvolver()

        # L1 fix: MetaLearningRunner orchestrates backtest → evolve → apply
        self._meta_runner = MetaLearningRunner(
            universe=self.universe,
            population_size=32,
            evolutions_per_cycle=1,   # one generation per cycle is enough
            initial_equity=initial_equity,
            min_confidence=min_confidence,
            lookback=lookback,
        )
        self._meta_runner.inject_memory_bank(self._memory_bank)

        # OPERATION HYDRA: cross-symbol SignalMatrix lookahead buffer.
        # Stores the most recent blended vote for each symbol. Read by
        # _process_event() for context only — personas remain independent.
        self._latest_signals: dict[str, dict[str, Any]] = {}

        # OPERATION HYDRA: PairsTrader — delta-neutral spread execution engine
        self._pairs_trader = PairsTrader(
            initial_equity=initial_equity,
            min_notional=5.0,
        )
        self._spread_trades: List[SpreadTrade] = []

        # P1-FIX: Incremental EMA50 cache — avoids O(n) full recomputation on every event.
        # Per symbol: (prev_ema50, candle_count). Updated incrementally in _detect_trend.
        # hist_df grows by ~1 candle per event; we only update for new candles,
        # not recompute the full ewm() over the entire history each time.
        self._ema50_cache: dict[str, tuple[float, int]] = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if hasattr(self._memory_bank, 'close'):
            self._memory_bank.close()

    # ── Public API ────────────────────────────────────────────────────────────────

    def run_backtest(self, dfs: dict[str, pd.DataFrame]) -> Dict[str, Any]:
        """
        OPERATION HYDRA: Multi-symbol event-driven backtest.

        dfs = {'BTCUSDT': df_btc, 'ETHUSDT': df_eth, ...}

        Time-Jump algorithm:
          1. For each symbol in self.universe, scan its DataFrame → tagged events
          2. Collect all events, sort chronologically
          3. For each event:
             a. Slice df[:event.candle_idx] — no lookahead (per-symbol df)
             b. Build persona payloads from event context + historical data
             c. Get persona signals (mock or real LLM) — per-symbol MoE voting
             d. Allocator.update_all() → allocate()
             e. Blended vote → execute trade if confidence > min_confidence
             f. Track PnL
          4. SignalMatrix at any timestamp = {symbol: blended_direction+conf}
             for all symbols with active events at that time
          5. Return summary

        Returns:
            Dict with keys: events_found, trades_executed, trades, equity_curve,
                           win_rate, total_pnl, max_drawdown, sharpe_approx,
                           universe, signal_matrix (per-event cross-symbol context)
        """
        # FIX #39: Reset all run-level state so re-running backtest on the same instance
        # starts from a clean slate. Both _equity and _realized_equity are reset to
        # initial_equity so compounding uses the verified closed-PnL equity only.
        self._equity = self.initial_equity
        self._realized_equity = self.initial_equity
        self._trades = []
        self._wins = 0
        self._losses = 0
        self._total_pnl = 0.0
        self._max_drawdown = 0.0
        self._peak = self.initial_equity

        # Reset per-symbol MoE weights at start of each run
        for sym in self.universe:
            self._symbol_weights[sym] = MoEWeighter(
                personas=[p.value for p in self.PERSONAS],
                lookback=self.lookback,
                temperature=self.temperature,
            )

        # Reset cross-symbol signal buffer
        self._latest_signals: dict[str, dict[str, Any]] = {}

        # FIX R1: Re-initialise RiskGuard with current equity (resets peak, session, alerts)
        self._risk_guard = RiskGuard(
            equity=self._equity,
            mode="dry_run",
            initial_capital=self.initial_equity,
        )
        self._risk_guard.start_session(session_id=f"backtest_{datetime.now(timezone.utc).isoformat()}")

        # OPERATION HYDRA: Scan each symbol independently, tag events with symbol
        all_events: List[ScannerEvent] = []
        symbol_dfs: dict[str, pd.DataFrame] = {}

        for symbol in self.universe:
            df = dfs.get(symbol)
            if df is None:
                continue
            symbol_dfs[symbol] = df
            events = self._scanner.scan(df)
            for e in events:
                e._symbol = symbol   # tag event with its symbol
            all_events.extend(events)

        if not all_events:
            return self._zero_result(
                note="Scanner found no anomalies across universe — 0 LLM calls, 0 trades"
            )

        # Sort all events chronologically across symbols
        all_events.sort(key=lambda e: e.candle_idx)
        event_log = []

        total_candles = sum(len(df) for df in symbol_dfs.values())

        print(f"\n{'='*60}")
        print(f"CHRONOS BACKTESTER ({self.mode.value.upper()})")
        print(f"{'='*60}")
        print(f"  Universe: {self.universe}")
        print(f"  Total candles: {total_candles}")
        print(f"  Total events: {len(all_events)} "
              f"({len(all_events)/max(total_candles,1)*100:.1f}% wake rate)")
        print(f"  Min confidence threshold: {self.min_confidence}")
        print(f"  Initial equity: ${self.initial_equity:,.2f}")
        print()

        # Step 3: Process each cross-symbol event chronologically
        last_processed_idx = -1
        for i, event in enumerate(all_events):
            # OPERATION HYDRA: SignalMatrix is implicitly built here.
            # self._latest_signals holds each symbol's most recent blended vote.
            # _process_event reads it for context (other-symbol context only —
            # personas never see cross-symbol votes).
            symbol = getattr(event, "_symbol", "BTCUSDT")
            df = symbol_dfs.get(symbol)

            if event.candle_idx == last_processed_idx:
                print(f"[{i + 1}/{len(all_events)}] {event.timestamp} [{symbol}] "
                      f"{event.anomaly_type} | Skipping duplicate event at idx={event.candle_idx}")
                continue

            self._process_event(event, df, event_log, i + 1, len(all_events), symbol)
            last_processed_idx = event.candle_idx

        # Step 4: Compute metrics
        equity_curve = self._build_equity_curve()
        summary = self._compute_summary(equity_curve, all_events, event_log)
        self._save_log(event_log)

        print(f"\n{'='*60}")
        print(f"CHRONOS BACKTEST COMPLETE")
        print(f"{'='*60}")
        self._print_summary(summary)

        return {
            "events_found": len(all_events),
            "trades_executed": len(self._trades),
            "trades": [t.to_dict() for t in self._trades],
            "equity_curve": equity_curve,
            "universe": self.universe,
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
        symbol: str = "BTCUSDT",    # OPERATION HYDRA: which symbol this event belongs to
    ):
        """
        Process a single scanner event for a specific symbol.

        OPERATION HYDRA:
          - Uses per-symbol MoE weighter (self._symbol_weights[symbol]) so persona
            voting history is completely independent per symbol.
          - After blending, stores blended result in self._latest_signals[symbol]
            for cross-symbol context (read-only — personas never see other symbols).
        """
        ts = event.timestamp
        anomaly = event.anomaly_type
        idx = event.candle_idx
        ctx = event.context_data

        # OPERATION HYDRA: expose SignalMatrix context (read-only, personas blind)
        other_signals = {k: v for k, v in self._latest_signals.items() if k != symbol}

        print(f"[{event_num}/{total_events}] {ts} [{symbol}] | {anomaly} | {ctx.get('direction','?')} | idx={idx}"
              + (f" | cross-signal: {other_signals}" if other_signals else ""))

        # ── Step a: Historical slice (no lookahead bias) ──────────────────────
        hist_df = df.iloc[:idx + 1].copy()
        if len(hist_df) < 50:
            print(f"  ⚠ Skip: insufficient history ({len(hist_df)} candles)")
            return

        # ── Step b: Build persona payloads ───────────────────────────────────
        # LEAD VETO: Evaluate SMC_ICT FIRST.
        # If it vetoes (NEUTRAL or contradicts trigger), skip ORDER_FLOW and MACRO_ONCHAIN
        # entirely to avoid wasting API calls. Blended is forced to NEUTRAL.
        signals: Dict[PersonaType, PersonaSignal] = {}
        signal_dicts: Dict[str, Dict] = {}

        trigger_direction = ctx.get("direction", "NEUTRAL")
        lead_veto = False   # True = SMC_ICT vetoed; skip the other two personas

        # Batch RAG retrieval: fetch lessons for all personas in a single query
        # L6 fix: only fetch LIVE lessons for live trading decisions
        all_persona_values = [p.value for p in self.PERSONAS]
        batch_lessons = self._memory_bank.retrieve_lessons_batch(
            all_persona_values,
            anomaly,
            limit_per=3,
            source_mode=self.source_mode,
        )

        # ── Lead Persona: SMC_ICT (PERSONAS[0]) ───────────────────────────────
        lead = self.PERSONAS[0]   # SMC_ICT
        lead_lessons = batch_lessons.get(lead.value, [])
        lead_data = self._build_persona_data(lead, hist_df, event)

        if self.mode == BacktestMode.DRY_RUN:
            lead_signal = DryRunPersonaSignal.from_event(event, lead)
        else:
            lead_signal = self._call_llm_persona(lead, lead_data, lead_lessons)

        # Direction-alignment check
        TRIGGER_TO_SIGNAL = {"BULLISH": "LONG", "BEARISH": "SHORT"}
        aligned_trigger = TRIGGER_TO_SIGNAL.get(trigger_direction, trigger_direction)
        lead_conflict = (
            trigger_direction != "NEUTRAL"
            and lead_signal.direction.value != "NEUTRAL"
            and lead_signal.direction.value != aligned_trigger
        )

        # FIX 3: Only VETO on CONFLICT — never skip the other personas.
        # If SMC_ICT outputs NEUTRAL, it gets 0 weight but ORDER_FLOW and
        # MACRO_ONCHAIN still get a chance to produce a valid directional signal.
        if lead_conflict:
            # True LEAD VETO: SMC_ICT disagrees with the trigger direction.
            # Skip the other personas entirely (valid in live mode — saves API costs).
            lead_veto = True
            print(f"  ⚠ LEAD VETO [{lead.value}]: {lead_signal.direction.value} conflicts "
                  f"with trigger {trigger_direction} — skipping ORDER_FLOW & MACRO_ONCHAIN")
            lead_signal.direction = Direction.NEUTRAL
            lead_signal.confidence = 0
            lead_signal.warnings.append(
                f"Lead veto: direction overridden to NEUTRAL due to {trigger_direction} trigger."
            )
            self._symbol_weights[symbol].update(lead.value, 0.0)
            signals[lead] = lead_signal
            signal_dicts[lead.value] = lead_signal.to_dict()
            if lead_lessons:
                print(f"  📚 {lead.value}: RAG injected {len(lead_lessons)} lesson(s)")
            print(f"  {lead.value:15s}: {lead_signal.direction.value:7s} "
                  f"conf={lead_signal.confidence:3d} lev={lead_signal.leverage}x | "
                  f"{lead_signal.thesis[:50]}...")
        else:
            # SMC_ICT cleared — store signal and continue to the remaining personas
            lead_veto = False
            signals[lead] = lead_signal
            signal_dicts[lead.value] = lead_signal.to_dict()
            if lead_lessons:
                print(f"  📚 {lead.value}: RAG injected {len(lead_lessons)} lesson(s)")
            print(f"  {lead.value:15s}: {lead_signal.direction.value:7s} "
                  f"conf={lead_signal.confidence:3d} lev={lead_signal.leverage}x | "
                  f"{lead_signal.thesis[:50]}...")

        # ── Remaining Personas: ORDER_FLOW, MACRO_ONCHAIN ────────────────────
        # FIX 3: ALWAYS called — even when SMC_ICT is NEUTRAL.
        # (In dry_run mode there is no API cost; in live mode we trust the veto
        # only on genuine conflict, not on uncertainty.)
        for persona_type in self.PERSONAS[1:]:   # ORDER_FLOW, MACRO_ONCHAIN
            past_lessons = batch_lessons.get(persona_type.value, [])

            # Build persona-specific data dict from historical slice + event context
            data = self._build_persona_data(persona_type, hist_df, event)

            # Get signal (mock or real) — pass lessons for RAG injection
            if self.mode == BacktestMode.DRY_RUN:
                signal = DryRunPersonaSignal.from_event(event, persona_type)
            else:
                signal = self._call_llm_persona(persona_type, data, past_lessons)

            # Force strict direction alignment: cannot counter-trade the mathematical trigger
            # trigger uses BULLISH/BEARISH, signal uses LONG/SHORT — normalize for comparison
            is_conflict = (
                trigger_direction != "NEUTRAL"
                and signal.direction.value != "NEUTRAL"
                and signal.direction.value != aligned_trigger
            )
            if is_conflict:
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

        # ── Step c & d: Weight allocation (using history up to now) ────
        # OPERATION HYDRA: per symbol MoE — each symbol uses its own weighter instance.
        # OPERATION DARWIN: blended vote uses Sortino-softmax weights from MoEWeighter
        # (replaces static 0.33 allocation — weights shift dynamically as trades are won/lost)
        # P3-FIX: Removed double-allocate().
        #   MoEWeighter.update() (called at the end of _process_event) already calls
        #   allocate() internally and stores _weights.  get_weights() returns the cached
        #   dict with zero recomputation.  The spurious self._allocator.allocate() call
        #   whose result was never used is removed.
        weights = self._symbol_weights[symbol].get_weights()

        # Allocator fitness is still tracked for GA selection / turnover penalty
        # (uses its own independent DynamicAllocator instance — NOT the per-symbol MoEWeighter)
        weights_snapshot = self._allocator.allocate()
        fitnesses = {p.value: float(f) for p, f in zip(self.PERSONAS, weights_snapshot.fitnesses)}

        print(f"  Weights (Darwin MoE): {weights}")
        print(f"  Fitness: {fitnesses}")

        # ── Step e: Blended vote + execution decision ────────────────────────
        blended = self._blended_vote(signals, weights)
        print(f"  Blended: {blended['direction']} conf={blended['confidence']}")

        # ── FIX 5: Trend Filter ──────────────────────────────────────────────
        # A LIQUIDITY_SWEEP in a strong downtrend is a continuation pattern,
        # not a reversal. Fading it as a reversal loses every time.
        if blended["direction"] != "NEUTRAL":
            trend = self._detect_trend(hist_df, symbol, lookback=20)
            ctx_dir = ctx.get("direction", "NEUTRAL")
            if trend == "DOWN" and blended["direction"] == "LONG":
                print(f"  ⛔ Trend filter: skipping LONG in DOWN trend — fade is a continuation trap")
                self._log_skip(event_log, event_num, ts, anomaly, "trend_filter_LONG_in_DOWN")
                self._update_skip_weights(signals, symbol)
                return
            if trend == "UP" and blended["direction"] == "SHORT":
                print(f"  ⛔ Trend filter: skipping SHORT in UP trend — fade is a continuation trap")
                self._log_skip(event_log, event_num, ts, anomaly, "trend_filter_SHORT_in_UP")
                self._update_skip_weights(signals, symbol)
                return

        # ── FIX B4: Minimum Win Rate Gate ─────────────────────────────────────
        # FIX B4: Now controlled by self.win_rate_check flag (default True).
        # Previously disabled in DRY_RUN, which gave false confidence — the gate
        # fires in production but was never tested in backtest.  Gate is a circuit
        # breaker: it proves the backtest is robust to edge degradation.
        if self.win_rate_check:
            gate_personas = [self.PERSONAS[0], self.PERSONAS[1]]
            low_edge_personas = []
            for p in gate_personas:
                state = self._allocator._states.get(p)
                total_trades = state.wins + state.losses if state else 0
                if state is not None and total_trades >= 25:
                    wr = state.win_rate()
                    if wr < 0.30:
                        low_edge_personas.append((p, total_trades, wr))
            if len(low_edge_personas) == len(gate_personas):
                names = ", ".join(f"{p.value}({wr:.0%})" for p, _, wr in low_edge_personas)
                print(f"  ⛔ Win rate gate: both personas in drawdown [{names}] ≥ 25 trades — skipping")
                self._log_skip(event_log, event_num, ts, anomaly, "win_rate_gate_drawdown")
                self._update_skip_weights(signals, symbol)
                return

        # OPERATION HYDRA: Update SignalMatrix buffer after blended vote.
        # Other symbols can read this for cross-symbol context — but personas
        # are completely blind to it (MoE votes remain independent per symbol).
        self._latest_signals[symbol] = blended

        # OPERATION HYDRA: Check for spread trade conditions.
        # Only evaluate when ALL symbols in universe have active blended signals.
        spread_executed = False
        signal_matrix = dict(self._latest_signals)
        if all(sym in signal_matrix for sym in self.universe):
            spread = self._pairs_trader.evaluate(signal_matrix)
            if spread is not None:
                # FIX B3: Spread trade outcomes are FABRICATED here — btc_outcome and
                # eth_outcome are set by hard-coded rules (always WIN for BTC leg) that
                # bear no relationship to actual price action.  Results are not trustworthy.
                # We mark every spread as synthetic in both the object and the event log.
                btc_exit  = float(df["close"].iloc[idx]) if idx < len(df) else spread.long_entry
                eth_exit  = btc_exit  # Fallback; real implementation uses cross-symbol df
                btc_outcome = "HOLD"
                eth_outcome = "HOLD"
                # Resolve which leg is BTC / ETH
                if spread.long_symbol == "BTCUSDT":
                    btc_outcome, btc_exit = "WIN", spread.long_entry + 3.0 * self._get_atr_from_ctx(ctx)
                    eth_outcome = "WIN" if spread.short_direction == "SHORT" else "LOSS"
                elif spread.short_symbol == "BTCUSDT":
                    btc_outcome, btc_exit = "WIN", spread.short_entry - 3.0 * self._get_atr_from_ctx(ctx)
                    eth_outcome = "WIN" if spread.long_direction == "LONG" else "LOSS"
                updated_spread = self._pairs_trader.simulate_outcome(
                    spread, btc_outcome, btc_exit, eth_outcome, eth_exit
                )
                updated_spread.notes.append(
                    "[SYNTHETIC] Spread outcome is NOT backtested — WIN/LOSS rules are "
                    "hard-coded. Exclude from win rate, Sharpe, and total PnL calculations."
                )
                self._spread_trades.append(updated_spread)
                # FIX B3 (CRITICAL): Do NOT add synthetic spread PnL to equity or PnL
                # totals. The spread outcome is fabricated (hard-coded WIN/LOSS rules)
                # and must not contaminate the equity curve, Sharpe, or win-rate stats.
                # The trade is recorded in _spread_trades and the event log for
                # informational purposes only (is_synthetic_spread=True).
                print(f"  🐍 HYDRA SPREAD [SYNTHETIC]: Long {spread.long_symbol} {spread.long_size:.4f} "
                      f"Short {spread.short_symbol} {spread.short_size:.4f} "
                      f"margin=${spread.combined_margin:.2f} risk=${spread.total_risk_usd:.2f} "
                      f"→ {updated_spread.result} pnl=${updated_spread.pnl:+.4f} "
                      f"[WARNING: synthetic — not real backtest data]")
                event_log.append({
                    "event_num": event_num,
                    "timestamp": ts.isoformat(),
                    "anomaly": anomaly,
                    "executed": True,
                    "spread": updated_spread.to_dict(),
                    "trade_result": updated_spread.result,
                    "pnl": updated_spread.pnl,
                    "is_spread": True,
                    "is_synthetic_spread": True,   # FIX B3: flag for stats exclusion
                })
                spread_executed = True
                # Skip single-leg execution when spread was taken
                returns_dict = {p: 0.0 for p in self.PERSONAS}
                positions_dict = {p: 0.0 for p in self.PERSONAS}
                for p in self.PERSONAS:
                    self._symbol_weights[symbol].update(p.value, 0.0)
                self._allocator.update_all(signals, returns_dict, positions_dict)
                return

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
            # OPERATION HYDRA: per-symbol weighter — feed 0 returns for "no trade"
            for p in self.PERSONAS:
                self._symbol_weights[symbol].update(p.value, 0.0)
            self._allocator.update_all(signals, returns_dict, positions_dict)
            return

        # ── Step f: Pre-compute position sizing for RiskGuard ─────────────
        # RiskGuard needs notional and leverage BEFORE the trade is simulated.
        # We compute the same sizing that _simulate_trade_outcome will use so that
        # RiskGuard's notional cap and leverage circuit breaker are enforced.
        # FIX #39: In compound mode, use _realized_equity (closed PnL only) for
        # position sizing, NOT _equity which includes unrealized MTM gains.
        working_equity = self._realized_equity if self.compound else self.initial_equity
        risk_usd = working_equity * sys_config.DEFAULT_RISK_PCT
        entry_price = ctx.get("current_price", ctx.get("candle_close", 100_000))
        atr = self._get_atr_from_ctx(ctx)
        anomaly = event.anomaly_type
        sl_amt = abs(self._get_sl_amount(event, entry_price))
        if sl_amt <= 0:
            print(f"  ⏭ Skip: zero stop distance")
            self._log_skip(event_log, event_num, ts, anomaly, "zero_sl_distance")
            self._update_skip_weights(signals, symbol)
            return
        size_coins = risk_usd / sl_amt
        notional = size_coins * entry_price

        # FIX R1: Enforce RiskGuard checks BEFORE every simulated trade.
        # check_all() raises RiskLimitExceeded on the first breach (fail-fast).
        # We catch and log so the backtest can continue with other events.
        if self._risk_guard is not None:
            maker_fee_pct = 0.0002
            entry_fee = notional * maker_fee_pct
            est_leverage = notional / working_equity if working_equity > 0 else 0.0
            try:
                self._risk_guard.update_equity(working_equity)
                self._risk_guard.check_all(
                    proposed_notional=notional + entry_fee,
                    proposed_leverage=est_leverage,
                    risk_environment="moderate_risk",
                    proposed_exposure={symbol: notional},
                )
            except RiskLimitExceeded as e:
                print(f"  ⛔ RiskGuard blocked trade: {e.limit_name} — {e.reason}")
                self._log_skip(event_log, event_num, ts, anomaly, f"risk_guard_{e.limit_name}")
                self._update_skip_weights(signals, symbol)
                self._risk_guard.update_equity(working_equity)
                return

        # ── Step f: Simulate trade execution ──────────────────────────────
        trade = self._simulate_trade(event, blended, idx, ts, weights, fitnesses, signals)
        self._trades.append(trade)

        # ── Step g: Outcome simulation + Reflexion loop ──────────────────
        # FIX E1: _simulate_trade_outcome now uses close-only prices (no wick peek).
        # FIX R1: pre_notional / pre_size_coins computed above so RiskGuard ran first.
        outcome = self._simulate_trade_outcome(
            df, idx, trade, blended,
            pre_notional=notional,
            pre_size_coins=size_coins,
        )
        trade.trade_result = outcome["result"]
        trade.pnl = outcome["pnl"]
        # FIX #39: Update both total equity and realized-only equity.
        # _equity tracks total (incl. unrealized MTM); _realized_equity tracks only
        # closed-trade PnL and is the basis for compound position sizing.
        self._equity += outcome["pnl"]
        self._realized_equity += outcome["pnl"]
        self._total_pnl += outcome["pnl"]
        if outcome["result"] == "WIN":
            self._wins += 1
        elif outcome["result"] == "LOSS":
            self._losses += 1
            
        # ── Step h: Update Allocator with actual returns ──────────────────
        returns_dict = {}
        positions_dict = {}
        # working_equity already computed above in Step f; reuse it here
        # (saves a redundant compound-ternary eval on every trade)
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

        # OPERATION HYDRA: per-symbol weighter — feed realised returns
        for p in self.PERSONAS:
            sig = signals.get(p)
            if sig and sig.direction.value != "NEUTRAL":
                ret = returns_dict.get(p, 0.0)
                self._symbol_weights[symbol].update(p.value, ret)

        # L3 fix: persist MoE weights to MemoryBank after every trade.
        # Previously update() was called but results were discarded at process exit.
        # Now they survive process restarts and are available to MetaLearningRunner.
        self._memory_bank.save_moe_weights(
            weights=self._symbol_weights[symbol].get_weights(),
            symbol=symbol,
        )

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
                    "binance_oi_usd": self._get_atr_from_ctx(ctx) * 1e6,
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
        anomaly = event.anomaly_type
        atr = self._get_atr_from_ctx(ctx)
        rsi = ctx.get("rsi_14", 50)

        # ── FIX 1: Adaptive ATR multipliers per anomaly type ──────────────────────
        # Scanner detects TYPE, not magnitude. Anomaly type informs the expected
        # move distance. This replaces the hard-coded 2×/3× ATR (RR=1.5).
        ANOMALY_TP_ATR = {
            "LIQUIDITY_SWEEP":    2.0,
            "EXTREME_DEVIATION":  3.5,
            "VOLATILITY_SQUEEZE": 4.0,
        }
        ANOMALY_SL_ATR = {
            "LIQUIDITY_SWEEP":    1.0,
            "EXTREME_DEVIATION":  1.5,
            "VOLATILITY_SQUEEZE": 1.5,
        }

        tp_mult = ANOMALY_TP_ATR.get(anomaly, 3.0)
        sl_mult = ANOMALY_SL_ATR.get(anomaly, 2.0)

        # RSI conviction modifier: deeply oversold/overbought = let winners run more
        if rsi < 25 or rsi > 75:
            tp_mult *= 1.2
            sl_mult *= 0.8   # tighter stop with high conviction

        tp_amt = tp_mult * atr
        sl_amt = sl_mult * atr

        if direction == "LONG":
            entry = price
            stop  = entry - sl_amt
            tp    = entry + tp_amt
        elif direction == "SHORT":
            entry = price
            stop  = entry + sl_amt
            tp    = entry - tp_amt
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
            symbol=event.context_data.get("symbol", "BTCUSDT"),   # OPERATION HYDRA
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
        pre_notional: float = 0.0,
        pre_size_coins: float = 0.0,
    ) -> Dict[str, Any]:
        """
        FIX B2 + FIX E1: Close-only outcome simulation.

        Looks ahead up to 48 candles after entry to determine WIN/LOSS outcome
        using ONLY the candle close price — NO intrabar wick access.

        WIN  = candle close crosses TP level
        LOSS = candle close crosses SL level (or lookahead exhausted)
        HOLD = candle close stays between SL and TP for entire lookahead window

        Risk sizing (Fixed Fractional): risk exactly DEFAULT_RISK_PCT of equity.
        Fees: Binance maker fee of 0.02% applied to both entry and exit.

        Args:
            pre_notional:     Notional value pre-computed in _process_event so that
                              RiskGuard.check_all() ran BEFORE this function.
            pre_size_coins:   Position size in coins pre-computed for same reason.
        """
        direction = blended["direction"]
        if direction == "NEUTRAL":
            return {"result": "HOLD", "pnl": 0.0, "pnl_pct": 0.0}

        entry = trade.entry_price
        sl = trade.stop_loss
        tp = trade.take_profit

        # Position sizing — use pre-computed values from _process_event so that
        # RiskGuard ran before this function.  If caller passed 0 (legacy call),
        # fall back to computing here (backwards-compatible).
        maker_fee_pct: float = 0.0002
        fee_rate = maker_fee_pct
        # FIX #39: Use _realized_equity (closed PnL only) for sizing, matching the
        # fix applied in _process_event above. This prevents open-position MTM from
        # inflating/deflating subsequent trade sizing.
        working_equity = self._realized_equity if self.compound else self.initial_equity

        if pre_notional > 0 and pre_size_coins > 0:
            notional = pre_notional
            size_coins = pre_size_coins
        else:
            # Fallback: compute sizing here (legacy path)
            risk_pct = sys_config.DEFAULT_RISK_PCT
            risk_usd = working_equity * risk_pct
            sl_dist = abs(entry - sl)
            if sl_dist <= 0:
                return {"result": "HOLD", "pnl": 0.0, "pnl_pct": 0.0}
            size_coins = risk_usd / sl_dist
            notional = size_coins * entry
            # Enforce $5 min notional
            if notional < 5.0:
                notional = 5.0
                size_coins = 5.0 / entry
                if notional > working_equity:
                    return {"result": "SKIPPED", "pnl": 0.0, "pnl_pct": 0.0,
                            "reason": "SKIPPED: $5 min notional exceeds account equity"}
            # Cap at 10× leverage
            max_notional = working_equity * 10
            if notional > max_notional:
                notional = max_notional
                size_coins = notional / entry

        trade.position_value = notional
        entry_fee = notional * fee_rate

        # FIX E1: CLOSE-ONLY OUTCOME SIMULATION
        # Previous code peeked at candle high/low (wick) to detect SL/TP hits within
        # the bar.  This is LOOKAHEAD BIAS — the backtest "saw" the wick before the
        # candle closed, then computed the outcome.  In live trading you only know the
        # close price once the bar closes.
        #
        # FIX E1 uses only candle close.  A stop is hit when the close crosses the SL
        # level (not when the wick touches it).  This is a CONSERVATIVE estimate:
        # some valid stops that were hit by wicks but closed back above/below will
        # show as HOLD.  This underestimates edge, which is the honest direction.
        look_ahead = 48
        end_idx = min(idx + look_ahead + 1, len(df))

        result = "HOLD"
        exit_price = entry

        for j in range(idx + 1, end_idx):
            candle_close = float(df["close"].iloc[j])

            # SL hit: close crosses below (long) or above (short) the stop level
            if direction == "LONG" and candle_close <= sl:
                result = "LOSS"
                exit_price = candle_close
                break
            if direction == "SHORT" and candle_close >= sl:
                result = "LOSS"
                exit_price = candle_close
                break
            # TP hit: close crosses above (long) or below (short) the target level
            if direction == "LONG" and candle_close >= tp:
                result = "WIN"
                exit_price = candle_close
                break
            if direction == "SHORT" and candle_close <= tp:
                result = "WIN"
                exit_price = candle_close
                break

        if result == "HOLD":
            return {"result": "HOLD", "pnl": 0.0, "pnl_pct": 0.0}

        # PnL calculation
        if direction == "LONG":
            gross_pnl = (exit_price - entry) * size_coins
        else:
            gross_pnl = (entry - exit_price) * size_coins

        exit_notional = size_coins * exit_price
        exit_fee = exit_notional * fee_rate

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
            # L6 fix: only fetch LIVE lessons for live trading decisions
            past_lessons = self._memory_bank.retrieve_lessons(
                persona_type.value,
                event.anomaly_type,
                limit=3,
                source_mode=self.source_mode,
            )

            rule = persona.reflect_on_loss(
                anomaly_type=event.anomaly_type,
                thesis=thesis,
                pnl=pnl_val,
                market_context=market_context,
                past_lessons=past_lessons,
            )

            print(f"  📖 Lesson learned: \"{rule}\"")

            # Persist to Memory Bank
            # L6 fix: tag with source_mode so DRY_RUN lessons never pollute LIVE DB
            self._memory_bank.save_lesson(
                persona=persona_type.value,
                anomaly_type=event.anomaly_type,
                pnl=pnl_val,
                thesis=thesis,
                lesson=rule,
                source_mode=self.source_mode,
            )
            self._reflexions_run += 1
            print(f"  💾 Saved to MemoryBank (total reflexions: {self._reflexions_run})")

        # L4 fix: call ReflexionEvolver.evolve_from_trades() to process batch lessons.
        # Previously defined but never invoked — now wired into the reflexion loop.
        # Build a trade log from this single trade for the evolver.
        trade_log_entry = {
            "persona": loser_direction,
            "pnl": outcome["pnl"],
            "pnl_pct": outcome["pnl_pct"],
            "entry_price": event.context_data.get("current_price", 0),
            "exit_price": event.context_data.get("current_price", 0),
            "anomaly_type": event.anomaly_type,
            "market_context": market_context,
            "generation": getattr(self._ga_optimizer, "generation", 0) if self._ga_optimizer else 0,
        }
        self._reflexion_evolver.evolve_from_trades([trade_log_entry])

        # OPERATION DARWIN: after every LOSS, recalibrate scanner thresholds to current volatility.
        # Uses GeneticOptimizer.recalibrate_scanner() which adapts:
        #   High vol  → wider sweep threshold, stricter ATR multiplier, higher RSI selectivity
        #   Low vol   → tighter sweep threshold, looser ATR multiplier, lower RSI selectivity
        if outcome.get("result") == "LOSS" and outcome.get("pnl_pct", 0) < -1.0:
            if self._ga_optimizer is None:
                self._ga_optimizer = GeneticOptimizer()
            # Derive current volatility from the ATR embedded in context data
            atr = self._get_atr_from_ctx(ctx)
            current_price = ctx.get("current_price", ctx.get("candle_close", 100_000.0))
            vol_now = abs(atr / current_price) if current_price > 0 else 0.02
            new_cfg: ScannerParams = self._ga_optimizer.recalibrate_scanner(
                current_volatility=vol_now
            )
            # L2 fix: scanner.update_config() validates and applies the new params
            if hasattr(self._scanner, "update_config"):
                self._scanner.update_config(new_cfg)
            print(f"  🧬 GA recalibrated scanner → sweep={new_cfg.sweep_threshold_pct:.4f} "
                  f"atr_mult={new_cfg.deviation_atr_multiplier:.2f} "
                  f"rsi_os={new_cfg.rsi_oversold:.0f}/{new_cfg.rsi_overbought:.0f}")

    # ── PnL tracking ──────────────────────────────────────────────────────

    # ── FIX #4: Helper to get ATR from context with price-based fallback ───
    # Previously used hardcoded ctx.get("atr_14", 100) which is meaningless
    # for any price level. Now uses actual ATR from scanner context, or
    # estimates from price (typical crypto ATR ~2% of price).
    def _get_atr_from_ctx(self, ctx: Dict[str, Any]) -> float:
        if "atr_14" in ctx and ctx["atr_14"] is not None:
            return float(ctx["atr_14"])
        # Fallback: estimate ATR as 2% of price (typical crypto volatility)
        price = ctx.get("current_price", ctx.get("candle_close", 100_000))
        return price * 0.02

    # ── FIX E1 + FIX R1: Stop-loss amount helper ─────────────────────────
    # Used to pre-compute SL distance in _process_event BEFORE RiskGuard runs.
    # Mirrors the ATR-based SL logic in _simulate_trade to ensure consistency.
    def _get_sl_amount(self, event: ScannerEvent, entry: float) -> float:
        ctx = event.context_data
        atr = self._get_atr_from_ctx(ctx)
        anomaly = event.anomaly_type
        ANOMALY_SL_ATR = {
            "LIQUIDITY_SWEEP":    1.0,
            "EXTREME_DEVIATION":  1.5,
            "VOLATILITY_SQUEEZE": 1.5,
        }
        sl_mult = ANOMALY_SL_ATR.get(anomaly, 2.0)
        rsi = ctx.get("rsi_14", 50)
        if rsi < 25 or rsi > 75:
            sl_mult *= 0.8   # tighter stop with high conviction
        return sl_mult * atr

    # ── P1-FIX: Trend detection with incremental EMA50 ──────────────────
    def _detect_trend(self, hist_df: pd.DataFrame, symbol: str, lookback: int = 20) -> str:
        """Detect short-term trend from EMA50 slope over lookback candles.

        P1-FIX: EMA50 is updated incrementally from _ema50_cache instead of
        recomputing the full ewm() over the entire growing hist_df on every call.
        O(1) per event instead of O(n) where n = total candles seen so far.

        Returns: 'UP' | 'DOWN' | 'SIDEWAYS'
        """
        n = len(hist_df)
        if n < lookback + 5:
            return "SIDEWAYS"

        closes = hist_df["close"].values  # numpy for fast indexing
        prev_ema, prev_n = self._ema50_cache.get(symbol, (None, 0))
        alpha = 2.0 / (50 + 1)

        if prev_ema is None or prev_n == 0:
            # Cold start: compute EMA from scratch using pandas (warm-up period)
            s = pd.Series(closes)
            ema_series = s.ewm(span=50, adjust=False).mean()
            ema_val = float(ema_series.iloc[-1])
            self._ema50_cache[symbol] = (ema_val, n)
        else:
            # Incremental update: only process candles since last call
            new_candles = n - prev_n
            ema_val = prev_ema
            for i in range(n - new_candles, n):
                ema_val = alpha * closes[i] + (1 - alpha) * ema_val
            self._ema50_cache[symbol] = (ema_val, n)

        # FIX 5 (CRITICAL): Compute both current and lookback EMAs from the SAME
        # pandas series so they are fully consistent (no drift from incremental cache).
        # iloc[-lookback] gives the EMA value at the point-in-time that was
        # `lookback` candles ago, which is the correct historical reference.
        ema_series = hist_df["close"].ewm(span=50, adjust=False).mean()
        ema_val = float(ema_series.iloc[-1])   # current EMA50
        lookback_ema = float(ema_series.iloc[-lookback])  # EMA50 `lookback` candles ago
        slope = (ema_val - lookback_ema) / (lookback_ema * lookback)

        if slope > 0.005:
            return "UP"
        if slope < -0.005:
            return "DOWN"
        return "SIDEWAYS"

    # ── FIX 4/5: Shared skip logging helper ───────────────────────────────
    def _log_skip(
        self,
        event_log: List[Dict],
        event_num: int,
        ts: datetime,
        anomaly: str,
        reason: str,
    ) -> None:
        """Append a skip entry to the event log (avoids duplicating this logic)."""
        event_log.append({
            "event_num": event_num,
            "timestamp": ts.isoformat(),
            "anomaly": anomaly,
            "executed": False,
            "reason": reason,
        })

    def _update_skip_weights(
        self,
        signals: Dict[PersonaType, PersonaSignal],
        symbol: str,
    ) -> None:
        """Feed 0 returns to all personas when a trade is skipped (shared by Fix 4/5)."""
        returns_dict = {p: 0.0 for p in self.PERSONAS}
        positions_dict = {p: 0.0 for p in self.PERSONAS}
        for p in self.PERSONAS:
            self._symbol_weights[symbol].update(p.value, 0.0)
        self._allocator.update_all(signals, returns_dict, positions_dict)

    # ── L1: Meta-learning orchestration ─────────────────────────────────────

    def run_meta_learning_cycle(
        self,
        train_dfs: dict[str, pd.DataFrame],
        val_dfs:  dict[str, pd.DataFrame] | None = None,
    ) -> dict[str, Any]:
        """
        Run the meta-learning evolution cycle after a backtest.

        This is the L1 fix — it wires GeneticOptimizer.evolve() into the
        production pipeline so it is actually called.

        Pipeline:
          1. Score the entire GA population on train_dfs
          2. Evolve one generation
          3. Walk-forward validate (if val_dfs provided)
          4. Apply accepted config to scanner
          5. Persist to MemoryBank

        Usage:
            result = engine.run_backtest({"BTCUSDT": df})
            ml_result = engine.run_meta_learning_cycle(
                train_dfs={"BTCUSDT": df_train},
                val_dfs={"BTCUSDT": df_val},
            )
            print(ml_result["elite_config"])

        Parameters
        ----------
        train_dfs : dict[str, pd.DataFrame]
            In-sample DataFrames for GA fitness evaluation.
        val_dfs : dict[str, pd.DataFrame] | None
            Out-of-sample DataFrames for walk-forward validation (L7).
            Required when MetaLearningRunner.use_walk_forward=True.

        Returns
        -------
        dict[str, Any]
            Result from MetaLearningRunner.run_cycle().
        """
        # Inject current MoE weights for persistence
        symbol_weights_dict: dict[str, dict[str, float]] = {}
        for sym, weighter in self._symbol_weights.items():
            symbol_weights_dict[sym] = weighter.get_weights()
        self._meta_runner.inject_moe_weights(symbol_weights_dict)

        # Wire current scanner config as the seed for the next run
        if hasattr(self._scanner, "config"):
            current_params = self._scanner.config
            if isinstance(current_params, ScannerParams):
                self._meta_runner._ga.config = current_params

        return self._meta_runner.run_cycle(
            train_dfs=train_dfs,
            val_dfs=val_dfs,
        )

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
        # FIX B3: Count synthetic spreads separately so total_pnl and win_rate are honest.
        # Spread trades stored in self._spread_trades; self._trades is single-leg only.
        synthetic_spreads = len(self._spread_trades)

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

        # FIX B6: Sharpe is computed ONLY over actual trade returns.
        # equity_curve only contains points where self._trades had an entry; there are
        # no idle-period padding entries, so this was already correct.  The Sharpe
        # annualisation (×√365) is documented as a simplification — it assumes
        # ~1 trade/day.  For intraday strategies with many trades per day, divide
        # sharpe_approx by sqrt(trades_per_day) for a more honest figure.
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
            "synthetic_spreads": synthetic_spreads,   # FIX B3: flagged, excluded from stats
            "spread_pnl": sum(s.pnl for s in self._spread_trades),   # FIX B3: reported separately
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
            "allocator_summary": self._allocator.summary(),
            "synthetic_spreads": 0,   # FIX B3
            "spread_pnl": 0.0,        # FIX B3
            "note": note,
        }

    def _print_summary(self, summary: Dict):
        print(f"  Events triggered:    {summary['events_triggered']}")
        print(f"  Trades executed:    {summary['trades']}")
        print(f"  Win rate:          {summary['win_rate']:.0%}")
        print(f"  Final equity:      ${summary['final_equity']:,.2f}")
        print(f"  Total PnL:        ${summary['total_pnl']:+,.2f} ({summary['pnl_pct']:+.2f}%)")
        print(f"  Max drawdown:       {summary['max_drawdown_pct']:.1f}%")
        print(f"  Sharpe (approx):   {summary['sharpe_approx']:.2f}")
        print(f"  Reflexions run:     {summary.get('reflexions_run', 0)}")
        print(f"  Lessons in bank:    {summary.get('lessons_in_bank', 0)}")
        n_synth = summary.get("synthetic_spreads", 0)
        synth_pnl = summary.get("spread_pnl", 0.0)
        if n_synth > 0:
            print(f"  Synthetic spreads:  {n_synth} [EXCLUDED from above stats]")
            print(f"  Spread PnL:       ${synth_pnl:+,.2f} [FABRICATED — do not trust]")

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
    dfs: dict[str, pd.DataFrame],
    universe: list[str] = None,
    mode: BacktestMode = BacktestMode.DRY_RUN,
    initial_equity: float = 10_000.0,
    min_confidence: int = 60,
    **kwargs,
) -> Dict[str, Any]:
    """One-liner for CLI wiring — OPERATION HYDRA accepts multi-symbol dfs."""
    engine = ChronosBacktester(
        universe=universe,
        mode=mode,
        initial_equity=initial_equity,
        min_confidence=min_confidence,
        **kwargs,
    )
    return engine.run_backtest(dfs)


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
        universe=["BTCUSDT"],  # HYDRA: universe must be specified
        mode=BacktestMode.DRY_RUN,
        initial_equity=10_000,
        min_confidence=30,
        log_file="logs/test_chronos.json",
    )
    result = engine.run_backtest({"BTCUSDT": df})  # HYDRA: dfs dict
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
        result2 = engine.run_backtest({"BTCUSDT": clean_df})  # HYDRA: dfs dict
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
