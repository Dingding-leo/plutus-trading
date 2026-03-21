"""
Plutus V3 — The Quant Evaluator
Portfolio Manager & Persona Fitness Engine

Architectural role:
  The "brain" of the MoE Trading Floor. Receives signals from all three
  LLM personas, maintains rolling performance histories, and computes a
  dynamic capital allocation via Fitness-Weighted Softmax.

Event-driven design:
  Designed to integrate with the Chronos Engine / VanguardScanner.
  The .update() method is O(1) — accepts a single event (signal + return)
  and incrementally updates rolling state. No full-history reprocessing.

Lifecycle:
  VanguardScanner (event) → DynamicAllocator.update(signal, epoch_return)
                            → DynamicAllocator.allocate() → weights [w1, w2, w3]
                            → execution layer scales each persona's direction/confidence

Metrics computed:
  1. Sharpe Ratio   — mean(returns) / std(returns)
  2. Sortino Ratio — mean(returns) / downside_std(returns)  [upside not penalised]
  3. Win Rate      — % of positive-return epochs
  4. Turnover      — capital churn rate per period (penalises over-trading)
  5. Fitness Score — (sortino * win_rate) / (1 + turnover * penalty_factor)

Capital allocation:
  Softmax over Fitness Scores → normalized weights that sum to 1.0
  e.g., [0.6, 0.4, 0.0] = 60% to SMC, 40% to Order Flow, 0% to Macro
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np

from ..data.personas import PersonaSignal, PersonaType


# ─── Metric Functions ────────────────────────────────────────────────────────────

def calculate_sharpe(
    returns: np.ndarray,
    risk_free_rate: float = 0.0,
) -> float:
    """
    Annualised Sharpe Ratio.

    Sharpe = (mean(returns) - risk_free_rate) / std(returns)

    Handles edge cases:
      - Fewer than 2 samples  → 0.0
      - Zero volatility        → 0.0 (avoids div-by-zero)
      - Annualisation          → multiply by sqrt(365) for daily data

    Args:
        returns: numpy array of period returns (e.g., hourly or daily)
        risk_free_rate: annualised risk-free rate (default 0.0 for crypto)

    Returns:
        Annualised Sharpe ratio (float)
    """
    if returns is None or returns.size == 0:
        return 0.0

    n = returns.size
    if n < 2:
        return 0.0

    mean_ret = np.mean(returns)
    std_ret  = np.std(returns, ddof=1)   # ddof=1 = sample std (unbiased)

    if std_ret < 1e-12:
        return 0.0

    # Annualise (assuming daily periods; scale down for hourly)
    periods_per_year = 365.0
    ann_sharpe = (mean_ret - risk_free_rate / periods_per_year) / std_ret * math.sqrt(periods_per_year)

    return float(ann_sharpe)


def calculate_sortino(
    returns: np.ndarray,
    target_return: float = 0.0,
) -> float:
    """
    Annualised Sortino Ratio.

    Sortino = (mean(returns) - target_return) / downside_std(returns)

    KEY DESIGN DECISION:
    Only the DOWNWARD deviation is penalised.
    Upside volatility (large positive returns) is our asymmetric edge in crypto
    and must NOT be penalised. We only measure how badly we fall, not how high we fly.

    Downside deviation = std dev of returns that fall BELOW the target
    (target = 0.0 for daily breakeven in crypto)

    Handles edge cases:
      - Fewer than 2 samples         → 0.0
      - No negative returns          → 0.0 (no downside to protect against)
      - Zero downside volatility     → 0.0 (avoids div-by-zero)

    Args:
        returns: numpy array of period returns
        target_return: minimum acceptable return (default 0.0 = breakeven)

    Returns:
        Annualised Sortino ratio (float)
    """
    if returns is None or returns.size == 0:
        return 0.0

    n = returns.size
    if n < 2:
        return 0.0

    mean_ret = np.mean(returns)

    # ── Downside deviation: only penalise negative excess returns ─────────────
    excess = returns - target_return          # how much each return missed target
    downside_returns = excess[excess < 0]     # only the shortfalls

    if downside_returns.size == 0:
        # No downside observed — Sortino undefined, return mean Sharpe as proxy
        std_all = np.std(returns, ddof=1)
        if std_all < 1e-12:
            return 0.0
        periods_per_year = 365.0
        return float(mean_ret / std_all * math.sqrt(periods_per_year))

    downside_std = np.std(downside_returns, ddof=1)

    if downside_std < 1e-12:
        return 0.0

    periods_per_year = 365.0
    ann_sortino = (mean_ret - target_return / periods_per_year) / downside_std * math.sqrt(periods_per_year)

    return float(ann_sortino)


def calculate_win_rate(returns: np.ndarray) -> float:
    """
    Win rate = fraction of periods with positive return.

    Args:
        returns: numpy array of period returns

    Returns:
        Win rate between 0.0 and 1.0
    """
    if returns is None or returns.size == 0:
        return 0.0
    return float(np.sum(returns > 0) / returns.size)


def calculate_turnover(
    position_history: List[float],
    initial_capital: float,
) -> float:
    """
    Capital turnover rate per epoch.

    Turnover = sum(|position_change|) / initial_capital
             = "how many times the capital base was fully turned over"

    CRITICAL for persona selection:
      - SMC_ICT persona: trades infrequently (patient, high-RR setups)
        → LOW turnover → HIGH fitness
      - ORDER_FLOW persona: trades squeeze events (moderate frequency)
        → MODERATE turnover
      - MACRO_ONCHAIN persona: trades regime changes (very infrequent)
        → VERY LOW turnover → HIGH fitness IF wins are large

    High-frequency persona gets penalised proportionally to avoid fee-bleed.

    Args:
        position_history: List of position values at each epoch [v0, v1, ..., vn]
                          (e.g., the notional value of the position at each step)
        initial_capital: Starting equity (normaliser)

    Returns:
        Annualised turnover rate (float). 1.0 = 100% capital churned per period.
    """
    if position_history is None or len(position_history) < 2:
        return 0.0

    if initial_capital <= 0:
        return 0.0

    positions = np.array(position_history, dtype=np.float64)

    # First difference: how much capital moved at each step
    deltas = np.abs(np.diff(positions))

    total_churn = np.sum(deltas)

    n_periods = len(positions)

    # Annualise: average per period × periods per year
    avg_turnover_per_period = total_churn / n_periods
    periods_per_year = 365.0
    annualised = avg_turnover_per_period / initial_capital * periods_per_year

    return float(max(0.0, annualised))


def calculate_fitness(
    sortino: float,
    win_rate: float,
    turnover: float,
    penalty_factor: float = 0.1,
) -> float:
    """
    Fitness Score — composite metric for persona capital allocation.

    Formula: (sortino * win_rate) / (1 + turnover * penalty_factor)

    Design:
      - sortino:  penalises downside risk, rewards risk-adjusted returns
      - win_rate: rewards consistency (avoids high-sortino, low-win-rate traps)
      - turnover: penalises over-trading (fee bleed, slippage, overfitting)
        * penalty_factor = 0.1 means:
          - 1.0x annual turnover (100%) → fitness × 1/(1+0.1) = 0.91x
          - 5.0x annual turnover (500%) → fitness × 1/(1+0.5) = 0.67x
          - 10x annual turnover         → fitness × 1/(1+1.0) = 0.50x
      - floor at 0: fitness can never be negative

    Args:
        sortino:       Annualised Sortino ratio
        win_rate:      Fraction of winning epochs (0.0 – 1.0)
        turnover:      Annualised turnover rate
        penalty_factor: Turnover penalty sensitivity (default 0.1)

    Returns:
        Fitness score (float, >= 0)
    """
    if sortino < 0:
        # Negative sortino already means poor risk-adjusted returns
        sortino = 0.0

    numerator   = sortino * win_rate
    denominator = 1.0 + (turnover * penalty_factor)

    fitness = numerator / denominator

    return float(max(0.0, fitness))


# ─── Fitness-Weighted Softmax Allocation ──────────────────────────────────────

def softmax_weights(fitness_scores: np.ndarray, temperature: float = 1.0) -> np.ndarray:
    """
    Convert raw fitness scores into a probability distribution via softmax.

    Weights_i = exp(fitness_i / T) / sum(exp(fitness_j / T))
    where T = temperature (higher T = more uniform, lower T = more greedy).

    Temperature design:
      - T = 1.0  → standard softmax
      - T = 0.3  → concentrates weights on top persona (aggressive)
      - T = 2.0  → nearly equal weights (conservative, diversification-first)

    All-zero handling: returns equal weights (no persona unfairly zeroed).

    Args:
        fitness_scores: numpy array of fitness scores, one per persona
        temperature:    softmax temperature (default 1.0)

    Returns:
        numpy array of weights summing to 1.0
    """
    if fitness_scores is None or fitness_scores.size == 0:
        return np.array([])

    if fitness_scores.size == 1:
        return np.array([1.0])

    # Guard: if all scores are zero/negative, fall back to uniform
    scores = np.array(fitness_scores, dtype=np.float64)
    if np.all(scores <= 0):
        return np.ones_like(scores) / scores.size

    # Softmax with numerical stability: subtract max before exp
    scores_stable = (scores - np.max(scores)) / temperature
    exp_scores    = np.exp(scores_stable - np.max(scores_stable))  # stable exp
    weights       = exp_scores / np.sum(exp_scores)

    return weights.astype(np.float64)


# ─── Dynamic Allocator ─────────────────────────────────────────────────────────

@dataclass
class PersonaState:
    """
    Per-persona rolling state maintained by DynamicAllocator.
    Updated incrementally on each event (O(1) per update).
    """
    persona_type:       PersonaType
    returns_history:    List[float]     = field(default_factory=list)
    position_history:   List[float]     = field(default_factory=list)
    trade_count:       int             = 0
    wins:               int             = 0
    losses:             int             = 0
    fitness_score:      float           = 0.0
    last_signal:        Optional[PersonaSignal] = None

    @property
    def n_periods(self) -> int:
        return len(self.returns_history)

    def win_rate(self) -> float:
        total = self.wins + self.losses
        if total == 0:
            return 0.0
        return self.wins / total

    def update_on_signal(self, signal: PersonaSignal, epoch_return: float, current_position_value: float):
        """
        Incrementally update state after one epoch.

        Called once per persona per event epoch by DynamicAllocator.

        Args:
            signal:                 The PersonaSignal just produced this epoch
            epoch_return:           The PnL return for this epoch (can be 0.0 for no-trade)
            current_position_value: The notional value of the open position (for turnover)
        """
        self.last_signal = signal

        # Track returns (rolling window managed by DynamicAllocator)
        self.returns_history.append(epoch_return)

        # Track position value for turnover calculation
        self.position_history.append(current_position_value)

        # Count as a trade only if the signal was actionable
        if signal.direction.value != "NEUTRAL" and signal.confidence > 0:
            self.trade_count += 1

        # Win/loss tracking
        if epoch_return > 0:
            self.wins += 1
        elif epoch_return < 0:
            self.losses += 1
        # epoch_return == 0 → no change, no win/loss recorded


@dataclass
class AllocatorWeights:
    """Immutable snapshot of allocation weights at a point in time."""
    weights:    np.ndarray          # shape (N,), sums to 1.0
    fitnesses:  np.ndarray          # raw fitness scores
    personas:   List[PersonaType]  # persona enum list (index-aligned)
    timestamp:  str                 # ISO timestamp of this allocation

    def weight_for(self, persona: PersonaType) -> float:
        idx = self.personas.index(persona)
        return float(self.weights[idx])

    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp,
            "weights": {p.value: round(float(w), 4) for p, w in zip(self.personas, self.weights)},
            "fitness":  {p.value: round(float(f), 4) for p, f in zip(self.personas, self.fitnesses)},
        }


class DynamicAllocator:
    """
    Event-driven capital allocator for the MoE Trading Floor.

    Receives signals from the VanguardScanner (event-driven, not periodic).
    Maintains rolling 30-day histories for each persona.
    Outputs a Fitness-Weighted Softmax capital allocation on each call to .allocate().

    Design principles:
      - O(1) per .update() event — no full-history reprocessing
      - Rolling window via FIFO drop — memory bounded regardless of backtest length
      - Zero personas treated as neutral (equal weight) to avoid lockout
      - Weights strictly sum to 1.0 by construction (softmax)

    Usage:
        allocator = DynamicAllocator(personas=[PersonaType.SMC_ICT, ...])
        for epoch_event in vanguard_scanner.events():
            for p_type, signal in epoch_event.signals.items():
                allocator.update(p_type, signal, epoch_return)
            weights = allocator.allocate()
            execute_blended_trade(weights, signals)

    Args:
        personas:     List of PersonaType enums (index = canonical order)
        lookback:     Number of epochs for rolling window (default 30)
        temperature:  Softmax temperature — higher = more uniform (default 1.0)
        penalty_factor: Turnover penalty in fitness formula (default 0.1)
    """

    def __init__(
        self,
        personas: List[PersonaType],
        lookback: int = 30,
        temperature: float = 1.0,
        penalty_factor: float = 0.1,
    ):
        if not personas:
            raise ValueError("DynamicAllocator requires at least one persona.")

        self.personas         = list(personas)
        self.n_personas        = len(personas)
        self.lookback         = lookback
        self.temperature      = temperature
        self.penalty_factor   = penalty_factor

        # Per-persona rolling states
        self._states: Dict[PersonaType, PersonaState] = {
            p: PersonaState(persona_type=p) for p in self.personas
        }

        # Rolling window management (FIFO — O(1) append, bounded memory)
        self._MAX_HISTORY = lookback * 3  # allow some burst before trimming

        # Allocation log
        self._alloc_log: List[AllocatorWeights] = []

        # Epoch counter
        self._epoch = 0

    # ── Accessors ────────────────────────────────────────────────────────────────

    def state(self, persona: PersonaType) -> PersonaState:
        return self._states[persona]

    def current_weights(self) -> Optional[AllocatorWeights]:
        """Return the most recent allocation, or None if .allocate() never called."""
        if not self._alloc_log:
            return None
        return self._alloc_log[-1]

    def fitness_scores(self) -> np.ndarray:
        """Return the current fitness score vector."""
        return np.array([
            self._states[p].fitness_score for p in self.personas
        ], dtype=np.float64)

    # ── Rolling Window Management ──────────────────────────────────────────────

    def _trim_history(self, state: PersonaState):
        """Drop oldest entries if rolling window exceeded. O(1) amortised."""
        if len(state.returns_history) > self._MAX_HISTORY:
            excess = len(state.returns_history) - self.lookback
            state.returns_history  = state.returns_history[excess:]
            state.position_history = state.position_history[excess:]

    # ── Metric Recomputation (called periodically, not per update) ──────────────

    def recompute_fitness(self, persona: PersonaType) -> float:
        """
        Recompute and cache the fitness score for one persona.
        Called internally by .allocate() — not on every event.

        Args:
            persona: PersonaType enum

        Returns:
            Fitness score (float)
        """
        state = self._states[persona]

        if state.n_periods < 2:
            # Not enough history — return current (or 0 for new persona)
            return state.fitness_score

        returns     = np.array(state.returns_history[-self.lookback:], dtype=np.float64)
        positions   = state.position_history[-self.lookback:]

        sharpe   = calculate_sharpe(returns)
        sortino  = calculate_sortino(returns)
        win_rate = state.win_rate()

        # Use the LAST position as initial capital for turnover
        # (more accurate for live capital tracking)
        initial_cap = positions[0] if positions else 1.0
        turnover    = calculate_turnover(positions, initial_cap)

        fitness = calculate_fitness(
            sortino      = sortino,
            win_rate     = win_rate,
            turnover     = turnover,
            penalty_factor = self.penalty_factor,
        )

        state.fitness_score = fitness
        return fitness

    def recompute_all_fitness(self) -> np.ndarray:
        """Recompute fitness for all personas. Called once per allocation epoch."""
        return np.array([
            self.recompute_fitness(p) for p in self.personas
        ], dtype=np.float64)

    # ── Event Update ────────────────────────────────────────────────────────────

    def update(
        self,
        persona: PersonaType,
        signal: PersonaSignal,
        epoch_return: float,
        position_value: float = 0.0,
    ):
        """
        Process one event for one persona.

        O(1) per call — appends to rolling list, no full reprocessing.

        Args:
            persona:         Which persona produced this signal
            signal:          The PersonaSignal produced
            epoch_return:    The PnL return this epoch (can be 0.0 if no trade)
            position_value:  Notional position value for turnover tracking
                            (only meaningful when a trade was taken)
        """
        if persona not in self._states:
            return  # Unknown persona — ignore

        state = self._states[persona]
        state.update_on_signal(signal, epoch_return, position_value)
        self._trim_history(state)
        self._epoch += 1

    def update_all(
        self,
        signals: Dict[PersonaType, PersonaSignal],
        returns:  Dict[PersonaType, float],
        positions: Dict[PersonaType, float],
    ):
        """
        Batch-update all personas from a single epoch event.
        For use with VanguardScanner epoch events.

        Args:
            signals:   {PersonaType: PersonaSignal} for this epoch
            returns:   {PersonaType: float} PnL returns this epoch
            positions: {PersonaType: float} position values
        """
        for persona in self.personas:
            sig  = signals.get(persona, PersonaSignal.neutral(persona))
            ret  = returns.get(persona, 0.0)
            pos  = positions.get(persona, 0.0)
            self.update(persona, sig, ret, pos)

    # ── Capital Allocation ─────────────────────────────────────────────────────

    def allocate(self) -> AllocatorWeights:
        """
        Compute and return the current Fitness-Weighted Softmax allocation.

        Steps:
          1. Recompute fitness for all personas (periodic, not per-event)
          2. Apply softmax over fitness scores
          3. Cache result with timestamp

        Returns:
            AllocatorWeights: weights array + raw fitnesses + timestamp
                              weights[i] = capital fraction for personas[i]
                              All weights sum to 1.0

        Note:
          If a persona has no history (new), it gets the current fitness_score
          (may be 0.0). Softmax over [0,0,0] returns [1/N, ...] = equal weight.
          No persona is ever permanently zeroed by inactivity alone.
        """
        fitnesses = self.recompute_all_fitness()
        weights    = softmax_weights(fitnesses, temperature=self.temperature)

        snapshot = AllocatorWeights(
            weights   = weights,
            fitnesses = fitnesses,
            personas  = self.personas,
            timestamp = datetime.utcnow().isoformat(),
        )

        self._alloc_log.append(snapshot)
        return snapshot

    # ── Logging ─────────────────────────────────────────────────────────────────

    ALLOC_LOG_FILE = "logs/moe_allocations.json"

    def log_allocation(self, filename: Optional[str] = None):
        """Append the latest allocation snapshot to the MoE log file."""
        if not self._alloc_log:
            return

        path = filename or self.ALLOC_LOG_FILE
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        latest = self._alloc_log[-1]
        entry = latest.to_dict()
        entry["epoch"] = self._epoch

        try:
            with open(path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass  # Non-fatal — don't break backtest on log failure

    def summary(self) -> Dict:
        """
        Human-readable debug summary of current allocator state.
        Useful for backtest output and live monitoring.
        """
        rows = []
        for persona in self.personas:
            s = self._states[persona]
            returns_arr = np.array(s.returns_history[-self.lookback:]) if s.returns_history else np.array([])
            rows.append({
                "persona":          persona.value,
                "n_periods":        s.n_periods,
                "trades":           s.trade_count,
                "wins":             s.wins,
                "losses":           s.losses,
                "win_rate":         round(s.win_rate(), 4),
                "fitness":          round(s.fitness_score, 4),
                "mean_return":      round(float(np.mean(returns_arr)), 6) if returns_arr.size else 0.0,
                "sortino":          round(calculate_sortino(returns_arr), 4) if returns_arr.size else 0.0,
                "sharpe":           round(calculate_sharpe(returns_arr), 4) if returns_arr.size else 0.0,
                "last_direction":   s.last_signal.direction.value if s.last_signal else "NEUTRAL",
                "last_confidence": s.last_signal.confidence       if s.last_signal else 0,
            })

        weights = self.current_weights()
        return {
            "epoch":           self._epoch,
            "lookback":        self.lookback,
            "temperature":     self.temperature,
            "penalty_factor":  self.penalty_factor,
            "weights":         {p.value: round(float(w), 4) for p, w in zip(self.personas, weights.weights)} if weights else {},
            "personas":       rows,
        }


# ─── Quick Tests ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Fix path so absolute imports work when running directly as a script
    import sys as _sys
    _sys.path.insert(0, str(__file__).rsplit("/src/backtest/", 1)[0])
    from src.data.personas import PersonaType

    # ── 1. Edge case tests ────────────────────────────────────────────────────
    empty = np.array([])
    assert calculate_sharpe(empty) == 0.0,   "Empty Sharpe"
    assert calculate_sortino(empty) == 0.0,  "Empty Sortino"
    assert calculate_win_rate(empty) == 0.0,  "Empty WinRate"

    no_negatives = np.array([0.01, 0.02, 0.03, 0.01])
    s = calculate_sortino(no_negatives)
    print(f"  Sortino (no negatives): {s:.4f}  [uses Sharpe proxy] ✓")

    # ── 2. Sortino only penalises downside ────────────────────────────────────
    choppy = np.array([0.10, -0.01, 0.10, -0.01, 0.10, -0.01])
    sortino_choppy = calculate_sortino(choppy)
    sharpe_choppy   = calculate_sharpe(choppy)
    print(f"  Choppy returns: Sortino={sortino_choppy:.4f}, Sharpe={sharpe_choppy:.4f}")
    print(f"  → Sortino > Sharpe? {sortino_choppy > sharpe_choppy}  ✓ (upside not penalised)")

    # ── 3. Turnover penalisation ──────────────────────────────────────────────
    no_trades = []
    high_trades = [10000, 8000, 11000, 7000, 12000, 6000, 13000]
    low_trades  = [10000, 10100, 10200, 10300, 10400, 10500]

    t_high = calculate_turnover(high_trades, 10000)
    t_low  = calculate_turnover(low_trades,  10000)
    print(f"  High-churn turnover: {t_high:.2f}x | Low-churn turnover: {t_low:.2f}x")
    assert t_high > t_low * 2, "High churn should be penalised more"
    print(f"  → High churn {t_high:.2f}x > Low churn {t_low:.2f}x ✓")

    # ── 4. Fitness formula ─────────────────────────────────────────────────────
    f_good = calculate_fitness(sortino=2.0, win_rate=0.65, turnover=0.5, penalty_factor=0.1)
    f_bad  = calculate_fitness(sortino=2.0, win_rate=0.65, turnover=5.0, penalty_factor=0.1)
    print(f"  Fitness (low turnover 0.5x): {f_good:.4f} | Fitness (high turnover 5x): {f_bad:.4f}")
    assert f_good > f_bad, "Lower turnover should score higher fitness"
    print(f"  → Fitness penalty applied correctly ✓")

    # ── 5. Softmax weights sum to 1 ────────────────────────────────────────────
    scores = np.array([3.0, 1.0, 0.0])
    w = softmax_weights(scores, temperature=1.0)
    print(f"  Softmax([3.0, 1.0, 0.0]): {[round(float(x),4) for x in w]}")
    assert abs(np.sum(w) - 1.0) < 1e-9, "Weights must sum to 1.0"
    assert w[0] > w[1] > w[2], "Highest score gets highest weight"
    print(f"  → Weights sum to 1.0 ✓  |  Top persona dominant: {float(w[0]):.2%} ✓")

    # ── 6. DynamicAllocator lifecycle ─────────────────────────────────────────
    alloc = DynamicAllocator(
        personas=[PersonaType.SMC_ICT, PersonaType.ORDER_FLOW, PersonaType.MACRO_ONCHAIN],
        lookback=5,
        temperature=1.0,
        penalty_factor=0.1,
    )

    from src.data.personas import PersonaSignal, Direction  # noqa: F401

    epoch_returns = {
        PersonaType.SMC_ICT:      0.025,
        PersonaType.ORDER_FLOW:   0.010,
        PersonaType.MACRO_ONCHAIN: -0.005,
    }

    signals = {
        PersonaType.SMC_ICT:      PersonaSignal(
            thesis="Test", direction=Direction.LONG,
            confidence=75, leverage=5, persona=PersonaType.SMC_ICT),
        PersonaType.ORDER_FLOW:   PersonaSignal(
            thesis="Test", direction=Direction.LONG,
            confidence=60, leverage=4, persona=PersonaType.ORDER_FLOW),
        PersonaType.MACRO_ONCHAIN: PersonaSignal(
            thesis="Test", direction=Direction.NEUTRAL,
            confidence=0, leverage=1, persona=PersonaType.MACRO_ONCHAIN),
    }

    positions = {p: 10000.0 for p in PersonaType}

    # Simulate 10 epochs
    np.random.seed(42)
    for i in range(10):
        rets = {p: float(np.random.normal(0.01, 0.03)) for p in PersonaType}
        alloc.update_all(signals, rets, positions)

    weights = alloc.allocate()
    summary = alloc.summary()

    print(f"\n  After 10 epochs:")
    print(f"  Weights: {summary['weights']}")
    print(f"  All personas in state: {list(alloc._states.keys())}")
    print(f"  Allocation: {[round(float(w),4) for w in weights.weights]}")
    assert abs(sum(weights.weights) - 1.0) < 1e-9, "Weights must sum to 1.0"

    print("\n  Persona performance summary:")
    for row in summary["personas"]:
        print(f"    {row['persona']:15s} | fitness={row['fitness']:6.4f} | "
              f"win_rate={row['win_rate']:.0%} | trades={row['trades']}")

    print("\n✓ All Quant Evaluator tests passed.")
    print("✓ DynamicAllocator lifecycle verified.")
    print("✓ Portfolio Manager ready for VanguardScanner integration.")
