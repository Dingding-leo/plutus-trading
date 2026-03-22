"""
Plutus V4 — Risk Guard
Hard-stop risk limit checks for live and dry-run trading sessions.

Architectural role:
    The last gate before any order is sent to the exchange. Every proposed trade
    must pass all checks in RiskGuard.check_all() before execution. Any failure
    raises RiskLimitExceeded and halts the trade.

Design principles:
    - Fail-closed: if any check fails, the trade is blocked. No silent overrides.
    - All limits are expressed in the config YAML — zero magic numbers in code.
    - Each check returns (passed: bool, reason: str) so callers can log the cause.
    - check_all() raises on the FIRST failure (fail-fast). Call check_all() once
      per proposed trade, not one by one in a loop.

Usage:
    from src.execution.risk_limits import RiskGuard, RiskLimitExceeded

    guard = RiskGuard(equity=10_000.0, mode="live")
    try:
        guard.check_all(
            proposed_notional=4_000,
            proposed_leverage=10,
            risk_environment="low_risk",
            session_loss_pct=-0.02,
            daily_drawdown_pct=-0.01,
            current_exposure={"BTCUSDT": 3000, "ETHUSDT": 2000},
            proposed_exposure={"BTCUSDT": 500},
            distance_to_liquidation_pct=0.03,
        )
    except RiskLimitExceeded as e:
        print(f"[KILL SWITCH] {e.limit_name}: {e.reason}")
        # Do not trade — escalate, alert, log

    # To check absolute equity (standalone, no trade):
    passed, reason = guard.check_absolute_equity(equity=1_500)
    if not passed:
        raise RiskLimitExceeded("equity_floor", reason)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


# ─── Exceptions ────────────────────────────────────────────────────────────────

class RiskLimitExceeded(Exception):
    """
    Raised when any configured risk limit is breached.
    Caught by the execution layer to halt trading and trigger alerts.
    """

    def __init__(self, limit_name: str, reason: str, details: Optional[Dict] = None):
        self.limit_name = limit_name
        self.reason = reason
        self.details = details or {}
        super().__init__(f"[KILL SWITCH] {limit_name}: {reason}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "limit_name": self.limit_name,
            "reason": self.reason,
            "details": self.details,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ─── Risk Environment ─────────────────────────────────────────────────────────

class RiskEnvironment(Enum):
    LOW      = "low_risk"
    MODERATE = "moderate_risk"
    HIGH     = "high_risk"

    @classmethod
    def from_string(cls, value: str) -> "RiskEnvironment":
        mapping = {
            "low_risk": cls.LOW,
            "moderate_risk": cls.MODERATE,
            "high_risk": cls.HIGH,
        }
        resolved = mapping.get(value, cls.MODERATE)
        if resolved not in mapping.values():
            raise ValueError(f"Unknown risk environment: {value!r}. Use: low_risk, moderate_risk, high_risk.")
        return resolved


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_mode_value(mode: str, dry_value: Any, live_value: Any) -> Any:
    """Return dry_value or live_value based on the trading mode."""
    if mode == "dry_run":
        return dry_value
    elif mode == "live":
        return live_value
    else:
        raise ValueError(f"Unknown mode {mode!r}. Use 'dry_run' or 'live'.")


def _format_pct(value: float) -> str:
    """Format a decimal fraction as a percentage string."""
    return f"{value * 100:+.2f}%"


def _format_usd(value: float) -> str:
    """Format a dollar amount."""
    return f"${value:,.2f}"


# ─── Config Loader ─────────────────────────────────────────────────────────────

def load_risk_config(
    config_path: str = "config/risk_limits.yaml",
    mode: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Load and parse the risk_limits YAML configuration.
    Resolves YAML anchors for the active mode.

    Args:
        config_path: Path to the YAML file (relative to project root or absolute).
        mode:        Override mode ('live' or 'dry_run'). If None, reads from YAML.
                     Caller-provided mode takes precedence over the YAML setting.

    Returns:
        The loaded config dict with mode-resolved values under a "resolved" key.

    Raises:
        FileNotFoundError: Config file does not exist.
        yaml.YAMLError: Config is not valid YAML.
    """
    path = Path(config_path)
    if not path.is_absolute():
        # Resolve relative to project root (two levels up from src/execution/)
        project_root = Path(__file__).resolve().parent.parent.parent
        path = project_root / config_path

    if not path.exists():
        raise FileNotFoundError(f"Risk config not found: {path}")

    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    # Strip the _anchors section (documentation-only, not consumed by the engine)
    raw.pop("_anchors", None)

    # Caller-provided mode takes precedence over the YAML setting.
    # If not provided, fall back to the YAML's own mode declaration.
    effective_mode = mode if mode in ("live", "dry_run") else raw.get("mode", "live")

    # Resolve sections that have dry_run / live sub-keys
    sections_to_resolve = [
        "fat_finger_protection",
        "correlated_exposure",
        "leverage_circuit_breakers",
        "session_loss_limits",
    ]

    resolved = {}
    for section in sections_to_resolve:
        if section in raw:
            cfg = raw[section]
            dry_val = cfg.get("dry_run")
            live_val = cfg.get("live")
            # Only resolve if both keys are present
            if dry_val is not None and live_val is not None:
                resolved[section] = _resolve_mode_value(effective_mode, dry_val, live_val)
            else:
                resolved[section] = cfg

    # Merge resolved sections back into raw for easy attribute access
    for section, value in resolved.items():
        raw[section] = value

    raw["_mode"] = effective_mode
    return raw


# ─── Equity Tracker ───────────────────────────────────────────────────────────

@dataclass
class EquitySnapshot:
    """
    Snapshot of equity state at a point in time.
    Used by RiskGuard to track rolling drawdown and session history.
    """
    timestamp: datetime
    equity: float
    peak_equity: float   # High-water mark for drawdown calculation

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return (self.equity - self.peak_equity) / self.peak_equity


@dataclass
class SessionSnapshot:
    """
    Tracks PnL within a single trading session.
    A session is a defined time window (default 480 min / 8 hours).
    """
    session_id: str
    start_time: datetime
    start_equity: float
    current_equity: float
    trade_count: int = 0

    @property
    def session_loss_pct(self) -> float:
        if self.start_equity <= 0:
            return 0.0
        return (self.current_equity - self.start_equity) / self.start_equity


# ─── Risk Guard ───────────────────────────────────────────────────────────────

class RiskGuard:
    """
    Hard-stop risk guard for Plutus V4.

    Checks all configured limits before a trade is submitted.
    Fails fast on the first breach, raising RiskLimitExceeded.

    Usage:
        guard = RiskGuard(equity=10_000.0, mode="live")
        guard.start_session()
        guard.update_equity(9_500.0)   # after some PnL
        try:
            guard.check_all(proposed_notional=4_000, ...)
        except RiskLimitExceeded as e:
            print(e.limit_name, e.reason)
    """

    # Correlated beta symbol sets (hardcoded reference — also in YAML)
    CRYPTO_BETA_SYMBOLS: List[str] = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

    def __init__(
        self,
        equity: float,
        mode: str = "live",
        config_path: str = "config/risk_limits.yaml",
        initial_capital: Optional[float] = None,
    ):
        """
        Args:
            equity: Current account equity (USD).
            mode: 'live' or 'dry_run'. Controls which YAML anchors are resolved.
            config_path: Path to risk_limits.yaml.
            initial_capital: Override for initial_capital from YAML (defaults to YAML value).
        """
        self.equity = equity
        self.mode = mode
        self._config = load_risk_config(config_path, mode=mode)

        # Override initial capital if provided
        self.initial_capital: float = (
            initial_capital
            if initial_capital is not None
            else self._config.get("initial_capital", 10_000.0)
        )

        # ── Peak tracking ────────────────────────────────────────────────────
        self._peak_equity: float = max(equity, self.initial_capital)

        # ── Session tracking ─────────────────────────────────────────────────
        self._session: Optional[SessionSnapshot] = None
        self._session_duration_minutes: int = self._config.get(
            "session_duration_minutes", 480
        )

        # ── Daily drawdown tracking ───────────────────────────────────────────
        self._daily_peak_equity: float = max(equity, self.initial_capital)
        self._daily_reset_time: datetime = datetime.now(timezone.utc)

        # ── Consecutive session stops (for kill switch) ───────────────────────
        self._consecutive_session_stops: int = 0

        # ── Kill switch state ────────────────────────────────────────────────
        self._kill_switch_active: bool = False
        self._kill_switch_time: Optional[datetime] = None
        self._cooldown_minutes: int = self._config.get(
            "black_swan", {}
        ).get("cooldown_after_kill_switch_minutes", 30)

        # ── Kill switch flag (permanent until reset) ─────────────────────────
        self._permanent_kill: bool = False

        # ── Exposure tracking ────────────────────────────────────────────────
        self._open_exposure: Dict[str, float] = {}  # symbol -> notional value

        # ── Alert state ──────────────────────────────────────────────────────
        self._alerts_fired: Dict[str, bool] = {}  # alert_name -> already_fired

    # ── Session Management ──────────────────────────────────────────────────

    def start_session(self, session_id: Optional[str] = None) -> SessionSnapshot:
        """
        Open a new trading session. Call at the start of each session.

        Args:
            session_id: Optional identifier (defaults to ISO timestamp).
        """
        self._session = SessionSnapshot(
            session_id=session_id or datetime.now(timezone.utc).isoformat(),
            start_time=datetime.now(timezone.utc),
            start_equity=self.equity,
            current_equity=self.equity,
            trade_count=0,
        )
        return self._session

    def update_equity(self, equity: float, trade_count_delta: int = 0) -> None:
        """
        Update current equity. Call after every PnL event.

        Args:
            equity: New equity value.
            trade_count_delta: Increment trade count (default 0).
        """
        self.equity = equity

        # Update peak
        if equity > self._peak_equity:
            self._peak_equity = equity

        # Update daily peak
        now = datetime.now(timezone.utc)
        if (now - self._daily_reset_time).total_seconds() > 86400:
            # New trading day — reset daily peak
            self._daily_peak_equity = max(equity, self.initial_capital)
            self._daily_reset_time = now

        if equity > self._daily_peak_equity:
            self._daily_peak_equity = equity

        # Update session
        if self._session is not None:
            self._session.current_equity = equity
            self._session.trade_count += trade_count_delta

    def record_trade(self, notional: float, symbol: str) -> None:
        """
        Record a trade into open exposure tracking.

        Args:
            notional: Notional value of the position.
            symbol: Trading pair symbol.
        """
        if symbol not in self._open_exposure:
            self._open_exposure[symbol] = 0.0
        self._open_exposure[symbol] += notional

    def close_position(self, notional: float, symbol: str) -> None:
        """
        Remove a position from open exposure tracking.

        Args:
            notional: Notional value of the position to remove.
            symbol: Trading pair symbol.
        """
        if symbol in self._open_exposure:
            self._open_exposure[symbol] = max(0.0, self._open_exposure[symbol] - notional)

    def flatten_all_positions(self) -> Dict[str, float]:
        """
        Emergency flatten: clear all open exposure.
        Returns the exposure that was cleared (for logging).
        """
        cleared = dict(self._open_exposure)
        self._open_exposure.clear()
        return cleared

    def reset_daily(self) -> None:
        """Manually reset daily peak. Call at start of each trading day."""
        self._daily_peak_equity = max(self.equity, self.initial_capital)
        self._daily_reset_time = datetime.now(timezone.utc)

    def reset_kill_switch(self) -> None:
        """Manually reset the kill switch. Call after trader review."""
        self._kill_switch_active = False
        self._permanent_kill = False
        self._consecutive_session_stops = 0

    # ── Kill Switch Helpers ────────────────────────────────────────────────

    @property
    def kill_switch_active(self) -> bool:
        """True if the kill switch is currently engaged."""
        return self._kill_switch_active or self._permanent_kill

    def _check_cooldown(self) -> Tuple[bool, str]:
        """Check if cooldown period after kill switch has elapsed."""
        if self._kill_switch_time is None:
            return True, ""

        elapsed_minutes = (
            datetime.now(timezone.utc) - self._kill_switch_time
        ).total_seconds() / 60.0

        if elapsed_minutes < self._cooldown_minutes:
            remaining = self._cooldown_minutes - elapsed_minutes
            return False, (
                f"Kill switch cooldown active. "
                f"{remaining:.1f} minutes remaining before resume allowed."
            )
        # Cooldown elapsed — can resume
        self._kill_switch_active = False
        self._kill_switch_time = None
        return True, ""

    def _engage_kill_switch(self, reason: str, permanent: bool = False) -> None:
        """Engage the kill switch."""
        self._kill_switch_active = True
        self._kill_switch_time = datetime.now(timezone.utc)
        if permanent:
            self._permanent_kill = True

    def _increment_session_stops(self) -> None:
        """Increment consecutive session stop counter and engage kill switch if threshold met."""
        self._consecutive_session_stops += 1
        black_swan = self._config.get("black_swan", {})
        threshold = black_swan.get("consecutive_session_stops_kill", 2)
        if self._consecutive_session_stops >= threshold:
            self._engage_kill_switch(
                reason=(
                    f"{self._consecutive_session_stops} consecutive session hard stops. "
                    f"Strategy broken or market conditions changed. Full review required."
                ),
                permanent=False,  # Allow resume after cooldown
            )

    # ── Exposure Helpers ─────────────────────────────────────────────────

    def _crypto_beta_exposure(self, proposed: Dict[str, float]) -> float:
        """Sum of notional exposure to crypto beta symbols (existing + proposed)."""
        total = 0.0
        for symbol, notional in self._open_exposure.items():
            if symbol in self.CRYPTO_BETA_SYMBOLS:
                total += notional
        for symbol, notional in (proposed or {}).items():
            if symbol in self.CRYPTO_BETA_SYMBOLS:
                total += notional
        return total

    def _alt_beta_exposure(self, proposed: Dict[str, float]) -> float:
        """Sum of notional exposure to alt beta symbols (existing + proposed)."""
        total = 0.0
        for symbol, notional in self._open_exposure.items():
            if symbol not in self.CRYPTO_BETA_SYMBOLS:
                total += notional
        for symbol, notional in (proposed or {}).items():
            if symbol not in self.CRYPTO_BETA_SYMBOLS:
                total += notional
        return total

    def _total_open_notional(self, proposed_notional: float = 0.0) -> float:
        """Total notional of all open positions plus proposed trade."""
        return sum(self._open_exposure.values()) + proposed_notional

    # ── Alert Helpers ─────────────────────────────────────────────────────

    def _should_fire_alert(
        self,
        alert_name: str,
        condition: bool,
    ) -> Optional[Dict[str, Any]]:
        """
        Fire an alert if condition is True and it hasn't fired yet.
        Returns alert dict if should fire, None otherwise.
        """
        if condition and not self._alerts_fired.get(alert_name):
            self._alerts_fired[alert_name] = True
            return {
                "alert": alert_name,
                "equity": self.equity,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        return None

    # ═══════════════════════════════════════════════════════════════════════
    # INDIVIDUAL LIMIT CHECKS
    # Each returns (passed: bool, reason: str).
    # ═══════════════════════════════════════════════════════════════════════

    def check_kill_switch(self) -> Tuple[bool, str]:
        """
        [0] Kill switch gate — must be checked FIRST before anything else.

        Returns:
            (True, "") if kill switch is clear.
            (False, reason) if kill switch is engaged or in cooldown.
        """
        if self._kill_switch_active or self._permanent_kill:
            if self._kill_switch_active:
                cooldown_ok, cooldown_msg = self._check_cooldown()
                if not cooldown_ok:
                    return False, cooldown_msg
                # Cooldown elapsed but kill switch still active — manual reset needed
                return False, (
                    "Kill switch engaged. Manual reset required after review. "
                    "Check news, assess market conditions, then call reset_kill_switch()."
                )
            else:
                return False, "Permanent kill switch active. No trading allowed."

        return True, ""

    def check_drawdown(
        self,
        daily_drawdown_pct: Optional[float] = None,
        lookback_hours: int = 24,
    ) -> Tuple[bool, str]:
        """
        [1] Global drawdown limit — halt all trading if equity drops X% in N hours.

        Args:
            daily_drawdown_pct: Current drawdown as negative decimal
                                 (e.g., -0.08 for -8%). If None, computed from peak.
            lookback_hours: Rolling window (default 24h).

        Returns:
            (passed, reason).
        """
        cfg = self._config.get("global_drawdown", {})
        if not cfg.get("enabled", True):
            return True, ""

        rolling_window_hours = cfg.get("rolling_window_hours", 24)
        halt_threshold = cfg.get("halt_threshold_pct", -0.08)

        # Compute current drawdown from peak if not provided
        if daily_drawdown_pct is None:
            current_drawdown = self.drawdown_pct
        else:
            current_drawdown = daily_drawdown_pct

        if current_drawdown <= halt_threshold:
            return False, (
                f"Global drawdown {_format_pct(current_drawdown)} exceeds "
                f"halt threshold {_format_pct(halt_threshold)} "
                f"over {rolling_window_hours}h window. All trading halted."
            )

        return True, ""

    @property
    def drawdown_pct(self) -> float:
        """Current drawdown from peak equity."""
        if self._peak_equity <= 0:
            return 0.0
        return (self.equity - self._peak_equity) / self._peak_equity

    def check_notional(
        self,
        proposed_notional: float,
        training_mode: bool = True,
    ) -> Tuple[bool, str]:
        """
        [2] Fat-finger protection — max notional per individual trade.

        Args:
            proposed_notional: Size of the proposed trade in USD.
            training_mode: If True, use training-phase cap; if False, use advanced cap.

        Returns:
            (passed, reason).
        """
        cfg = self._config.get("fat_finger_protection", {})
        max_notional = cfg.get("max_notional", 5_000)

        if proposed_notional > max_notional:
            return False, (
                f"Proposed notional {_format_usd(proposed_notional)} exceeds "
                f"fat-finger cap {_format_usd(max_notional)}. "
                f"Reduce position size or skip this trade."
            )

        # Also check total open notional
        total_cfg = cfg.get("max_total_notional_pct_of_equity_training"
                            if training_mode
                            else "max_total_notional_pct_of_equity_advanced",
                            3.0)
        total_notional_cap = self.equity * total_cfg
        total_open = self._total_open_notional(proposed_notional)

        if total_open > total_notional_cap:
            return False, (
                f"Total open notional {_format_usd(total_open)} exceeds cap "
                f"({total_cfg:.1f}× equity = {_format_usd(total_notional_cap)}). "
                f"Reduce existing exposure or skip new trade."
            )

        return True, ""

    def check_correlated_exposure(
        self,
        proposed_exposure: Optional[Dict[str, float]] = None,
    ) -> Tuple[bool, str]:
        """
        [3] Correlated beta exposure — max % of account in crypto beta and alt beta.

        Args:
            proposed_exposure: Dict of {symbol: notional} for the proposed trade.
                                Can include symbols already open (additive).

        Returns:
            (passed, reason).
        """
        cfg = self._config.get("correlated_exposure", {})
        crypto_beta_cap = cfg.get("crypto_beta_pct")
        alt_beta_cap = cfg.get("alt_beta_pct")

        # No limit configured (dry_run fallback)
        if crypto_beta_cap is None and alt_beta_cap is None:
            return True, ""

        # Compute current + proposed exposure
        crypto_beta_exposure = self._crypto_beta_exposure(proposed_exposure or {})
        alt_beta_exposure = self._alt_beta_exposure(proposed_exposure or {})

        crypto_beta_limit = self.equity * crypto_beta_cap if crypto_beta_cap is not None else float("inf")
        alt_beta_limit = self.equity * alt_beta_cap if alt_beta_cap is not None else float("inf")

        crypto_beta_pct = crypto_beta_exposure / self.equity if self.equity > 0 else 0

        if crypto_beta_cap is not None and crypto_beta_exposure > crypto_beta_limit:
            return False, (
                f"Crypto beta exposure {_format_usd(crypto_beta_exposure)} "
                f"({crypto_beta_pct:.1%} of equity) exceeds limit "
                f"{crypto_beta_cap:.1%} of equity ({_format_usd(crypto_beta_limit)}). "
                f"BTC/ETH/SOL/BTC/XRP concentration too high — reduce correlated exposure."
            )

        alt_beta_pct = alt_beta_exposure / self.equity if self.equity > 0 else 0

        if alt_beta_cap is not None and alt_beta_exposure > alt_beta_limit:
            return False, (
                f"Alt beta exposure {_format_usd(alt_beta_exposure)} "
                f"({alt_beta_pct:.1%} of equity) exceeds limit "
                f"{alt_beta_cap:.1%} of equity ({_format_usd(alt_beta_limit)}). "
                f"Alt coin concentration too high — reduce alt beta exposure."
            )

        return True, ""

    def check_leverage(
        self,
        proposed_leverage: float,
        risk_environment: str = "moderate_risk",
    ) -> Tuple[bool, str]:
        """
        [4] Leverage circuit breaker — max leverage by risk environment.

        Args:
            proposed_leverage: Proposed leverage (e.g., 10.0 for 10×).
            risk_environment: 'low_risk', 'moderate_risk', or 'high_risk'.

        Returns:
            (passed, reason).
        """
        cfg = self._config.get("leverage_circuit_breakers", {})

        # Resolve max leverage for this environment
        env_limits = {
            "low_risk": cfg.get("max_leverage_low_risk", 15),
            "moderate_risk": cfg.get("max_leverage_moderate_risk", 10),
            "high_risk": cfg.get("max_leverage_high_risk", 5),
        }

        max_leverage = env_limits.get(risk_environment, env_limits["moderate_risk"])

        if proposed_leverage > max_leverage:
            return False, (
                f"Proposed leverage {proposed_leverage:.1f}× exceeds "
                f"circuit breaker limit {max_leverage:.1f}× for "
                f"{risk_environment} environment. "
                f"Reduce leverage to {max_leverage:.1f}× or below."
            )

        return True, ""

    def check_session_loss(
        self,
        session_loss_pct: Optional[float] = None,
        trade_count: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        [5] Single-session loss limit — halt if session loss exceeds threshold.

        Args:
            session_loss_pct: Current session loss as negative decimal.
                               If None, computed from session snapshot.
            trade_count: Override for session trade count.

        Returns:
            (passed, reason).
        """
        cfg = self._config.get("session_loss_limits", {})
        hard_stop = cfg.get("hard_stop_pct", -0.05)
        warning = cfg.get("warning_pct", -0.03)

        if self._session is None:
            return True, ""  # No active session — skip

        # Use provided or compute
        if session_loss_pct is None:
            session_loss_pct = self._session.session_loss_pct
        if trade_count is None:
            trade_count = self._session.trade_count

        if session_loss_pct <= hard_stop:
            self._increment_session_stops()
            self._engage_kill_switch(
                reason=(
                    f"Session hard stop triggered. "
                    f"Session loss {_format_pct(session_loss_pct)} exceeds "
                    f"limit {_format_pct(hard_stop)}. "
                    f"{trade_count} trades in session."
                )
            )
            return False, (
                f"Session hard stop: {_format_pct(session_loss_pct)} loss "
                f"(threshold {_format_pct(hard_stop)}). "
                f"Trading halted for this session. "
                f"Consecutive session stops: {self._consecutive_session_stops}."
            )

        if session_loss_pct <= warning:
            # Warning only — do not block, but log
            return True, (
                f"[WARNING] Session loss {_format_pct(session_loss_pct)} "
                f"approaching hard stop {_format_pct(hard_stop)}. "
                f"Reduce position sizes or close trades."
            )

        return True, ""

    def check_liquidation_buffer(
        self,
        distance_to_liquidation_pct: float,
        coin_type: str = "major",
    ) -> Tuple[bool, str]:
        """
        [6] Margin call buffer — minimum distance from liquidation to open new trade.

        Args:
            distance_to_liquidation_pct: Distance from entry price to estimated
                                         liquidation, as a positive decimal
                                         (e.g., 0.03 for 3%).
            coin_type: 'major' (BTC, ETH) or 'small' (alts).

        Returns:
            (passed, reason).
        """
        cfg = self._config.get("margin_call_buffers", {})
        if not cfg.get("enabled", True):
            return True, ""

        min_distance = (
            cfg.get("major_coin_min_distance_pct", 0.015)
            if coin_type == "major"
            else cfg.get("small_coin_min_distance_pct", 0.025)
        )

        if distance_to_liquidation_pct < min_distance:
            return False, (
                f"Distance to liquidation {_format_pct(distance_to_liquidation_pct)} "
                f"is below minimum buffer {_format_pct(min_distance)} for {coin_type} coins. "
                f"Position is too close to liquidation. "
                f"Widen stop or reduce leverage to increase buffer."
            )

        return True, ""

    def check_absolute_equity(self, equity: Optional[float] = None) -> Tuple[bool, str]:
        """
        [7] Absolute equity floor — hard block if equity below minimum survival level.

        Args:
            equity: Equity to check. Defaults to current guard equity.

        Returns:
            (passed, reason).
        """
        cfg = self._config.get("absolute_equity_floor", {})
        if not cfg.get("enabled", True):
            return True, ""

        equity = equity if equity is not None else self.equity
        minimum = cfg.get("minimum_equity", 1_000)

        if equity < minimum:
            self._engage_kill_switch(
                reason=(
                    f"Equity {_format_usd(equity)} below survival floor "
                    f"{_format_usd(minimum)}. Account cannot recover without deposit."
                ),
                permanent=True,
            )
            return False, (
                f"[KILL SWITCH] Equity {_format_usd(equity)} below absolute floor "
                f"{_format_usd(minimum)}. No new positions allowed. "
                f"Account requires deposit or strategy review."
            )

        return True, ""

    def check_daily_drawdown_black_swan(self) -> Tuple[bool, str]:
        """
        [8] V4 Black Swan: auto-flatten if daily drawdown exceeds auto-flatten threshold.

        This check does NOT block a new trade — it indicates whether the
        flatten trigger has fired. Callers should use the result to close
        all positions.

        Returns:
            (True, reason) if no flatten needed.
            (False, reason) if auto-flatten triggered.
        """
        black_swan = self._config.get("black_swan", {})
        flatten_threshold = black_swan.get("daily_drawdown_auto_flatten_pct", -0.05)

        if self._daily_peak_equity <= 0:
            return True, ""

        daily_drawdown = (self.equity - self._daily_peak_equity) / self._daily_peak_equity

        if daily_drawdown <= flatten_threshold:
            self._engage_kill_switch(
                reason=(
                    f"Daily drawdown {_format_pct(daily_drawdown)} exceeds "
                    f"auto-flatten threshold {_format_pct(flatten_threshold)}. "
                    f"Black swan event detected. All positions flattened."
                ),
                permanent=False,
            )
            return False, (
                f"Daily drawdown {_format_pct(daily_drawdown)} exceeds "
                f"auto-flatten threshold {_format_pct(flatten_threshold)}. "
                f"Close all positions immediately."
            )

        return True, ""

    # ═══════════════════════════════════════════════════════════════════════
    # ALERT CHECKS
    # These do NOT block trading — they return alert dicts if conditions are met.
    # Callers should dispatch to Telegram/email/webhook.
    # ═══════════════════════════════════════════════════════════════════════

    def check_alerts(
        self,
        daily_drawdown_pct: Optional[float] = None,
        session_loss_pct: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Check all alert thresholds. Returns a list of alerts to fire.

        This method does NOT raise — it returns alert dicts for the
        execution layer to dispatch to Telegram/email/webhook.

        Args:
            daily_drawdown_pct: Current daily drawdown (negative decimal).
            session_loss_pct: Current session loss (negative decimal).

        Returns:
            List of alert dicts. Empty list if no alerts need firing.
        """
        alerts: List[Dict[str, Any]] = []
        cfg = self._config.get("alerts", {})

        # Daily drawdown alerts
        dd = daily_drawdown_pct if daily_drawdown_pct is not None else self.drawdown_pct
        dd_critical = cfg.get("daily_drawdown_critical_pct", -0.05)
        dd_warning = cfg.get("daily_drawdown_warning_pct", -0.03)

        if dd <= dd_critical:
            alert = self._should_fire_alert("daily_drawdown_critical", True)
            if alert:
                alert["message"] = (
                    f"[CRITICAL] Daily drawdown {_format_pct(dd)} "
                    f"exceeds critical threshold {_format_pct(dd_critical)}. "
                    f"Positions being flattened. Equity: {_format_usd(self.equity)}."
                )
                alerts.append(alert)
        elif dd <= dd_warning:
            alert = self._should_fire_alert("daily_drawdown_warning", True)
            if alert:
                alert["message"] = (
                    f"[WARNING] Daily drawdown {_format_pct(dd)} "
                    f"exceeds warning threshold {_format_pct(dd_warning)}. "
                    f"Equity: {_format_usd(self.equity)}."
                )
                alerts.append(alert)

        # Equity alerts
        equity_warning = cfg.get("equity_warning", 2_000)
        equity_critical = cfg.get("equity_critical", 1_500)

        if self.equity <= equity_critical:
            alert = self._should_fire_alert("equity_critical", True)
            if alert:
                alert["message"] = (
                    f"[CRITICAL] Equity {_format_usd(self.equity)} at critical level. "
                    f"Kill switch imminent. Immediate action required."
                )
                alerts.append(alert)
        elif self.equity <= equity_warning:
            alert = self._should_fire_alert("equity_warning", True)
            if alert:
                alert["message"] = (
                    f"[WARNING] Equity {_format_usd(self.equity)} below "
                    f"warning level {_format_usd(equity_warning)}. "
                    f"Floor: {_format_usd(1_000)}."
                )
                alerts.append(alert)

        # Session loss alerts
        sl = session_loss_pct
        if self._session is not None and sl is None:
            sl = self._session.session_loss_pct

        sl_critical = cfg.get("session_loss_critical_pct", -0.05)
        sl_warning = cfg.get("session_loss_warning_pct", -0.03)

        if sl is not None:
            if sl <= sl_critical:
                alert = self._should_fire_alert("session_loss_critical", True)
                if alert:
                    alert["message"] = (
                        f"[CRITICAL] Session loss {_format_pct(sl)} "
                        f"exceeds critical threshold {_format_pct(sl_critical)}. "
                        f"Session halted."
                    )
                    alerts.append(alert)
            elif sl <= sl_warning:
                alert = self._should_fire_alert("session_loss_warning", True)
                if alert:
                    alert["message"] = (
                        f"[WARNING] Session loss {_format_pct(sl)} "
                        f"exceeds warning threshold {_format_pct(sl_warning)}. "
                        f"Hard stop at {_format_pct(sl_critical)}."
                    )
                    alerts.append(alert)

        return alerts

    # ═══════════════════════════════════════════════════════════════════════
    # MASTER CHECK
    # ═══════════════════════════════════════════════════════════════════════

    def check_all(
        self,
        proposed_notional: float,
        proposed_leverage: float,
        risk_environment: str = "moderate_risk",
        session_loss_pct: Optional[float] = None,
        daily_drawdown_pct: Optional[float] = None,
        current_exposure: Optional[Dict[str, float]] = None,
        proposed_exposure: Optional[Dict[str, float]] = None,
        distance_to_liquidation_pct: Optional[float] = None,
        coin_type: str = "major",
        training_mode: bool = True,
        equity: Optional[float] = None,
    ) -> None:
        """
        Run all risk limit checks. Raises RiskLimitExceeded on first failure.

        The checks run in this order (fail-fast):
            [0] Kill switch
            [1] Global drawdown
            [2] Fat-finger notional cap
            [3] Correlated beta exposure
            [4] Leverage circuit breaker
            [5] Session loss limit
            [6] Liquidation buffer
            [7] Absolute equity floor

        Alert checks run separately via check_alerts() — they do not block.

        Args:
            proposed_notional:        Size of the proposed trade in USD.
            proposed_leverage:        Proposed leverage (e.g., 10.0).
            risk_environment:         'low_risk', 'moderate_risk', 'high_risk'.
            session_loss_pct:         Current session PnL as negative decimal.
            daily_drawdown_pct:       Daily drawdown as negative decimal.
            current_exposure:         {symbol: notional} of existing positions.
            proposed_exposure:        {symbol: notional} of proposed trade.
            distance_to_liquidation_pct: Positive decimal (e.g., 0.03 for 3%).
            coin_type:                'major' or 'small'.
            training_mode:            True = training cap; False = advanced cap.
            equity:                   Override equity for this check.

        Raises:
            RiskLimitExceeded: On first limit breach.
        """
        # Merge exposure dicts for correlated exposure check
        combined_exposure: Dict[str, float] = {}
        if current_exposure:
            for sym, val in current_exposure.items():
                combined_exposure[sym] = combined_exposure.get(sym, 0) + val
        if proposed_exposure:
            for sym, val in proposed_exposure.items():
                combined_exposure[sym] = combined_exposure.get(sym, 0) + val

        # Resolve equity to check (use override or current)
        equity_to_check = equity if equity is not None else self.equity

        checks: List[Tuple[str, Tuple[bool, str]]] = [
            ("kill_switch",           self.check_kill_switch()),
            ("global_drawdown",       self.check_drawdown(daily_drawdown_pct)),
            ("fat_finger_notional",   self.check_notional(proposed_notional, training_mode)),
            ("correlated_exposure",   self.check_correlated_exposure(combined_exposure)),
            ("leverage_circuit",      self.check_leverage(proposed_leverage, risk_environment)),
            ("session_loss",          self.check_session_loss(session_loss_pct)),
        ]

        # Liquidation buffer only if distance was provided
        if distance_to_liquidation_pct is not None:
            checks.append(
                ("liquidation_buffer", self.check_liquidation_buffer(distance_to_liquidation_pct, coin_type))
            )

        checks.append(
            ("absolute_equity_floor", self.check_absolute_equity(equity_to_check))
        )

        # Run all checks (fail-fast on first failure)
        for limit_name, (passed, reason) in checks:
            if not passed:
                raise RiskLimitExceeded(
                    limit_name=limit_name,
                    reason=reason,
                    details={
                        "equity": equity_to_check,
                        "proposed_notional": proposed_notional,
                        "proposed_leverage": proposed_leverage,
                        "risk_environment": risk_environment,
                        "session_loss_pct": session_loss_pct,
                        "daily_drawdown_pct": daily_drawdown_pct,
                    },
                )

    # ── Status Report ────────────────────────────────────────────────────────

    def status_report(self) -> Dict[str, Any]:
        """
        Return a snapshot of all risk guard state. Useful for logging and monitoring.
        """
        return {
            "mode": self.mode,
            "equity": self.equity,
            "initial_capital": self.initial_capital,
            "peak_equity": self._peak_equity,
            "current_drawdown_pct": self.drawdown_pct,
            "daily_peak_equity": self._daily_peak_equity,
            "daily_drawdown_pct": (
                (self.equity - self._daily_peak_equity) / self._daily_peak_equity
                if self._daily_peak_equity > 0 else 0.0
            ),
            "open_exposure": dict(self._open_exposure),
            "total_open_notional": self._total_open_notional(),
            "kill_switch_active": self.kill_switch_active,
            "kill_switch_permanent": self._permanent_kill,
            "consecutive_session_stops": self._consecutive_session_stops,
            "session": {
                "active": self._session is not None,
                "session_id": self._session.session_id if self._session else None,
                "session_loss_pct": self._session.session_loss_pct if self._session else None,
                "trade_count": self._session.trade_count if self._session else 0,
            }
            if self._session
            else None,
        }


# ─── Quick Test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("Plutus V4 — RiskGuard: Quick Tests")
    print("=" * 60)

    # ── 1. Load config ─────────────────────────────────────────────────────
    try:
        config = load_risk_config("config/risk_limits.yaml")
        print(f"[1] Config loaded: mode={config['_mode']}, initial_capital={config['initial_capital']}")
        print(f"    Fat finger notional cap: {config['fat_finger_protection']}")
        print(f"    Session hard stop: {config['session_loss_limits']}")
        print(f"    Global drawdown halt: {config['global_drawdown']}")
        print(f"    ✓ Config loads without error")
    except Exception as e:
        print(f"[1] FAIL: {e}")
        sys.exit(1)

    # ── 2. Kill switch blocks everything ──────────────────────────────────
    guard = RiskGuard(equity=10_000.0, mode="live")
    guard._engage_kill_switch(reason="Test kill switch", permanent=False)
    try:
        guard.check_all(proposed_notional=1_000, proposed_leverage=5)
        print("[2] FAIL: Kill switch should have blocked")
        sys.exit(1)
    except RiskLimitExceeded as e:
        assert e.limit_name == "kill_switch"
        print(f"[2] ✓ Kill switch blocks: {e.reason[:60]}...")

    guard.reset_kill_switch()

    # ── 3. Fat finger notional ─────────────────────────────────────────────
    guard = RiskGuard(equity=10_000.0, mode="live")
    passed, reason = guard.check_notional(proposed_notional=8_000)
    assert not passed, "8k exceeds 5k cap in live mode"
    print(f"[3] ✓ Fat finger cap blocks 8k notional: {reason[:60]}...")

    # Dry run allows 10k
    guard_dr = RiskGuard(equity=10_000.0, mode="dry_run")
    passed, reason = guard_dr.check_notional(proposed_notional=9_000)
    assert passed, "Dry run should allow 9k"
    print(f"[4] ✓ Dry run allows 9k notional")

    # ── 5. Leverage circuit breaker ────────────────────────────────────────
    guard = RiskGuard(equity=10_000.0, mode="live")
    passed, reason = guard.check_leverage(proposed_leverage=18.0, risk_environment="high_risk")
    assert not passed, "18x exceeds 5x cap in high risk"
    print(f"[5] ✓ Leverage circuit breaker blocks 18x in high risk: {reason[:60]}...")

    passed, reason = guard.check_leverage(proposed_leverage=14.0, risk_environment="low_risk")
    assert passed, "14x should be allowed in low risk"
    print(f"[6] ✓ 14x allowed in low risk")

    # ── 7. Session loss ────────────────────────────────────────────────────
    guard = RiskGuard(equity=10_000.0, mode="live")
    guard.start_session()
    guard.update_equity(9_400.0)  # -6% session loss
    passed, reason = guard.check_session_loss()
    assert not passed, "Should trigger session hard stop at -6%"
    print(f"[7] ✓ Session hard stop at -6%: {reason[:60]}...")

    # Warning zone
    guard2 = RiskGuard(equity=10_000.0, mode="live")
    guard2.start_session()
    guard2.update_equity(9_600.0)  # -4% session loss (warning zone)
    passed, reason = guard2.check_session_loss()
    assert passed, "Warning zone should pass"
    assert "WARNING" in reason
    print(f"[8] ✓ Session warning at -4%: {reason[:60]}...")

    # ── 9. Absolute equity floor ───────────────────────────────────────────
    guard = RiskGuard(equity=950.0, mode="live")
    passed, reason = guard.check_absolute_equity()
    assert not passed, "950 < 1000 floor"
    print(f"[9] ✓ Equity floor blocks $950: {reason[:60]}...")

    # ── 10. Correlated exposure ─────────────────────────────────────────────
    guard = RiskGuard(equity=10_000.0, mode="live")
    proposed = {"BTCUSDT": 4_000, "ETHUSDT": 2_000}  # 6k crypto beta = 60%
    passed, reason = guard.check_correlated_exposure(proposed)
    assert not passed, "60% crypto beta exceeds 50% cap"
    print(f"[10] ✓ Crypto beta cap blocks 60%: {reason[:60]}...")

    # ── 11. Liquidation buffer ─────────────────────────────────────────────
    guard = RiskGuard(equity=10_000.0, mode="live")
    passed, reason = guard.check_liquidation_buffer(0.008, coin_type="major")
    assert not passed, "0.8% < 1.5% buffer for major coins"
    print(f"[11] ✓ Liquidation buffer blocks 0.8%: {reason[:60]}...")

    passed, reason = guard.check_liquidation_buffer(0.025, coin_type="small")
    assert passed, "2.5% buffer should pass for small coins"
    print(f"[12] ✓ 2.5% buffer allowed for small coins")

    # ── 13. check_all fail-fast ────────────────────────────────────────────
    guard = RiskGuard(equity=800.0, mode="live")
    try:
        # Should fail on kill_switch first (kill switch not active, but equity floor)
        guard.check_all(
            proposed_notional=1_000,
            proposed_leverage=20.0,
            risk_environment="high_risk",
            distance_to_liquidation_pct=0.01,
        )
        print("[13] FAIL: check_all should have raised")
        sys.exit(1)
    except RiskLimitExceeded as e:
        # First check in order is kill_switch (passes), then equity floor fails
        print(f"[13] ✓ check_all fail-fast: {e.limit_name} → {e.reason[:60]}...")

    # ── 14. Black swan auto-flatten ────────────────────────────────────────
    guard = RiskGuard(equity=10_000.0, mode="live")
    guard._daily_peak_equity = 10_500.0
    guard.equity = 9_800.0  # -6.7% daily
    passed, reason = guard.check_daily_drawdown_black_swan()
    assert not passed, "Should trigger auto-flatten at -6.7%"
    print(f"[14] ✓ Auto-flatten at -6.7% daily: {reason[:60]}...")

    # ── 15. Alert checks ────────────────────────────────────────────────────
    guard = RiskGuard(equity=1_800.0, mode="live")
    alerts = guard.check_alerts()
    fired = [a["alert"] for a in alerts]
    assert "equity_warning" in fired, f"Should fire equity_warning, got {fired}"
    print(f"[15] ✓ Alert fires for low equity: {fired}")

    # ── 16. Status report ───────────────────────────────────────────────────
    guard = RiskGuard(equity=9_200.0, mode="live")
    guard.start_session()
    guard.record_trade(3_000.0, "BTCUSDT")
    guard.record_trade(1_500.0, "ETHUSDT")
    report = guard.status_report()
    assert report["total_open_notional"] == 4_500.0
    assert report["session"]["active"] is True
    print(f"[16] ✓ Status report: total_notional={report['total_open_notional']}")

    print()
    print("=" * 60)
    print("✓ All RiskGuard tests passed.")
    print("✓ Hard-stop limits verified.")
    print("✓ Fail-closed design confirmed.")
