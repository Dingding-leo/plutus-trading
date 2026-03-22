"""
Plutus V4 — Unified Parameter Schema and ScannerParams.

This module is the SINGLE SOURCE OF TRUTH for all scanner-evolvable parameters.
All GA evolution, validation, and scanner configuration MUST use these types.

================================================================================
 ARCHITECTURE
================================================================================

  GeneticOptimizer (meta_learning.py)
         │  evolves ScannerParams
         ▼
  ScannerParams (this file)          ← ONE class, one place
         │
         ├──► VanguardScanner.update_config(ScannerParams)
         │                    validates against ParamSchema
         │
         └──► chronos_engine.py  (imports from here, not meta_learning.py)

================================================================================
 PARAMETER SCHEMA
================================================================================

All 12 scanner parameters are defined here with type, default, min, and max.
The GA evolves ALL of them (with appropriate constraints).

Cross-reference:
  GA field name  →  ScannerParams field name  (MUST match exactly)
  scanner.py had →  ScannerParams field name  (unified)

Legacy names unified:
  meta_learning.py GA used:  sweep_threshold,        vol_squeeze_atr_mult,
                             deviation_z_score,     min_confidence_threshold
  scanner.py had:            sweep_threshold_pct,   atr_period, etc.
  Unified name:              sweep_threshold_pct    atr_period, etc.
                              (scanner.py names preserved — they are the
                               canonical implementation names)

================================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, get_type_hints


# ─── ParamSchema ────────────────────────────────────────────────────────────────

@dataclass
class ParamSchema:
    """
    Type-safe parameter definition with range enforcement.

    Use ``validate()`` to check a value before applying it.  The scanner's
    ``update_config()`` calls ``validate()`` on every field, so impossible
    values (e.g. sweep_threshold_pct=0.0001) are rejected before they reach
    the detection logic.
    """
    name:       str
    param_type: type
    min_val:    Optional[float] = None
    max_val:    Optional[float] = None
    default:    Any             = None

    def validate(self, value: Any) -> tuple[bool, str]:
        """
        Check that ``value`` is of the correct type and within [min_val, max_val].

        Returns
        -------
        tuple[bool, str]
            (True, "")          — valid
            (False, error_msg)  — invalid, with human-readable reason
        """
        if not isinstance(value, self.param_type):
            return False, (
                f"{self.name} must be {self.param_type.__name__}, "
                f"got {type(value).__name__} ({value!r})"
            )
        if self.param_type in (int, float):
            num = float(value)
            if self.min_val is not None and num < self.min_val:
                return False, (
                    f"{self.name}={num} is below minimum {self.min_val} "
                    f"({'noise floor' if 'threshold' in self.name else 'hard limit'})"
                )
            if self.max_val is not None and num > self.max_val:
                return False, f"{self.name}={num} exceeds maximum {self.max_val}"
        return True, ""


# ─── Hard constraints for GA evolution (G2 fix) ───────────────────────────────
#
# These are NOT the same as the schema defaults.
# These are the FLOOR values that prevent the GA from converging on noise.
#
# Key insight: sweep_threshold_pct=0.0001 (0.01%) means even a $7 wick on
# BTC at $67k passes the sweep filter — this is a micro-wick generator.
# A sweep threshold below 0.5% has no practical trading meaning.

# Minimum sweep threshold: 0.5% — below this is noise in any market
SWEEP_THRESHOLD_MIN: float = 0.005   # 0.5 % — hard floor (G2)
SWEEP_THRESHOLD_MAX: float = 0.05   # 5.0 % — extreme high-vol only

# Deviation ATR multiplier: how many ATRs away from EMA triggers deviation alert
DEVIATION_ATR_MIN: float = 1.5   # Tight — only extreme extensions trigger
DEVIATION_ATR_MAX: float = 5.0   # Very loose — large moves only

# Volatility squeeze: BB width vs rolling min
VOL_SQUEEZE_ATR_MIN: float = 0.1   # Almost any candle qualifies (too noisy)
VOL_SQUEEZE_ATR_MAX: float = 2.0   # Strict — only deep compressions

# RSI levels (oversold / overbought)
RSI_OVERSOLD_MIN:    float = 10.0   # Very oversold
RSI_OVERSOLD_MAX:    float = 40.0   # Upper bound for oversold territory
RSI_OVERBOUGHT_MIN:  float = 60.0   # Lower bound for overbought territory
RSI_OVERBOUGHT_MAX:  float = 90.0   # Very overbought

# BB standard deviation multiplier
BB_STD_MIN: float = 1.0   # Narrow bands — sensitive
BB_STD_MAX: float = 3.0   # Wide bands — only large moves qualify

# Squeeze threshold (% above rolling min BB width to still qualify as squeeze)
SQ_THRESHOLD_MIN: float = 0.5   # 0.5% — very tight
SQ_THRESHOLD_MAX: float = 10.0  # 10% — allow wide compressions

# Periods (integers — more constrained)
ATR_PERIOD_MIN:  int = 7
ATR_PERIOD_MAX:  int = 30
EMA_PERIOD_MIN:  int = 20
EMA_PERIOD_MAX:  int = 200
SWEEP_LBK_MIN:  int = 5
SWEEP_LBK_MAX:  int = 100
SQ_LBK_MIN:     int = 10
SQ_LBK_MAX:     int = 200


# ─── ScannerParams schema registry ────────────────────────────────────────────

# All 12 GA-evolvable scanner parameters with their schemas.
# This list is the authoritative registry — used by update_config() validation.
SCANNER_PARAM_SCHEMAS: list[ParamSchema] = [
    # ── Trigger 1: Liquidity Sweep ──────────────────────────────────────────
    ParamSchema(
        name="sweep_lookback", param_type=int,
        min_val=SWEEP_LBK_MIN, max_val=SWEEP_LBK_MAX, default=20,
    ),
    ParamSchema(
        name="sweep_threshold_pct", param_type=float,
        min_val=SWEEP_THRESHOLD_MIN, max_val=SWEEP_THRESHOLD_MAX,
        default=0.0015,
    ),

    # ── Trigger 2: Extreme Mean Reversion ──────────────────────────────────
    ParamSchema(
        name="deviation_atr_multiplier", param_type=float,
        min_val=DEVIATION_ATR_MIN, max_val=DEVIATION_ATR_MAX, default=2.5,
    ),
    ParamSchema(
        name="rsi_oversold", param_type=float,
        min_val=RSI_OVERSOLD_MIN, max_val=RSI_OVERSOLD_MAX, default=25.0,
    ),
    ParamSchema(
        name="rsi_overbought", param_type=float,
        min_val=RSI_OVERBOUGHT_MIN, max_val=RSI_OVERBOUGHT_MAX, default=75.0,
    ),

    # ── Trigger 3: Volatility Squeeze ────────────────────────────────────
    ParamSchema(
        name="bb_period", param_type=int,
        min_val=5, max_val=50, default=20,
    ),
    ParamSchema(
        name="bb_std", param_type=float,
        min_val=BB_STD_MIN, max_val=BB_STD_MAX, default=2.0,
    ),
    ParamSchema(
        name="squeeze_lookback", param_type=int,
        min_val=SQ_LBK_MIN, max_val=SQ_LBK_MAX, default=30,
    ),
    ParamSchema(
        name="squeeze_threshold_pct", param_type=float,
        min_val=SQ_THRESHOLD_MIN, max_val=SQ_THRESHOLD_MAX, default=2.0,
    ),

    # ── Rolling window periods ─────────────────────────────────────────────
    ParamSchema(
        name="atr_period", param_type=int,
        min_val=ATR_PERIOD_MIN, max_val=ATR_PERIOD_MAX, default=14,
    ),
    ParamSchema(
        name="ema_period", param_type=int,
        min_val=EMA_PERIOD_MIN, max_val=EMA_PERIOD_MAX, default=50,
    ),
    ParamSchema(
        name="rsi_period", param_type=int,
        min_val=7, max_val=30, default=14,
    ),
]

# Build a lookup dict for O(1) validation
_SCANNER_SCHEMA_MAP: dict[str, ParamSchema] = {s.name: s for s in SCANNER_PARAM_SCHEMAS}


# ─── ScannerParams ─────────────────────────────────────────────────────────────

@dataclass
class ScannerParams:
    """
    Unified scanner parameter bundle — the SINGLE source of truth.

    This dataclass replaces the two incompatible ScannerConfig classes that
    previously existed in scanner.py and meta_learning.py.

    All 12 fields are GA-evolvable within the hard constraints defined above.
    The GA genome IS this class — no translation layer needed.

    Fields
    ------
    sweep_lookback : int
        N-bar rolling window for lowest-high / highest-low in sweep detection.
        Higher = fewer but higher-quality signals.  Default 20.
    sweep_threshold_pct : float
        Minimum wick pierce distance as a fraction of price (0.0015 = 0.15%).
        Prevents micro-wicks from triggering.  Default 0.0015.
        GA hard floor: 0.005 (0.5%) — prevents noise generation.  G2 fix.
    deviation_atr_multiplier : float
        Price must be this many ATRs away from EMA50 to qualify as extreme
        deviation.  Higher = rarer, more significant signals.  Default 2.5.
    rsi_oversold : float
        RSI below this level in combination with extreme deviation = bullish
        exhaustion signal.  Default 25.0.
    rsi_overbought : float
        RSI above this level in combination with extreme deviation = bearish
        exhaustion signal.  Default 75.0.
    bb_period : int
        Bollinger Band period.  Default 20.
    bb_std : float
        Bollinger Band standard deviation multiplier.  Default 2.0.
    squeeze_lookback : int
        N-bar rolling window for BB width minimum (squeeze detection).
        Shorter = faster regime adaptation.  Default 30.
    squeeze_threshold_pct : float
        BB width must be within this % of its rolling minimum to qualify as
        a squeeze.  Default 2.0.
    atr_period : int
        ATR period (Wilder smoothing).  Default 14.
    ema_period : int
        EMA period for deviation calculation.  Default 50.
    rsi_period : int
        RSI period.  Default 14.
    """
    sweep_lookback:           int    = 20
    sweep_threshold_pct:     float  = 0.0015
    deviation_atr_multiplier: float  = 2.5
    rsi_oversold:            float  = 25.0
    rsi_overbought:           float  = 75.0
    bb_period:               int    = 20
    bb_std:                  float  = 2.0
    squeeze_lookback:        int    = 30
    squeeze_threshold_pct:    float  = 2.0
    atr_period:              int    = 14
    ema_period:              int    = 50
    rsi_period:              int    = 14

    # ── Validation ───────────────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """
        Validate ALL fields against the schema registry.

        Returns
        -------
        list[str]
            Empty list if all fields are valid; otherwise a list of error
            messages (one per invalid field).
        """
        errors: list[str] = []
        for schema in SCANNER_PARAM_SCHEMAS:
            value = getattr(self, schema.name, None)
            if value is None:
                errors.append(f"{schema.name} is not set")
                continue
            valid, msg = schema.validate(value)
            if not valid:
                errors.append(f"ScannerParams.{schema.name}: {msg}")
        return errors

    def raise_if_invalid(self) -> None:
        """Raise ValueError with all validation errors if any field is invalid."""
        errors = self.validate()
        if errors:
            raise ValueError(
                f"ScannerParams validation failed ({len(errors)} error(s)):\n  "
                + "\n  ".join(errors)
            )

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict of all field names → values."""
        return {
            "sweep_lookback":            self.sweep_lookback,
            "sweep_threshold_pct":       self.sweep_threshold_pct,
            "deviation_atr_multiplier":  self.deviation_atr_multiplier,
            "rsi_oversold":             self.rsi_oversold,
            "rsi_overbought":            self.rsi_overbought,
            "bb_period":                self.bb_period,
            "bb_std":                   self.bb_std,
            "squeeze_lookback":          self.squeeze_lookback,
            "squeeze_threshold_pct":    self.squeeze_threshold_pct,
            "atr_period":               self.atr_period,
            "ema_period":               self.ema_period,
            "rsi_period":               self.rsi_period,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ScannerParams:
        """Construct ScannerParams from a dict, ignoring unknown keys."""
        known = {f.name for f in SCANNER_PARAM_SCHEMAS}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)

    # ── Copy with overrides ──────────────────────────────────────────────────

    def with_updates(self, **kwargs: Any) -> ScannerParams:
        """Return a new ScannerParams with the given fields replaced."""
        import dataclasses
        params = dataclasses.replace(self)
        for key, val in kwargs.items():
            if hasattr(params, key):
                setattr(params, key, val)
        return params


# ─── Backward-compatibility alias ─────────────────────────────────────────────
#
# chronos_engine.py imports GeneticOptimizer, ScannerConfig from meta_learning.
# scanner.py references ScannerConfig as the config argument type.
# Keep ScannerConfig as a direct alias so existing import paths continue to work
# without code changes.  The alias points to the unified ScannerParams.
# G3 fix: one class, imported everywhere.

ScannerConfig = ScannerParams


# ─── QLearnConfig (deferred — G5 fix) ─────────────────────────────────────────
#
# Defined but NOT instantiated anywhere in the codebase as of V4.0.
# Decision: keep the definition with a DEFERRED_NOTE so it is clear this is
# intentional, and it will be wired up in Phase 2 of the MoE rollout.
# When Phase 2 begins, add:
#   from src.models.params import QLearnConfig
# to the relevant persona modules.

DEFERRED_NOTE = (
    "QLearnConfig is defined here as a type specification.  Instantiation and "
    "integration with persona Q-tables is deferred to Phase 2 of the MoE "
    "rollout (see docs/V4_META_LEARNING.md).  Do NOT remove this class."
)


@dataclass
class QLearnConfig:
    """
    Hyperparameters for the tabular Q-learning component used by each persona.

    DEFERRED: This class is defined but not instantiated in V4.0.  See
    docs/V4_META_LEARNING.md — Phase 2 task list.

    Attributes
    ----------
    learning_rate : float
        Initial learning rate α for Q-value updates.
        Q(s, a) ← Q(s, a) + α × (reward − Q(s, a))
        Typical range: 0.1 – 0.5.
    discount_factor : float
        Discount factor γ ∈ (0, 1).  Controls how much future rewards matter.
        γ = 0.95 is standard for short-horizon trading.
    epsilon_start : float
        Initial exploration rate ε.  The agent takes a random action with
        probability ε.
    epsilon_min : float
        Floor on ε after annealing.  Prevents complete exploitation.
    epsilon_decay : float
        Multiplicative decay per episode: ε ← ε × epsilon_decay.
        A value of 0.995 → slow annealing over many episodes.
    """
    learning_rate:   float = 0.2
    discount_factor: float = 0.95
    epsilon_start:   float = 1.0
    epsilon_min:     float = 0.05
    epsilon_decay:   float = 0.995
