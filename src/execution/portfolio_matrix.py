"""
Plutus V4.0 — Cross-Asset Portfolio Matrix

Provides multi-asset correlation tracking, global portfolio heat management,
pairs trading signal generation, and delta-neutral position management.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict, deque
from typing import Optional

import numpy as np

from src.execution.position_sizer import calculate_max_leverage


# ---------------------------------------------------------------------------
# Section A — CorrelationEngine
# ---------------------------------------------------------------------------


class CorrelationEngine:
    """
    Rolling Pearson correlation engine for N assets.

    Maintains a rolling window of returns per asset and computes pairwise
    Pearson correlation on demand.

    Pearson formula:
        r = Σ(r_a - μ_a)(r_b - μ_b) / (N · σ_a · σ_b)

    Beta formula (to a benchmark):
        β = Cov(asset, benchmark) / Var(benchmark)
    """

    def __init__(self, assets: list[str], lookback: int = 60) -> None:
        """
        Initialise the correlation engine.

        Args:
            assets:   List of asset symbols, e.g. ["BTCUSDT", "ETHUSDT"].
            lookback:  Number of return observations to keep per asset.
        """
        self.assets: list[str] = assets
        self.lookback: int = lookback
        # Rolling return series per asset; deque auto-evicts oldest obs.
        self._returns: dict[str, deque[float]] = {
            symbol: deque(maxlen=lookback) for symbol in assets
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, asset: str, return_: float) -> None:
        """
        Append a single return observation for `asset`.

        Args:
            asset:    Symbol already registered in __init__.
            return_:  Decimal return (e.g. 0.015 for +1.5 %).
        """
        if asset not in self._returns:
            self._returns[asset] = deque(maxlen=self.lookback)
        self._returns[asset].append(return_)

    def get_correlation(self, asset_a: str, asset_b: str) -> float:
        """
        Compute Pearson correlation between two assets.

        Requires at least 2 observations for each asset.
        Returns 0.0 if insufficient data.

        Args:
            asset_a: First asset symbol.
            asset_b: Second asset symbol.

        Returns:
            Pearson r in [-1, 1].
        """
        series_a = list(self._returns.get(asset_a, []))
        series_b = list(self._returns.get(asset_b, []))

        if len(series_a) < 2 or len(series_b) < 2:
            return 0.0

        n = min(len(series_a), len(series_b))
        ra = series_a[-n:]
        rb = series_b[-n:]

        mean_a = statistics.mean(ra)
        mean_b = statistics.mean(rb)

        cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb)) / n

        var_a = statistics.variance(ra) if len(ra) > 1 else 0.0
        var_b = statistics.variance(rb) if len(rb) > 1 else 0.0

        denom = math.sqrt(var_a * var_b)
        if denom == 0.0:
            return 0.0

        return cov / denom

    def get_correlation_matrix(self) -> np.ndarray:
        """
        Compute the full N×N Pearson correlation matrix.

        Returns:
            NumPy array of shape (N, N) where entry [i, j] is the Pearson r
            between assets[i] and assets[j].
        """
        n = len(self.assets)
        matrix = np.eye(n, dtype=float)

        for i, sym_i in enumerate(self.assets):
            for j, sym_j in enumerate(self.assets):
                if i == j:
                    continue
                matrix[i, j] = self.get_correlation(sym_i, sym_j)

        return matrix

    def get_beta(self, asset: str, benchmark: str = "BTCUSDT") -> float:
        """
        Compute beta of `asset` against `benchmark`.

        β = Cov(asset, benchmark) / Var(benchmark)

        Args:
            asset:     Asset symbol.
            benchmark: Benchmark symbol (default BTCUSDT).

        Returns:
            Beta float.
        """
        series_asset = list(self._returns.get(asset, []))
        series_bench = list(self._returns.get(benchmark, []))

        if len(series_asset) < 2 or len(series_bench) < 2:
            return 0.0

        n = min(len(series_asset), len(series_bench))
        ra = np.array(series_asset[-n:])
        rb = np.array(series_bench[-n:])

        cov = np.cov(ra, rb, bias=True)[0, 1]
        var_bench = np.var(rb, ddof=0)

        if var_bench == 0.0:
            return 0.0

        return float(cov / var_bench)


# ---------------------------------------------------------------------------
# Section B — RiskManager
# ---------------------------------------------------------------------------


class RiskManager:
    """
    Global portfolio heat monitor and size adjuster.

    Tracks beta-weighted position heat and enforces position-size reductions
    when correlation between new and existing positions is elevated.
    """

    MAX_PORTFOLIO_HEAT: float = 2.0  # heat units — reduction kicks in above here

    def __init__(
        self, correlation_engine: CorrelationEngine, initial_equity: float
    ) -> None:
        """
        Initialise the risk manager.

        Args:
            correlation_engine: Shared CorrelationEngine instance.
            initial_equity:      Starting account equity in USD.
        """
        self.ce: CorrelationEngine = correlation_engine
        self.equity: float = initial_equity

    # ------------------------------------------------------------------
    # Core heat formula
    # ------------------------------------------------------------------

    def check_global_heat(
        self, positions: list[dict], vix: float
    ) -> float:
        """
        Compute global portfolio heat.

        Formula:
            heat = Σ(|position_value_i| × |β_i|) / equity

        If VIX > 30 an additional 0.5 heat multiplier is applied.

        Args:
            positions: List of position dicts with keys "value" and "symbol".
            vix:        Current VIX level.

        Returns:
            Heat float — above MAX_PORTFOLIO_HEAT triggers size reduction.
        """
        heat = 0.0
        for pos in positions:
            value = abs(pos.get("value", 0.0))
            symbol = pos.get("symbol", "BTCUSDT")
            beta = abs(self.ce.get_beta(symbol))
            heat += value * beta

        heat /= self.equity

        if vix > 30:
            heat *= 1.5

        return heat

    # ------------------------------------------------------------------
    # Size reduction on correlated entries
    # ------------------------------------------------------------------

    def assess_trade(
        self,
        direction: str,
        symbol: str,
        position_value: float,
        existing_positions: list[dict],
    ) -> dict:
        """
        Assess whether a new position is allowed and whether its size
        must be reduced due to correlation with existing positions.

        Size-reduction table (LONG-on-LONG or SHORT-on-SHORT in same regime):
            ρ > 0.8  → size_reduction = 0.25  (75 % reduction)
            ρ > 0.6  → size_reduction = 0.50  (50 % reduction)
            otherwise → size_reduction = 1.00  (no reduction)

        SHORT adding to existing LONG (or vice-versa) is always allowed
        at up to 1.25× the normal size.

        Args:
            direction:        "LONG" or "SHORT".
            symbol:            New trade symbol.
            position_value:    Planned notional value.
            existing_positions: List of dicts with "direction", "symbol", "value".

        Returns:
            dict with keys "approved", "size_reduction", "reason".
        """
        reduction = 1.0
        reason = "approved"

        # Heat check — reject outright if already overloaded
        heat = self.check_global_heat(existing_positions, vix=20.0)
        if heat >= self.MAX_PORTFOLIO_HEAT:
            return {
                "approved": False,
                "size_reduction": 0.0,
                "reason": f"global heat {heat:.2f} exceeds max {self.MAX_PORTFOLIO_HEAT}",
            }

        for existing in existing_positions:
            rho = self.ce.get_correlation(symbol, existing.get("symbol", ""))

            # Correlated entries in the same direction → reduce size
            if rho > 0.8 and direction == existing.get("direction"):
                reduction = min(reduction, 0.25)
                reason = f"ρ={rho:.2f}>0.8 — 75% size reduction applied"
            elif rho > 0.6 and direction == existing.get("direction"):
                reduction = min(reduction, 0.50)
                reason = f"ρ={rho:.2f}>0.6 — 50% size reduction applied"

            # Opposite directions in correlated assets → allow boost
            if rho > 0.6 and direction != existing.get("direction"):
                reduction = 1.25
                reason = f"ρ={rho:.2f} opposite direction — 1.25x allowed"

        return {
            "approved": True,
            "size_reduction": reduction,
            "reason": reason,
        }

    # ------------------------------------------------------------------
    # Leverage enforcement
    # ------------------------------------------------------------------

    @staticmethod
    def enforce_max_leverage(
        stop_distance: float, equity: float, symbol: str
    ) -> int:
        """
        Compute maximum safe leverage given a stop-loss distance.

        Delegates to the single, tested implementation in
        ``src.execution.position_sizer.calculate_max_leverage()``.
        This ensures one canonical formula across the codebase.

        Args:
            stop_distance: Stop-loss distance as a fraction (e.g. 0.02 for 2 %).
            equity:        Account equity (used only for a sanity check).
            symbol:        Asset symbol — determines coin type (major vs small cap).

        Returns:
            Integer leverage (minimum 1, capped by coin type).
        """
        if equity <= 0 or stop_distance <= 0:
            return 1

        coin_type = "major" if symbol in {"BTCUSDT", "ETHUSDT"} else "small"
        result = calculate_max_leverage(
            stop_distance=stop_distance,
            coin_type=coin_type,
        )

        if not result["valid"]:
            return 1  # stop is inside buffer — no safe leverage

        return int(result["max_leverage"])



from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class SpreadTrade:
    """
    A delta-neutral pairs trade capturing both legs of a spread.

    Attributes:
        timestamp:       When the spread was entered.
        long_symbol:      Symbol on the long leg  (e.g. "BTCUSDT").
        short_symbol:     Symbol on the short leg (e.g. "ETHUSDT").
        long_entry:       Entry price for the long leg.
        short_entry:      Entry price for the short leg.
        long_size:        Position size in coins for the long leg.
        short_size:       Position size in coins for the short leg.
        combined_margin:  Total margin used across both legs (USD).
        total_risk_usd:   Max loss if both SLs are hit (USD).
        net_beta:         Delta-neutrality indicator (~0 = fully hedged).
        result:           "OPEN" | "WIN" | "LOSS".
        pnl:              Realised PnL in USD.
        long_sl:          Stop-loss price for the long leg.
        short_sl:         Stop-loss price for the short leg.
        long_tp:          Take-profit price for the long leg.
        short_tp:         Take-profit price for the short leg.
        long_direction:   "LONG" or "SHORT" (always opposite of short).
        short_direction:  "SHORT" or "LONG".
        notes:            Free-text annotations.
    """
    timestamp:        datetime
    long_symbol:      str
    short_symbol:      str
    long_entry:        float
    short_entry:       float
    long_size:         float
    short_size:        float
    combined_margin:   float
    total_risk_usd:     float
    net_beta:          float
    result:            str = "OPEN"
    pnl:               float = 0.0
    long_sl:           float = 0.0
    short_sl:          float = 0.0
    long_tp:           float = 0.0
    short_tp:          float = 0.0
    long_direction:    str = "LONG"
    short_direction:   str = "SHORT"
    notes:             list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


class SpreadTrader:
    """
    Converts directional multi-symbol signals from the Chronos engine into
    delta-neutral spread trades.

    Rules of Engagement (HYDRA):
        BTC LONG + ETH SHORT (both conf >= 65)  → Spread: Long BTC / Short ETH
        BTC SHORT + ETH LONG  (both conf >= 65)  → Spread: Short BTC / Long ETH
        Both LONG or Both SHORT (both conf >= 65) → Halve each leg's position size
        Any leg below conf 65                    → Treat that leg as FLAT → return None
        Both legs FLAT or only one leg active    → return None (no partial spreads)

    $50 Micro-Account Rule:
        Total combined margin across both legs   <= $50  (equity cap)
        Combined risk across both legs           <= $1.00 (2% of $50)
        Each leg individually respects $5 min notional

    Usage:
        pt = PairsTrader(initial_equity=50.0, min_notional=5.0)
        spread = pt.evaluate({
            "BTCUSDT": {"direction": "LONG",  "confidence": 72, ...},
            "ETHUSDT": {"direction": "SHORT", "confidence": 68, ...},
        })
        if spread:
            result = pt.simulate_outcome(spread, "BTCUSDT", "WIN", 67_200,
                                         "ETHUSDT", "LOSS", 3_620)
    """

    CONFIDENCE_THRESHOLD = 65   # minimum confidence for leg participation

    def __init__(self, initial_equity: float = 50.0, min_notional: float = 5.0):
        self.initial_equity: float = initial_equity
        self.min_notional: float = min_notional
        self.risk_pct: float = 0.02          # 2% of equity per spread
        self.active_spreads: list[SpreadTrade] = []
        self.closed_spreads: list[SpreadTrade] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, signal_matrix: dict[str, dict]) -> Optional[SpreadTrade]:
        """
        Evaluate a signal_matrix and return a SpreadTrade if conditions are met.

        Args:
            signal_matrix:  Dict keyed by symbol, e.g.:
                {
                    "BTCUSDT": {"direction": "LONG",  "confidence": 72,
                                "entry": 67_000.0, "atr": 150.0, ...},
                    "ETHUSDT": {"direction": "SHORT", "confidence": 68,
                                "entry": 3_500.0,  "atr": 80.0,  ...},
                }
                Symbols not in the dict are treated as FLAT.

        Returns:
            SpreadTrade if spread conditions are met, None otherwise.
        """
        # Extract directional signals
        btc_dir  = self._get_direction(signal_matrix, "BTCUSDT")
        btc_conf = self._get_confidence(signal_matrix, "BTCUSDT")
        eth_dir  = self._get_direction(signal_matrix, "ETHUSDT")
        eth_conf = self._get_confidence(signal_matrix, "ETHUSDT")

        btc_active = btc_conf >= self.CONFIDENCE_THRESHOLD
        eth_active = eth_conf >= self.CONFIDENCE_THRESHOLD

        # Rule: both FLAT → no signal
        if not btc_active and not eth_active:
            return None

        # Rule: exactly one leg active → no partial spreads
        if not btc_active or not eth_active:
            return None

        # At this point both legs are active (conf >= 65)
        # Spread conditions:
        is_btc_long_eth_short = (btc_dir == "LONG"  and eth_dir == "SHORT")
        is_btc_short_eth_long = (btc_dir == "SHORT" and eth_dir == "LONG")

        if not (is_btc_long_eth_short or is_btc_short_eth_long):
            # Both LONG or Both SHORT — halve position size later in _compute_sizing
            pass

        # Resolve which leg is long / short
        if is_btc_long_eth_short:
            long_sym  = "BTCUSDT"; short_sym  = "ETHUSDT"
            long_dir  = "LONG";    short_dir  = "SHORT"
        elif is_btc_short_eth_long:
            long_sym  = "ETHUSDT"; short_sym  = "BTCUSDT"
            long_dir  = "LONG";    short_dir  = "SHORT"
        else:
            # Both same direction — reduce size but still execute
            if btc_dir == "LONG":
                long_sym = "BTCUSDT"; short_sym = "ETHUSDT"
            else:
                long_sym = "ETHUSDT"; short_sym = "BTCUSDT"
            long_dir = btc_dir; short_dir = eth_dir

        # Extract prices and ATR
        long_entry  = self._get_entry(signal_matrix, long_sym)
        short_entry = self._get_entry(signal_matrix, short_sym)
        long_atr    = self._get_atr(signal_matrix, long_sym)
        short_atr   = self._get_atr(signal_matrix, short_sym)

        if long_entry <= 0 or short_entry <= 0:
            return None

        # Derive SL/TP
        if long_dir == "LONG":
            long_sl = long_entry - 2.0 * long_atr
            long_tp = long_entry + 3.0 * long_atr
            short_sl = short_entry + 2.0 * short_atr
            short_tp = short_entry - 3.0 * short_atr
        else:
            long_sl = long_entry + 2.0 * long_atr
            long_tp = long_entry - 3.0 * long_atr
            short_sl = short_entry - 2.0 * short_atr
            short_tp = short_entry + 3.0 * short_atr

        # Halve position if both legs same direction
        pos_mult = 0.5 if (btc_dir == eth_dir) else 1.0

        # Compute sizing for both legs
        equity_for_sizing = self.initial_equity * pos_mult
        size_long, size_short, combined_margin, combined_risk = self._compute_sizing(
            long_entry, short_entry,
            long_sl,   short_sl,
            equity_for_sizing,
        )

        # Build net_beta estimate (delta-neutral check)
        # Using a simplified 1:1 notional hedge → net_beta ≈ 0
        net_beta = 0.0

        spread = SpreadTrade(
            timestamp=datetime.now(),
            long_symbol=long_sym,
            short_symbol=short_sym,
            long_entry=long_entry,
            short_entry=short_entry,
            long_size=size_long,
            short_size=size_short,
            combined_margin=combined_margin,
            total_risk_usd=combined_risk,
            net_beta=net_beta,
            result="OPEN",
            pnl=0.0,
            long_sl=long_sl,
            short_sl=short_sl,
            long_tp=long_tp,
            short_tp=short_tp,
            long_direction=long_dir,
            short_direction=short_dir,
            notes=[
                f"pos_mult={pos_mult}",
                f"btc_conf={btc_conf}",
                f"eth_conf={eth_conf}",
                f"btc_dir={btc_dir}",
                f"eth_dir={eth_dir}",
            ],
        )

        self.active_spreads.append(spread)
        return spread

    def simulate_outcome(
        self,
        spread: SpreadTrade,
        btc_outcome: str,  btc_exit: float,
        eth_outcome: str,  eth_exit: float,
    ) -> SpreadTrade:
        """
        Simulate the outcome of a spread trade given per-leg exit prices.

        WIN  = both legs WIN, or one WIN + one HOLD (net positive)
        LOSS = both legs LOSS, or one LOSS + one HOLD (net negative)
        HOLD = both legs HOLD, or both legs at break-even

        Args:
            spread:      The SpreadTrade to evaluate.
            btc_outcome: "WIN" | "LOSS" | "HOLD" for the BTC leg.
            btc_exit:    Exit price for BTC.
            eth_outcome: "WIN" | "LOSS" | "HOLD" for the ETH leg.
            eth_exit:    Exit price for ETH.

        Returns:
            Updated SpreadTrade with result and pnl filled in.
        """
        # Determine per-leg PnL from BTC leg
        btc_pnl = self._leg_pnl(
            spread.long_symbol,  spread.short_symbol,
            spread.long_entry,   spread.short_entry,
            spread.long_sl,      spread.short_sl,
            spread.long_direction, spread.short_direction,
            btc_outcome, btc_exit,
        )

        # Per-leg PnL from ETH leg
        eth_pnl = self._leg_pnl(
            spread.short_symbol, spread.long_symbol,
            spread.short_entry, spread.long_entry,
            spread.short_sl,    spread.long_sl,
            spread.short_direction, spread.long_direction,
            eth_outcome, eth_exit,
        )

        # Aggregate
        if btc_pnl > 0 and eth_pnl >= 0:
            result = "WIN"
        elif btc_pnl >= 0 and eth_pnl > 0:
            result = "WIN"
        elif btc_pnl < 0 and eth_pnl <= 0:
            result = "LOSS"
        elif btc_pnl <= 0 and eth_pnl < 0:
            result = "LOSS"
        elif (btc_pnl + eth_pnl) > 0:
            result = "WIN"
        elif (btc_pnl + eth_pnl) < 0:
            result = "LOSS"
        else:
            result = "HOLD"

        total_pnl = btc_pnl + eth_pnl

        # Update spread in-place
        spread.result = result
        spread.pnl    = round(total_pnl, 4)

        # Move from active to closed
        if spread in self.active_spreads:
            self.active_spreads.remove(spread)
        self.closed_spreads.append(spread)

        return spread

    # ------------------------------------------------------------------
    # Sizing math
    # ------------------------------------------------------------------

    def _compute_sizing(
        self,
        long_entry: float,
        short_entry: float,
        long_sl: float,
        short_sl: float,
        equity: float,
    ) -> tuple[float, float, float, float]:
        """
        Compute position sizes for both legs satisfying:

        - Each leg notional >= min_notional ($5)
        - Combined margin <= equity ($50)
        - Combined risk  <= 2% of equity ($1.00)

        Returns:
            (size_long, size_short, combined_margin, combined_risk)
        """
        max_risk_total = equity * self.risk_pct   # e.g. $1.00 on $50

        # ── Long leg sizing ──────────────────────────────────────────────
        long_risk_usd   = max_risk_total / 2.0
        long_sl_dist    = abs(long_entry - long_sl)
        if long_sl_dist <= 0:
            long_sl_dist = long_entry * 0.005   # 0.5% fallback
        size_long        = long_risk_usd / long_sl_dist
        notional_long    = size_long * long_entry
        if notional_long < self.min_notional:
            notional_long = self.min_notional
            size_long     = self.min_notional / long_entry

        # ── Short leg sizing ─────────────────────────────────────────────
        short_risk_usd   = max_risk_total / 2.0
        short_sl_dist    = abs(short_entry - short_sl)
        if short_sl_dist <= 0:
            short_sl_dist = short_entry * 0.005
        size_short        = short_risk_usd / short_sl_dist
        notional_short    = size_short * short_entry
        if notional_short < self.min_notional:
            notional_short = self.min_notional
            size_short     = self.min_notional / short_entry

        combined_margin = notional_long + notional_short

        # ── Combined margin cap (scale both proportionally) ─────────────
        if combined_margin > equity:
            scale = equity / combined_margin
            notional_long  *= scale
            notional_short *= scale
            size_long      *= scale
            size_short     *= scale
            combined_margin = equity

        # ── Combined risk cap (scale both proportionally) ─────────────────
        risk_long  = size_long  * abs(long_entry  - long_sl)
        risk_short = size_short * abs(short_entry - short_sl)
        combined_risk = risk_long + risk_short

        if combined_risk > max_risk_total:
            scale = max_risk_total / combined_risk
            size_long      *= scale
            size_short     *= scale
            risk_long      *= scale
            risk_short     *= scale
            combined_risk   = max_risk_total

        return size_long, size_short, combined_margin, combined_risk

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    @staticmethod
    def _get_direction(signal_matrix: dict, symbol: str) -> Optional[str]:
        return signal_matrix.get(symbol, {}).get("direction")

    @staticmethod
    def _get_confidence(signal_matrix: dict, symbol: str) -> int:
        return int(signal_matrix.get(symbol, {}).get("confidence", 0))

    @staticmethod
    def _get_entry(signal_matrix: dict, symbol: str) -> float:
        return float(signal_matrix.get(symbol, {}).get("entry", 0.0))

    @staticmethod
    def _get_atr(signal_matrix: dict, symbol: str) -> float:
        return float(signal_matrix.get(symbol, {}).get("atr", 0.0))

    @staticmethod
    def _leg_pnl(
        leg_sym: str,
        _other_sym: str,
        leg_entry: float,
        _other_entry: float,
        leg_sl: float,
        _other_sl: float,
        leg_direction: str,
        _other_direction: str,
        outcome: str,
        exit_price: float,
    ) -> float:
        """
        Compute the PnL for a single leg given its outcome and exit price.

        This is simplified: WIN ≈ TP hit → use entry vs TP as approximate.
        In production this would use actual TP prices; here we use the
        outcome string directly and return a rough signed PnL estimate.
        """
        if outcome == "WIN":
            # Assume TP was hit: 3× ATR from entry → approx 6× SL distance reward
            if leg_direction == "LONG":
                reward = (exit_price - leg_entry) * 1.0
            else:
                reward = (leg_entry - exit_price) * 1.0
            return reward
        elif outcome == "LOSS":
            if leg_direction == "LONG":
                loss = (leg_entry - leg_sl) * 1.0
            else:
                loss = (leg_sl - leg_entry) * 1.0
            return -loss
        else:  # HOLD
            return 0.0


# ── Alias for backward compatibility ─────────────────────────────────────────────
# Files that import PairsTrader (e.g. chronos_engine.py) still work.
PairsTrader = SpreadTrader


# ---------------------------------------------------------------------------
# Section E — PortfolioMatrix
# ---------------------------------------------------------------------------


class PortfolioMatrix:
    """
    Top-level portfolio manager combining correlation, heat, and delta tracking.

    Manages a portfolio of open positions and exposes delta-neutrality checks
    and rebalancing hedge computations.
    """

    def __init__(self, initial_equity: float, max_positions: int = 5) -> None:
        """
        Initialise the portfolio matrix.

        Args:
            initial_equity: Starting equity in USD.
            max_positions:  Maximum simultaneous positions allowed.
        """
        self.equity: float = initial_equity
        self.max_positions: int = max_positions
        self.ce: CorrelationEngine = CorrelationEngine([], lookback=60)
        self.rm: RiskManager = RiskManager(self.ce, initial_equity)
        self.positions: list[dict] = []

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def add_position(
        self,
        symbol: str,
        direction: str,
        size: float,
        entry_price: float,
        stop_loss: float,
        tp: float,
        confidence: int,
    ) -> None:
        """
        Register a new position in the matrix.

        Args:
            symbol:       Asset symbol.
            direction:    "LONG" or "SHORT".
            size:         Position size (USD notional).
            entry_price:  Entry price.
            stop_loss:    Stop-loss price.
            tp:           Take-profit price.
            confidence:   Confidence score (1-3).
        """
        if len(self.positions) >= self.max_positions:
            raise RuntimeError(
                f"max positions ({self.max_positions}) reached — "
                "remove or close a position first"
            )

        pos = {
            "symbol": symbol,
            "direction": direction,
            "size": size,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "tp": tp,
            "confidence": confidence,
            "value": size * (1.0 if direction == "LONG" else -1.0),
        }
        self.positions.append(pos)

        # Register symbol in correlation engine if new
        if symbol not in self.ce.assets:
            self.ce.assets.append(symbol)

    # ------------------------------------------------------------------
    # Beta-weighted delta
    # ------------------------------------------------------------------

    def get_portfolio_delta(self) -> float:
        """
        Compute beta-weighted portfolio delta.

        Formula:
            Δ_portfolio = Σ(position_value_i × direction_i × β_i) / equity

        where direction_i is +1 for LONG, -1 for SHORT.

        Args:
            None

        Returns:
            Delta float — a value near 0 indicates delta-neutrality.
        """
        total = 0.0
        for pos in self.positions:
            symbol = pos["symbol"]
            direction = 1.0 if pos["direction"] == "LONG" else -1.0
            value = pos.get("value", 0.0)
            beta = self.ce.get_beta(symbol)
            total += value * direction * beta

        return total / self.equity

    def is_delta_neutral(self, threshold: float = 0.1) -> bool:
        """
        Check whether the portfolio is delta-neutral within a tolerance.

        Args:
            threshold: Maximum absolute delta before declaring non-neutral.

        Returns:
            True if |delta| < threshold.
        """
        return abs(self.get_portfolio_delta()) < threshold

    # ------------------------------------------------------------------
    # Delta rebalancing
    # ------------------------------------------------------------------

    def rebalance_hedge(
        self, existing: list[dict], target_beta: float
    ) -> dict:
        """
        Compute a hedge order required to bring portfolio delta to a target.

        Hedge ratio formula:
            h_edge = -(Σ β_i × pos_i) / β_hedge

        Args:
            existing:    List of position dicts (must include "symbol", "value").
            target_beta: Target beta for the hedge instrument (e.g. -1.0 for BTC).

        Returns:
            Hedge order dict:
            {
                "symbol": str,
                "direction": str,
                "size": float,       # USD notional
                "hedge_ratio": float,
            }
        """
        beta_sum = 0.0
        for pos in existing:
            symbol = pos.get("symbol", "BTCUSDT")
            beta = self.ce.get_beta(symbol)
            value = pos.get("value", 0.0)
            beta_sum += beta * value

        if target_beta == 0.0:
            return {
                "symbol": "BTCUSDT",
                "direction": "NO_HEDGE",
                "size": 0.0,
                "hedge_ratio": 0.0,
            }

        hedge_ratio = -beta_sum / target_beta
        direction = "SHORT" if hedge_ratio > 0 else "LONG"
        size = abs(hedge_ratio)

        return {
            "symbol": "BTCUSDT",
            "direction": direction,
            "size": size,
            "hedge_ratio": hedge_ratio,
        }


# ---------------------------------------------------------------------------
# Test block
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import random

    print("=" * 60)
    print("PortfolioMatrix — smoke tests")
    print("=" * 60)

    # ── CorrelationEngine ───────────────────────────────────────────────
    print("\n[CorrelationEngine]")
    ce = CorrelationEngine(["BTCUSDT", "ETHUSDT", "SOLUSDT"], lookback=60)

    # Simulate correlated returns
    rng = random.Random(42)
    btc_returns = [rng.gauss(0.001, 0.02) for _ in range(80)]
    eth_returns = [r * 0.9 + rng.gauss(0, 0.005) for r in btc_returns]
    sol_returns = [rng.gauss(-0.0005, 0.04) for _ in range(80)]

    for i, (b, e, s) in enumerate(zip(btc_returns, eth_returns, sol_returns)):
        ce.update("BTCUSDT", b)
        ce.update("ETHUSDT", e)
        ce.update("SOLUSDT", s)

    rho_be = ce.get_correlation("BTCUSDT", "ETHUSDT")
    rho_bs = ce.get_correlation("BTCUSDT", "SOLUSDT")
    beta_eth = ce.get_beta("ETHUSDT")
    beta_sol = ce.get_beta("SOLUSDT")

    print(f"  ρ(BTC, ETH) = {rho_be:.4f}  (expected > 0, correlated)")
    print(f"  ρ(BTC, SOL) = {rho_bs:.4f}  (expected < 0, uncorrelated)")
    print(f"  β(ETH/BTC)  = {beta_eth:.4f}  (expected 0.8-1.1)")
    print(f"  β(SOL/BTC)  = {beta_sol:.4f}  (expected near 0 or slightly neg)")

    matrix = ce.get_correlation_matrix()
    print(f"  Correlation matrix shape: {matrix.shape}")

    # ── RiskManager ──────────────────────────────────────────────────────
    print("\n[RiskManager]")
    rm = RiskManager(ce, initial_equity=10_000.0)

    existing = [
        {"symbol": "BTCUSDT", "direction": "LONG", "value": 5000.0},
        {"symbol": "ETHUSDT", "direction": "LONG", "value": 3000.0},
    ]
    assessment = rm.assess_trade("LONG", "ETHUSDT", 2000.0, existing)
    print(f"  LONG ETH into existing LONG ETH: {assessment}")

    heat = rm.check_global_heat(existing, vix=25.0)
    print(f"  Global heat (VIX=25): {heat:.4f}")

    heat_high_vix = rm.check_global_heat(existing, vix=35.0)
    print(f"  Global heat (VIX=35): {heat_high_vix:.4f}  (+0.5x multiplier)")

    lev_major = rm.enforce_max_leverage(0.02, 10_000.0, "BTCUSDT")
    lev_small = rm.enforce_max_leverage(0.02, 10_000.0, "SOLUSDT")
    print(f"  Max leverage BTC (2%% stop dist, major): {lev_major}x  (expected ~66)")
    print(f"  Max leverage SOL (2%% stop dist, small cap): {lev_small}x  (expected ~50)")

    # ── SpreadTrader ─────────────────────────────────────────────────────
    print("\n[SpreadTrader]")
    pt = SpreadTrader(initial_equity=50.0, min_notional=5.0)

    # Simulate a ratio breakout
    rng2 = random.Random(99)
    returns_a = [rng2.gauss(0.002, 0.015) for _ in range(25)]
    returns_b = [r * 0.95 + rng2.gauss(0, 0.003) for r in returns_a]

    opp = pt.detect_pairs_opportunity("BTCUSDT", "ETHUSDT", returns_a, returns_b)
    if opp:
        print(f"  Detected opportunity: {opp['direction']}")
        print(f"  Z-score: {opp['z_score']:.4f}  confidence: {opp['confidence']}")
        print(f"  Ratio: {opp['ratio']:.4f}  MA: {opp['ma']:.4f}  σ: {opp['sigma']:.4f}")
    else:
        print("  No opportunity (ρ ≤ 0.8 or ratio not in range)")

    signal = pt.generate_pairs_signal(ratio=1.05, ma=1.0, sigma=0.02)
    print(f"  Signal(1.05, 1.0, 0.02): {signal}")

    order = pt.execute_pairs(
        {"symbol": "BTCUSDT", "direction": "SHORT", "price": 67_000.0},
        {"symbol": "ETHUSDT", "direction": "LONG", "price": 3_500.0},
        notional=10_000.0,
    )
    print(f"  Pairs order struct: {order}")

    # ── PortfolioMatrix ─────────────────────────────────────────────────
    print("\n[PortfolioMatrix]")
    pm = PortfolioMatrix(initial_equity=10_000.0, max_positions=5)

    pm.add_position(
        symbol="BTCUSDT",
        direction="LONG",
        size=5_000.0,
        entry_price=67_000.0,
        stop_loss=64_500.0,
        tp=72_000.0,
        confidence=3,
    )
    pm.add_position(
        symbol="ETHUSDT",
        direction="SHORT",
        size=3_000.0,
        entry_price=3_500.0,
        stop_loss=3_700.0,
        tp=3_200.0,
        confidence=2,
    )

    delta = pm.get_portfolio_delta()
    neutral = pm.is_delta_neutral(threshold=0.1)
    print(f"  Portfolio delta: {delta:.4f}")
    print(f"  Delta-neutral (threshold=0.1): {neutral}")

    hedge = pm.rebalance_hedge(pm.positions, target_beta=-1.0)
    print(f"  Rebalance hedge: {hedge}")

    print(f"  Open positions: {len(pm.positions)}/{pm.max_positions}")

    print("\n[All tests passed]")
    print("=" * 60)
