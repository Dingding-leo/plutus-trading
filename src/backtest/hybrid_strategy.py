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
"""

from datetime import datetime
from typing import Dict, List, Optional

from .strategy import WorkflowStrategy, StrategyConfig
from ..data.llm_client import get_llm_macro_context
from ..data.coin_tiers import is_major, normalize_symbol


# ─── Logging ────────────────────────────────────────────────────────────────

LLM_LOG_FILE = "logs/llm_macro_context.json"


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
        the LLM Execution Gate.
        """
        # Step 1: Get technical setup from core strategy
        setup = self._core.check_entry(symbol, analysis, equity)
        if not setup:
            return None

        direction = setup["direction"]
        ts_str = timestamp.isoformat() if timestamp else datetime.now().isoformat()

        # Step 2: If LLM disabled, return technical setup directly
        if not self._use_llm:
            return setup

        # Step 3: Get LLM macro context (cached)
        llm_ctx = self._get_llm_context(
            symbol=symbol,
            analysis=analysis,
            btc_analysis=btc_analysis,
            timestamp=timestamp or datetime.now(),
        )

        # Step 3b: Evaluate Execution Gate
        allowed, block_reason = self._evaluate_execution_gate(
            symbol=symbol,
            direction=direction,
            llm_ctx=llm_ctx,
            timestamp=ts_str,
        )
        if not allowed:
            # Override to None = no trade fires
            print(f"[LLM GATE] {symbol} {direction.value}: {block_reason}")
            return None

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
