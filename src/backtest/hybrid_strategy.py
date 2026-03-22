"""
Hybrid Rule + LLM Strategy — Plutus V2.

The LLM is promoted from "Trade Signal Generator" to "Macro Risk Officer".
Technical rules generate trade setups; the LLM provides an Execution Gate
(macro_regime, btc_strength, volatility_warning) that must align before
a trade fires. Pure-rule fallback when LLM is disabled.

Architecture:
  - WorkflowStrategy: generates technical setups (unchanged)
  - HybridWorkflowStrategy: wraps WorkflowStrategy + LLM Execution Gate
  - get_llm_macro_context(): calls LLM, returns macro context dict

Three-Phase Decision Framework (CLAUDE.md Section 11):
  PHASE 1 (未动 — No trigger) → NO TRADE
  PHASE 2 (冲击  — Shock)      → WAIT; define trigger for PHASE 3
  PHASE 3 (确认 — Confirmed)  → EXECUTE if Execution Gate passes

Execution Gate (all four must be True):
  1. structure_break  — structure invalidation has been crossed
  2. macro_aligned     — macro regime supports the direction
  3. invalidation_clear — stop-loss level is mathematically defined
  4. RR >= 1.5         — risk/reward including extension targets >= 1.5
"""

from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from .strategy import WorkflowStrategy, StrategyConfig
from ..data.llm_client import get_llm_macro_context
from ..data.coin_tiers import is_major, normalize_symbol


# ─── Logging ────────────────────────────────────────────────────────────────

LLM_LOG_FILE = "logs/llm_macro_context.json"


# ─── Three-Phase Decision Framework (CLAUDE.md Section 11) ──────────────────────


class ThreePhase(Enum):
    """
    The three-phase decision states from the Execution Framework.

    PHASE 1 — 未动 (No Movement): No trigger is present. No trade.
    PHASE 2 — 冲击 (Shock):      A trigger has fired but not yet confirmed.
                                    WAIT — define the confirmation condition.
    PHASE 3 — 确认 (Confirmed):    Trigger confirmed. Execute if all gates pass.

    Mindset (CLAUDE.md Section 11.8):
        没动 → 不做       (no movement → no trade)
        刚动 → 不追       (just moved → don't chase)
        动完确认 → 必须做 (movement confirmed → MUST execute)
    """
    PHASE_1_NO_TRIGGER  = "PHASE_1_NO_TRIGGER"
    PHASE_2_SHOCK      = "PHASE_2_SHOCK"
    PHASE_3_CONFIRMED  = "PHASE_3_CONFIRMED"


def _log_macro_context(ctx: dict, symbol: str, trade_decision: str, timestamp: str):
    """Append LLM macro context decision to log file."""
    import os, json
    os.makedirs("logs", exist_ok=True)
    entry = {
        "timestamp": timestamp,
        "symbol": symbol,
        "macro_regime": ctx.get("macro_regime", "UNKNOWN"),
        "btc_strength": ctx.get("btc_strength", "UNKNOWN"),
        "volatility_warning": ctx.get("volatility_warning", "UNKNOWN"),
        "trade_decision": trade_decision,  # "ALLOWED" | "BLOCKED"
        "block_reason": ctx.get("_block_reason", None),
    }
    try:
        with open(LLM_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ─── Hybrid Strategy ─────────────────────────────────────────────────────────


class HybridWorkflowStrategy:
    """
    Wraps WorkflowStrategy with an LLM-driven Execution Gate.

    Execution Gate logic:
      1. WorkflowStrategy generates a technical setup (check_entry returns a dict)
      2. HybridWorkflowStrategy calls get_llm_macro_context()
      3. Gate evaluation:
         - ALT LONG + (macro_regime == RISK_OFF OR btc_strength == WEAK) → BLOCK
         - macro_regime == RISK_OFF AND btc_strength == WEAK → BLOCK ALL ALTS
         - volatility_warning == HIGH → force pos_mult = 0.3 (most conservative)
      4. If gate passes → execute the trade with adjusted pos_mult

    The underlying WorkflowStrategy is never modified.
    When use_llm=False, behaves identically to WorkflowStrategy.
    """

    def __init__(
        self,
        config: StrategyConfig = None,
        use_llm: bool = False,
        llm_provider: str = "minimax",
        llm_cache_seconds: int = 3600,
    ):
        # Pure-rule engine — never modified
        self._core = WorkflowStrategy(config)

        self._use_llm = use_llm
        self._llm_provider = llm_provider
        self._llm_cache_seconds = llm_cache_seconds

        # LLM context cache: refreshed per symbol per ~1h by default
        self._llm_ctx: Dict[str, dict] = {}
        self._llm_ctx_ts: Dict[str, datetime] = {}

        # Passthrough: delegate config to core (read-only)
        self.config = self._core.config

    # ── Passthrough properties ────────────────────────────────────────────────

    @property
    def states(self):
        return self._core.states

    @property
    def btc_signal(self):
        return self._core.btc_signal

    @property
    def market_context(self):
        return self._core.market_context

    # ── LLM Execution Gate ────────────────────────────────────────────────────

    def _get_llm_context(
        self,
        symbol: str,
        analysis: dict,
        btc_analysis: dict,
        timestamp: datetime,
    ) -> dict:
        """
        Fetch (cached) LLM macro context for the given symbol.

        Cache TTL defaults to 3600 s (1 h) so we don't spam the LLM on
        every single candle during backtesting. Live commands use a
        shorter TTL (passed in by CLI).
        """
        # Force cache refresh on first call or after TTL
        ts_key = self._llm_ctx_ts.get(symbol)
        now = timestamp
        cache_valid = (
            ts_key is not None
            and (now - ts_key).total_seconds() < self._llm_cache_seconds
            and symbol in self._llm_ctx
        )

        if not cache_valid:
            ctx = get_llm_macro_context(
                btc_analysis=btc_analysis,
                target_symbol=symbol,
                target_analysis=analysis,
                market_overview={},          # live market_overview injected by CLI
                provider=self._llm_provider,
            )
            self._llm_ctx[symbol] = ctx
            self._llm_ctx_ts[symbol] = now
        else:
            ctx = self._llm_ctx[symbol]

        return ctx

    def _evaluate_execution_gate(
        self,
        symbol: str,
        direction,          # TradeDirection from engine
        llm_ctx: dict,
        timestamp: str,
    ) -> tuple[bool, Optional[str]]:
        """
        Evaluate the LLM Execution Gate.

        Returns:
            (allowed: bool, block_reason: str | None)
        """
        macro_regime = llm_ctx.get("macro_regime", "NEUTRAL")
        btc_strength = llm_ctx.get("btc_strength", "NEUTRAL")
        volatility   = llm_ctx.get("volatility_warning", "LOW")
        is_alt = not is_major(symbol)
        is_long = direction.value == "LONG"

        # ── Gate Rule 1: Risk-Off + BTC Weak → block ALT LONGs ─────────────
        if is_alt and is_long:
            if macro_regime == "RISK_OFF" and btc_strength == "WEAK":
                reason = (
                    f"BLOCKED: ALT LONG forbidden — macro_regime=RISK_OFF, "
                    f"btc_strength=WEAK"
                )
                _log_macro_context(llm_ctx, symbol, "BLOCKED", timestamp)
                return False, reason
            if macro_regime == "RISK_OFF":
                reason = (
                    f"BLOCKED: ALT LONG forbidden — macro_regime=RISK_OFF"
                )
                _log_macro_context(llm_ctx, symbol, "BLOCKED", timestamp)
                return False, reason

        # ── Gate Rule 2: BTC Weak → only BTC shorts allowed ────────────────
        if btc_strength == "WEAK" and is_long:
            reason = (
                f"BLOCKED: LONG forbidden — btc_strength=WEAK"
            )
            _log_macro_context(llm_ctx, symbol, "BLOCKED", timestamp)
            return False, reason

        _log_macro_context(llm_ctx, symbol, "ALLOWED", timestamp)
        return True, None

    def _adjust_pos_mult_for_volatility(
        self,
        llm_ctx: dict,
        current_pos_mult: float,
    ) -> float:
        """
        Phase 4: If LLM returns HIGH volatility_warning, force the most
        conservative tier (0.3x), overriding standard technical sizing.
        """
        if llm_ctx.get("volatility_warning") == "HIGH":
            return 0.3
        return current_pos_mult

    # ── Three-Phase Decision Framework ─────────────────────────────────────────

    def _three_phase_evaluate(
        self,
        analysis: dict,
        setup: dict,
        btc_analysis: dict,
        llm_ctx: dict,
    ) -> tuple[ThreePhase, Optional[str]]:
        """
        Apply the CLAUDE.md Section 11 three-phase decision framework.

        Returns
        -------
        (phase, reason)
            phase : ThreePhase
                The phase this setup is in.
            reason : str or None
                Human-readable reason for logging; None when phase == PHASE_3.

        Phase Logic
        -----------
        PHASE 1 — No trigger:
            The WorkflowStrategy technical check found no signal at all.
            check_entry() already returns None in this case, so this method
            is only called when a signal exists.  PHASE 1 here means the
            signal is present but other conditions indicate "wait".

        PHASE 2 — Shock:
            A trigger has fired (signal present) but the Execution Gate
            has not confirmed it.  This is the "just moved" state — do NOT
            chase.  Keep watching for confirmation.

        PHASE 3 — Confirmed:
            All four Execution Gate conditions are met:
                structure_break AND macro_aligned AND invalidation_clear AND RR >= 1.5

        Execution Gate (CLAUDE.md Section 11.4)
        ---------------------------------------
            gate = structure_break
                AND macro_aligned
                AND invalidation_clear
                AND RR >= 1.5

        Anti-Avoidance Rule (CLAUDE.md Section 11.6)
        -----------------------------------------------
            If outputting NO TRADE in PHASE 3, must explicitly prove ONE of:
                1. control unclear
                2. invalidation unclear
                3. RR < 1.5
            Otherwise mark as "AVOIDANCE BEHAVIOR".
        """
        signal    = analysis.get("signal", "HOLD")
        direction = setup.get("direction")
        is_long   = direction.value == "LONG" if direction else False

        macro_regime = llm_ctx.get("macro_regime", "NEUTRAL")
        btc_strength = llm_ctx.get("btc_strength", "NEUTRAL")
        volatility   = llm_ctx.get("volatility_warning", "LOW")
        rr           = setup.get("rr_ratio", 0.0)
        stop_dist    = setup.get("stop_distance", 0.0)
        entry        = setup.get("entry", 0.0)
        stop         = setup.get("stop", 0.0)

        # ── Gate component 1: structure_break ─────────────────────────────
        # Structure is broken when:
        #   LONG:  price is below key support / EMA200 bearish
        #   SHORT: price is above key resistance / EMA200 bullish
        # We use the technical signal as a proxy: the strategy already
        # confirmed structure via its own rules.
        structure_break = signal in ("BUY", "SELL")

        # ── Gate component 2: macro_aligned ─────────────────────────────────
        # Macro aligns when the direction is supported by the macro regime.
        if is_long:
            macro_aligned = macro_regime in ("RISK_ON", "NEUTRAL")
        else:
            # Shorts are valid in risk-off (BTC weakness) or neutral
            macro_aligned = macro_regime in ("RISK_OFF", "NEUTRAL")

        # ── Gate component 3: invalidation_clear ────────────────────────────
        # Invalidation is clear when a mathematically-defined stop level exists.
        # Stop must be:
        #   - Present (not None/0)
        #   - At least 0.5% away from entry (avoids noise扫 stop)
        #   - Directionally correct (below entry for longs, above for shorts)
        stop_too_tight = (
            entry > 0
            and stop_dist > 0
            and stop_dist < 0.005   # < 0.5% = noise zone
        )
        invalidation_clear = (
            stop is not None
            and stop > 0
            and entry > 0
            and (
                (is_long  and stop < entry)   # long stop must be below entry
                or (not is_long and stop > entry)  # short stop must be above entry
            )
            and not stop_too_tight
        )

        # ── Gate component 4: RR >= 1.5 (including extension) ─────────────
        # RR is computed from setup["rr_ratio"] which already includes
        # extension targets per WorkflowStrategy.
        rr_pass = rr >= 1.5

        # ── Evaluate gate ───────────────────────────────────────────────────
        gate_passes = structure_break and macro_aligned and invalidation_clear and rr_pass

        if not gate_passes:
            reasons = []
            if not structure_break: reasons.append("no_structure_break")
            if not macro_aligned:    reasons.append(f"macro={macro_regime}_opposes_{direction.value}")
            if not invalidation_clear:
                if stop_too_tight:
                    reasons.append("stop_<0.5%_(noise_zone)")
                else:
                    reasons.append("no_clear_invalidation")
            if not rr_pass:         reasons.append(f"RR={rr:.2f}_<_1.5")
            reason = "PHASE_2_SHOCK: " + "; ".join(reasons)
            return ThreePhase.PHASE_2_SHOCK, reason

        # ── Gate passes → PHASE 3 CONFIRMED ─────────────────────────────────
        return ThreePhase.PHASE_3_CONFIRMED, None

    def _risk_off_guard_no_llm(
        self,
        symbol: str,
        direction,
        btc_analysis: dict,
    ) -> tuple[bool, Optional[str]]:
        """
        Risk-off enforcement for when LLM is disabled (pure-rule mode).

        Per CLAUDE.md Section 10: ALT LONG is forbidden when:
            macro = risk_off AND BTC shows weakness signals.

        We derive the regime from btc_analysis alone (no LLM required):
            risk_off  = BTC close < EMA200
            btc_weak  = BTC close < EMA200 AND BTC RSI < 45

        This mirrors the gate in _evaluate_execution_gate() so that the
        enforcement is consistent regardless of whether LLM is on.

        Returns (allowed, reason).
        """
        if is_major(symbol):
            return True, None   # BTC is always permitted

        is_long = direction.value == "LONG" if direction else False
        if not is_long:
            return True, None   # Shorts are not restricted by this rule

        if btc_analysis is None:
            return True, None   # Cannot determine; defer to other gates

        btc_price  = btc_analysis.get("current_price", 0.0)
        btc_ema200  = btc_analysis.get("ema200", 0.0)
        btc_rsi    = btc_analysis.get("rsi_14", 50.0)

        risk_off = (btc_price < btc_ema200) if btc_ema200 > 0 else False
        btc_weak = risk_off and (btc_rsi < 45)

        if risk_off and btc_weak:
            reason = (
                f"BLOCKED: ALT LONG forbidden — risk_off detected "
                f"(BTC < EMA200), btc_weak (RSI < 45). "
                f"(CLAUDE.md Section 10)"
            )
            return False, reason

        return True, None

    # ── Entry check (wraps WorkflowStrategy.check_entry) ─────────────────────

    def check_entry(
        self,
        symbol: str,
        analysis: dict,
        equity: float,
        btc_analysis: dict = None,
        timestamp: datetime = None,
    ) -> Optional[dict]:
        """
        Technical setup from WorkflowStrategy, then filtered through
        the LLM Execution Gate and the Three-Phase Decision Framework.

        Execution order:
            1. WorkflowStrategy.check_entry() — pure technical signal
            2. [LLM disabled path]  Risk-off guard (pure-rule, no LLM needed)
            3. [LLM enabled path]   LLM macro context + Execution Gate
            4. Three-Phase Evaluation — structure_break / macro_aligned /
                                         invalidation_clear / RR >= 1.5
            5. Volatility Shield — force pos_mult=0.3 on HIGH vol
        """
        # Step 1: Get technical setup from core strategy
        setup = self._core.check_entry(symbol, analysis, equity)
        if not setup:
            return None

        direction = setup["direction"]
        ts_str = timestamp.isoformat() if timestamp else datetime.now().isoformat()

        # ── PHASE 1 gate: no valid technical setup already returned None ────
        # Nothing to do — PHASE_1 means no signal at all.

        llm_ctx: dict = {}

        # Step 2: If LLM disabled — apply pure-rule risk-off guard + skip to Phase eval
        if not self._use_llm:
            # Pure-rule risk-off enforcement (Section 10)
            allowed, block_reason = self._risk_off_guard_no_llm(
                symbol=symbol,
                direction=direction,
                btc_analysis=btc_analysis,
            )
            if not allowed:
                print(f"[RISK-OFF GUARD] {symbol} {direction.value}: {block_reason}")
                return None
            # Skip LLM context fetch; llm_ctx stays empty (neutral defaults)
            llm_ctx = {"macro_regime": "NEUTRAL", "btc_strength": "NEUTRAL",
                       "volatility_warning": "LOW"}
        else:
            # Step 3: Get LLM macro context (cached)
            llm_ctx = self._get_llm_context(
                symbol=symbol,
                analysis=analysis,
                btc_analysis=btc_analysis,
                timestamp=timestamp or datetime.now(),
            )

            # Step 3b: Evaluate LLM Execution Gate
            allowed, block_reason = self._evaluate_execution_gate(
                symbol=symbol,
                direction=direction,
                llm_ctx=llm_ctx,
                timestamp=ts_str,
            )
            if not allowed:
                print(f"[LLM GATE] {symbol} {direction.value}: {block_reason}")
                return None

        # ── PHASE 2/3 — Three-Phase Decision Framework ──────────────────────
        phase, phase_reason = self._three_phase_evaluate(
            analysis=analysis,
            setup=setup,
            btc_analysis=btc_analysis,
            llm_ctx=llm_ctx,
        )

        if phase == ThreePhase.PHASE_1_NO_TRIGGER:
            # Should not be reachable (setup would be None above), but guard anyway
            print(f"[3-PHASE] {symbol}: PHASE_1_NO_TRIGGER — skipping")
            return None

        if phase == ThreePhase.PHASE_2_SHOCK:
            # "刚动 → 不追" (just moved — don't chase)
            print(f"[3-PHASE] {symbol}: {phase_reason}")
            return None

        # PHASE_3_CONFIRMED — gate passes; execute
        # (phase == ThreePhase.PHASE_3_CONFIRMED)

        # Step 4: Volatility Shield — force conservative sizing on HIGH vol
        original_mult = setup.get("pos_mult", self._core.config.pos_mult)
        adjusted_mult = self._adjust_pos_mult_for_volatility(llm_ctx, original_mult)
        if adjusted_mult != original_mult:
            print(
                f"[MACRO RISK] {symbol}: High Volatility Detected. "
                f"Sizing reduced to 0.3x (was {original_mult:.2f}x)."
            )
            setup["pos_mult"] = adjusted_mult
            # Recalculate position size with new multiplier
            from ..execution import position_sizer as ps
            coin_type = "major" if is_major(symbol) else "small"
            stop_distance = setup.get("stop_distance", 0.02)
            position = ps.calculate_position_size(
                equity=equity,
                risk_pct=self._core.config.base_risk_pct,
                stop_distance=stop_distance,
                pos_mult=adjusted_mult,
                coin_type=coin_type,
                training_mode=self._core.config.training_mode,
            )
            if position["valid"]:
                setup["size"] = position["max_position"] / setup["entry"]
                setup["leverage"] = min(
                    position["recommended_leverage"],
                    self._core.config.max_leverage,
                )

        return setup

    # ── Core execute (delegates to WorkflowStrategy, intercepts check_entry) ─

    def execute(
        self,
        engine,
        symbol: str,
        data: Dict[str, List[dict]],
        timestamp: datetime,
        ts_int: int,
    ):
        """
        Main execution loop. Mirrors WorkflowStrategy.execute() but
        calls self.check_entry() (with LLM gate) instead of _core.check_entry().
        """
        # Delegate market context update to core
        self._core.engine = engine
        symbol = normalize_symbol(symbol)

        if symbol == "BTCUSDT":
            self._core.update_market_context(data, ts_int)

        analysis = self._core.analyze_symbol(symbol, data, ts_int)
        if not analysis:
            return

        if symbol not in self._core.states:
            self._core.states[symbol] = self._core.__class__.StrategyState(symbol)

        state = self._core.states[symbol]
        state.analysis = analysis
        state.last_check_time = timestamp

        # ── Manage open positions (no LLM gate needed) ───────────────────────
        from .engine import TradeDirection

        if symbol in engine.open_trades:
            trade = engine.open_trades[symbol]
            engine.check_stop_take(symbol, analysis["current_price"], timestamp)

            if analysis["signal"] == "SELL" and trade.direction == TradeDirection.LONG:
                engine.close_trade(symbol, analysis["current_price"], timestamp, "SIGNAL_SELL")
            elif analysis["signal"] == "BUY" and trade.direction == TradeDirection.SHORT:
                engine.close_trade(symbol, analysis["current_price"], timestamp, "SIGNAL_BUY")

        else:
            # ── Entry check with LLM Execution Gate ────────────────────────
            # Get BTC analysis for LLM macro context
            btc_analysis = None
            if symbol != "BTCUSDT" and "BTCUSDT" in data:
                btc_analysis = self._core.analyze_symbol("BTCUSDT", data["BTCUSDT"], ts_int)

            entry_setup = self.check_entry(
                symbol=symbol,
                analysis=analysis,
                equity=engine.equity,
                btc_analysis=btc_analysis,
                timestamp=timestamp,
            )
            if entry_setup:
                engine.open_trade(
                    symbol=symbol,
                    direction=entry_setup["direction"],
                    entry_price=entry_setup["entry"],
                    size=entry_setup["size"],
                    leverage=entry_setup["leverage"],
                    stop_loss=entry_setup["stop"],
                    take_profit=entry_setup["target"],
                    timestamp=timestamp,
                )
