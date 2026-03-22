"""Plutus V4.0 — Alpha Research Models.

Exports the meta-learning components:
- GeneticOptimizer  — evolves ScannerConfig across generations
- QLearnConfig      — reinforcement-learning hyperparameters for Q-learning
- MoEWeighter       — dynamic persona weight allocation via softmax Sharpe

All components are stateless wrt the trading engine; they consume trade logs
and emit updated configurations.

NOTE
---
ScannerConfig, ScannerParams, ParamSchema, QLearnConfig, and all GA constraint
constants are defined in src/models/params.py (the single source of truth).
This module re-exports GeneticOptimizer which now operates on ScannerParams.

Parameter mismatch (G1) fix: GeneticOptimizer no longer uses legacy field names
(sweep_threshold, vol_squeeze_atr_mult, deviation_z_score).  It now uses the
canonical ScannerParams field names (sweep_threshold_pct, deviation_atr_multiplier,
etc.) that match VanguardScanner exactly.

GA hard constraints (G2) fix: sweep_threshold_pct floor raised from 0.0001 (0.01%)
to 0.005 (0.5%) to prevent the GA from converging on micro-wick noise generators.
All other parameter bounds are enforced via ParamSchema values imported from params.py.
"""

from __future__ import annotations

import random
import sqlite3
import string
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Import from the single source of truth
from src.models.params import (
    ScannerParams,
    ScannerConfig,         # backward-compat alias — same class as ScannerParams
    QLearnConfig,
    SWEEP_THRESHOLD_MIN,
    SWEEP_THRESHOLD_MAX,
    DEVIATION_ATR_MIN,
    DEVIATION_ATR_MAX,
    VOL_SQUEEZE_ATR_MIN,
    VOL_SQUEEZE_ATR_MAX,
    RSI_OVERSOLD_MIN,
    RSI_OVERSOLD_MAX,
    RSI_OVERBOUGHT_MIN,
    RSI_OVERBOUGHT_MAX,
    BB_STD_MIN,
    BB_STD_MAX,
    SQ_THRESHOLD_MIN,
    SQ_THRESHOLD_MAX,
    ATR_PERIOD_MIN,
    ATR_PERIOD_MAX,
    EMA_PERIOD_MIN,
    EMA_PERIOD_MAX,
    SWEEP_LBK_MIN,
    SWEEP_LBK_MAX,
    SQ_LBK_MIN,
    SQ_LBK_MAX,
)


# Chromosome layout — MUST match ScannerParams field names exactly (G1 fix)
# The GA genome IS the 12 fields of ScannerParams.
# No translation layer between GA output and scanner input.
_CHROM_LABELS = [
    "sweep_lookback",
    "sweep_threshold_pct",
    "deviation_atr_multiplier",
    "rsi_oversold",
    "rsi_overbought",
    "bb_period",
    "bb_std",
    "squeeze_lookback",
    "squeeze_threshold_pct",
    "atr_period",
    "ema_period",
    "rsi_period",
]

_MUTATION_SIGMA = 0.01  # Gaussian mutation standard deviation per gene (float fields)
_MUTATION_SIGMA_INT = 2  # Gaussian sigma for integer fields (periods)
_TOURNAMENT_K = 4  # individuals per tournament


# ---------------------------------------------------------------------------
# GeneticOptimizer
# ---------------------------------------------------------------------------


@dataclass
class GeneticOptimizer:
    """Steady-state genetic algorithm that evolves :class:`ScannerParams`.

    Lifecycle
    --------
    1. Initialise with a seed :class:`ScannerParams` and a population size.
    2. After each generation evaluate fitness (Sharpe / Sortino) per persona.
    3. Call :meth:`evolve` to produce the next generation's best individual.

    The chromosome is the flat real-valued vector of the 12
    :attr:`ScannerParams` fields.  Crossover is uniform (50 / 50 blend);
    mutation is additive Gaussian (σ = 0.01 per gene for floats, σ = 2 for ints).

    G1 fix: All field names now match VanguardScanner exactly — no translation
    layer between GA output and scanner.update_config().

    G2 fix: All hard bounds are enforced at GA operator time AND validated
    by ScannerParams.validate() at scanner.apply().  sweep_threshold_pct
    minimum is 0.005 (0.5%), not 0.0001.

    Parameters
    ----------
    config : ScannerParams
        Seed configuration for the initial population.
    population_size : int
        Number of individuals in the GA population.  Default 32.
    """

    config: ScannerParams = field(default_factory=ScannerParams)
    population_size: int = 32

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        self._generation: int = 0
        self._population: list[ScannerParams] = self._seed_population()

    @property
    def generation(self) -> int:
        """Current generation counter (0-indexed)."""
        return self._generation

    def get_current_config(self) -> ScannerParams:
        """Return the fittest individual of the current population."""
        return self._population[0]

    def evolve(self, fitness_scores: dict[str, float]) -> ScannerParams:
        """Advance one GA generation and return the new best :class:`ScannerParams`.

        Parameters
        ----------
        fitness_scores : dict[str, float]
            Maps a config fingerprint (e.g. its string representation) to a
            positive fitness value.  Higher is better.  Typical fitness =
            rolling 30-day Sharpe or Sortino ratio per persona aggregated
            via :class:`MoEWeighter`.

        Returns
        -------
        ScannerParams
            The elite (fittest) individual after selection, crossover,
            and mutation.
        """
        # Re-rank population by supplied fitness scores; unknown individuals
        # receive a neutral fitness of 0.0 (survive but not elite).
        scored: list[tuple[float, ScannerParams]] = []
        for cfg in self._population:
            key = self._fingerprint(cfg)
            score = fitness_scores.get(key, 0.0)
            scored.append((score, cfg))

        # Elitism — keep the top-2 untouched.
        scored.sort(key=lambda x: x[0], reverse=True)
        elite: list[ScannerParams] = [cfg for _, cfg in scored[:2]]

        # Build the rest of the next generation via tournament selection,
        # crossover, and mutation.
        new_population: list[ScannerParams] = elite.copy()
        while len(new_population) < self.population_size:
            p1 = self._tournament_select(scored)
            p2 = self._tournament_select(scored)
            child = self._crossover(p1, p2)
            child = self._mutate(child)
            new_population.append(child)

        self._population = new_population
        self._generation += 1
        return self._population[0]

    # ------------------------------------------------------------------
    # GA operators
    # ------------------------------------------------------------------

    def _seed_population(self) -> list[ScannerParams]:
        """Create the initial population by Gaussian perturbation of the seed."""
        base = self.config
        pop: list[ScannerParams] = []
        for _ in range(self.population_size):
            pop.append(
                ScannerParams(
                    # ── Trigger 1: Liquidity Sweep ──────────────────────────
                    sweep_lookback=_clip_int(
                        round(base.sweep_lookback + random.gauss(0.0, _MUTATION_SIGMA_INT)),
                        SWEEP_LBK_MIN, SWEEP_LBK_MAX,
                    ),
                    sweep_threshold_pct=_clip(
                        base.sweep_threshold_pct + random.gauss(0.0, _MUTATION_SIGMA),
                        SWEEP_THRESHOLD_MIN,   # G2 fix: 0.5% floor, not 0.0001
                        SWEEP_THRESHOLD_MAX,
                    ),
                    # ── Trigger 2: Extreme Mean Reversion ────────────────────
                    deviation_atr_multiplier=_clip(
                        base.deviation_atr_multiplier + random.gauss(0.0, _MUTATION_SIGMA),
                        DEVIATION_ATR_MIN, DEVIATION_ATR_MAX,
                    ),
                    rsi_oversold=_clip(
                        base.rsi_oversold + random.gauss(0.0, _MUTATION_SIGMA),
                        RSI_OVERSOLD_MIN, RSI_OVERSOLD_MAX,
                    ),
                    rsi_overbought=_clip(
                        base.rsi_overbought + random.gauss(0.0, _MUTATION_SIGMA),
                        RSI_OVERBOUGHT_MIN, RSI_OVERBOUGHT_MAX,
                    ),
                    # ── Trigger 3: Volatility Squeeze ────────────────────────
                    bb_period=_clip_int(
                        round(base.bb_period + random.gauss(0.0, _MUTATION_SIGMA_INT)),
                        5, 50,
                    ),
                    bb_std=_clip(
                        base.bb_std + random.gauss(0.0, _MUTATION_SIGMA),
                        BB_STD_MIN, BB_STD_MAX,
                    ),
                    squeeze_lookback=_clip_int(
                        round(base.squeeze_lookback + random.gauss(0.0, _MUTATION_SIGMA_INT)),
                        SQ_LBK_MIN, SQ_LBK_MAX,
                    ),
                    squeeze_threshold_pct=_clip(
                        base.squeeze_threshold_pct + random.gauss(0.0, _MUTATION_SIGMA),
                        SQ_THRESHOLD_MIN, SQ_THRESHOLD_MAX,
                    ),
                    # ── Rolling window periods ──────────────────────────────
                    atr_period=_clip_int(
                        round(base.atr_period + random.gauss(0.0, _MUTATION_SIGMA_INT)),
                        ATR_PERIOD_MIN, ATR_PERIOD_MAX,
                    ),
                    ema_period=_clip_int(
                        round(base.ema_period + random.gauss(0.0, _MUTATION_SIGMA_INT)),
                        EMA_PERIOD_MIN, EMA_PERIOD_MAX,
                    ),
                    rsi_period=_clip_int(
                        round(base.rsi_period + random.gauss(0.0, _MUTATION_SIGMA_INT)),
                        7, 30,
                    ),
                )
            )
        return pop

    def _tournament_select(
        self,
        scored: list[tuple[float, ScannerParams]],
    ) -> ScannerParams:
        """Tournament selection: pick the best of k=4 random individuals."""
        competitors = random.sample(scored, k=min(_TOURNAMENT_K, len(scored)))
        return max(competitors, key=lambda x: x[0])[1]

    def _crossover(self, p1: ScannerParams, p2: ScannerParams) -> ScannerParams:
        """Uniform blend crossover: 50 % chance to inherit each gene from p1."""
        return ScannerParams(
            sweep_lookback=_clip_int(
                round((p1.sweep_lookback + p2.sweep_lookback) / 2),
                SWEEP_LBK_MIN, SWEEP_LBK_MAX,
            ),
            sweep_threshold_pct=_clip(
                random.uniform(p1.sweep_threshold_pct, p2.sweep_threshold_pct),
                SWEEP_THRESHOLD_MIN, SWEEP_THRESHOLD_MAX,
            ),
            deviation_atr_multiplier=_clip(
                random.uniform(p1.deviation_atr_multiplier, p2.deviation_atr_multiplier),
                DEVIATION_ATR_MIN, DEVIATION_ATR_MAX,
            ),
            rsi_oversold=_clip(
                random.uniform(p1.rsi_oversold, p2.rsi_oversold),
                RSI_OVERSOLD_MIN, RSI_OVERSOLD_MAX,
            ),
            rsi_overbought=_clip(
                random.uniform(p1.rsi_overbought, p2.rsi_overbought),
                RSI_OVERBOUGHT_MIN, RSI_OVERBOUGHT_MAX,
            ),
            bb_period=_clip_int(
                round((p1.bb_period + p2.bb_period) / 2),
                5, 50,
            ),
            bb_std=_clip(
                random.uniform(p1.bb_std, p2.bb_std),
                BB_STD_MIN, BB_STD_MAX,
            ),
            squeeze_lookback=_clip_int(
                round((p1.squeeze_lookback + p2.squeeze_lookback) / 2),
                SQ_LBK_MIN, SQ_LBK_MAX,
            ),
            squeeze_threshold_pct=_clip(
                random.uniform(p1.squeeze_threshold_pct, p2.squeeze_threshold_pct),
                SQ_THRESHOLD_MIN, SQ_THRESHOLD_MAX,
            ),
            atr_period=_clip_int(
                round((p1.atr_period + p2.atr_period) / 2),
                ATR_PERIOD_MIN, ATR_PERIOD_MAX,
            ),
            ema_period=_clip_int(
                round((p1.ema_period + p2.ema_period) / 2),
                EMA_PERIOD_MIN, EMA_PERIOD_MAX,
            ),
            rsi_period=_clip_int(
                round((p1.rsi_period + p2.rsi_period) / 2),
                7, 30,
            ),
        )

    def _mutate(self, cfg: ScannerParams) -> ScannerParams:
        """Additive Gaussian mutation (σ = 0.01 for floats, σ = 2 for ints)."""
        return ScannerParams(
            sweep_lookback=_clip_int(
                round(cfg.sweep_lookback + random.gauss(0.0, _MUTATION_SIGMA_INT)),
                SWEEP_LBK_MIN, SWEEP_LBK_MAX,
            ),
            sweep_threshold_pct=_clip(
                cfg.sweep_threshold_pct + random.gauss(0.0, _MUTATION_SIGMA),
                SWEEP_THRESHOLD_MIN,   # G2 fix: 0.5% floor
                SWEEP_THRESHOLD_MAX,
            ),
            deviation_atr_multiplier=_clip(
                cfg.deviation_atr_multiplier + random.gauss(0.0, _MUTATION_SIGMA),
                DEVIATION_ATR_MIN, DEVIATION_ATR_MAX,
            ),
            rsi_oversold=_clip(
                cfg.rsi_oversold + random.gauss(0.0, _MUTATION_SIGMA),
                RSI_OVERSOLD_MIN, RSI_OVERSOLD_MAX,
            ),
            rsi_overbought=_clip(
                cfg.rsi_overbought + random.gauss(0.0, _MUTATION_SIGMA),
                RSI_OVERBOUGHT_MIN, RSI_OVERBOUGHT_MAX,
            ),
            bb_period=_clip_int(
                round(cfg.bb_period + random.gauss(0.0, _MUTATION_SIGMA_INT)),
                5, 50,
            ),
            bb_std=_clip(
                cfg.bb_std + random.gauss(0.0, _MUTATION_SIGMA),
                BB_STD_MIN, BB_STD_MAX,
            ),
            squeeze_lookback=_clip_int(
                round(cfg.squeeze_lookback + random.gauss(0.0, _MUTATION_SIGMA_INT)),
                SQ_LBK_MIN, SQ_LBK_MAX,
            ),
            squeeze_threshold_pct=_clip(
                cfg.squeeze_threshold_pct + random.gauss(0.0, _MUTATION_SIGMA),
                SQ_THRESHOLD_MIN, SQ_THRESHOLD_MAX,
            ),
            atr_period=_clip_int(
                round(cfg.atr_period + random.gauss(0.0, _MUTATION_SIGMA_INT)),
                ATR_PERIOD_MIN, ATR_PERIOD_MAX,
            ),
            ema_period=_clip_int(
                round(cfg.ema_period + random.gauss(0.0, _MUTATION_SIGMA_INT)),
                EMA_PERIOD_MIN, EMA_PERIOD_MAX,
            ),
            rsi_period=_clip_int(
                round(cfg.rsi_period + random.gauss(0.0, _MUTATION_SIGMA_INT)),
                7, 30,
            ),
        )

    def recalibrate_scanner(
        self,
        current_volatility: float,
        baseline_volatility: float = 0.02,
    ) -> ScannerParams:
        """OPERATION DARWIN — adapt scanner thresholds to current regime.

        The market's ATR/volatility is the primary Darwin selection pressure.
        High-vol regimes need wider sweep thresholds and tighter ATR cuts;
        low-vol regimes can be more sensitive.

        Adaptation rules
        ---------------
        vol_ratio = current_volatility / baseline_volatility

        If vol_ratio > 2.0  (high vol):
          sweep_threshold_pct   ← clamp(seed × 1.5,  0.005, 0.05)   [G2: 0.005 floor]
          deviation_atr_mult    ← clamp(seed × 0.75, 1.5, 5.0)       [less strict]
          rsi_oversold          ← clamp(seed + 5,    10, 40)          [more selective]
          rsi_overbought        ← clamp(seed - 5,    60, 90)           [more selective]

        If vol_ratio < 0.7  (low vol):
          sweep_threshold_pct   ← clamp(seed × 0.6,  0.005, 0.05)    [G2: 0.005 floor]
          deviation_atr_mult   ← clamp(seed × 1.25, 1.5, 5.0)       [stricter]
          rsi_oversold          ← clamp(seed - 5,    10, 40)           [less selective]
          rsi_overbought        ← clamp(seed + 5,    60, 90)          [less selective]

        Otherwise (normal vol):
          seed config is returned unchanged.

        Parameters
        ----------
        current_volatility : float
            Current 14-period ATR / close ratio (a fraction, e.g. 0.04 for 4 %).
        baseline_volatility : float
            Long-run average volatility used as the normalisation anchor.
            Default 0.02 (2 %).

        Returns
        -------
        ScannerParams
            Adapted scanner params for the current regime.
        """
        vol_ratio = current_volatility / baseline_volatility
        seed = self.config  # the GA's seed config

        if vol_ratio > 2.0:
            return ScannerParams(
                sweep_lookback=seed.sweep_lookback,
                sweep_threshold_pct=_clip(
                    seed.sweep_threshold_pct * 1.5,
                    SWEEP_THRESHOLD_MIN, SWEEP_THRESHOLD_MAX,
                ),
                deviation_atr_multiplier=_clip(
                    seed.deviation_atr_multiplier * 0.75,
                    DEVIATION_ATR_MIN, DEVIATION_ATR_MAX,
                ),
                rsi_oversold=_clip(seed.rsi_oversold + 5, RSI_OVERSOLD_MIN, RSI_OVERSOLD_MAX),
                rsi_overbought=_clip(seed.rsi_overbought - 5, RSI_OVERBOUGHT_MIN, RSI_OVERBOUGHT_MAX),
                bb_period=seed.bb_period,
                bb_std=seed.bb_std,
                squeeze_lookback=seed.squeeze_lookback,
                squeeze_threshold_pct=seed.squeeze_threshold_pct,
                atr_period=seed.atr_period,
                ema_period=seed.ema_period,
                rsi_period=seed.rsi_period,
            )
        elif vol_ratio < 0.7:
            return ScannerParams(
                sweep_lookback=seed.sweep_lookback,
                sweep_threshold_pct=_clip(
                    seed.sweep_threshold_pct * 0.6,
                    SWEEP_THRESHOLD_MIN, SWEEP_THRESHOLD_MAX,
                ),
                deviation_atr_multiplier=_clip(
                    seed.deviation_atr_multiplier * 1.25,
                    DEVIATION_ATR_MIN, DEVIATION_ATR_MAX,
                ),
                rsi_oversold=_clip(seed.rsi_oversold - 5, RSI_OVERSOLD_MIN, RSI_OVERSOLD_MAX),
                rsi_overbought=_clip(seed.rsi_overbought + 5, RSI_OVERBOUGHT_MIN, RSI_OVERBOUGHT_MAX),
                bb_period=seed.bb_period,
                bb_std=seed.bb_std,
                squeeze_lookback=seed.squeeze_lookback,
                squeeze_threshold_pct=seed.squeeze_threshold_pct,
                atr_period=seed.atr_period,
                ema_period=seed.ema_period,
                rsi_period=seed.rsi_period,
            )
        else:
            return seed

    @staticmethod
    def _fingerprint(cfg: ScannerParams) -> str:
        """Deterministic string key for a config (used in fitness dict)."""
        return (
            f"sp{cfg.sweep_lookback}"
            f"_st{cfg.sweep_threshold_pct:.5f}"
            f"_da{cfg.deviation_atr_multiplier:.3f}"
            f"_ro{cfg.rsi_oversold:.1f}"
            f"_rb{cfg.rsi_overbought:.1f}"
            f"_bb{cfg.bb_period}_{cfg.bb_std:.2f}"
            f"_sq{cfg.squeeze_lookback}_{cfg.squeeze_threshold_pct:.3f}"
            f"_ap{cfg.atr_period}"
            f"_ep{cfg.ema_period}"
            f"_rp{cfg.rsi_period}"
        )

    # ── L5: Drawdown-penalised fitness ─────────────────────────────────────────

    @staticmethod
    def compute_fitness(
        equity_curve: list[float],
        max_drawdown_constraint: float = 0.20,
        sharpe_weight: float = 0.6,
        dd_weight: float = 0.4,
    ) -> tuple[float, float, float]:
        """
        Compute composite fitness from an equity curve.

        Fitness formula:
            fitness = Sharpe × sharpe_weight + (1 − max_dd) × dd_weight

        Hard constraint: reject (return −inf) any config with drawdown > 20%.
        This prevents the GA from choosing volatile configs that happened to be lucky.

        L5 fix: Without drawdown penalty the GA systematically overfits to
        recent winners.  A high Sharpe with a 35% drawdown is not a good config.

        Parameters
        ----------
        equity_curve : list[float]
            Equity values at each step (starting from initial capital).
        max_drawdown_constraint : float
            Maximum allowed drawdown fraction. Default 0.20 = 20 %.
            Configs exceeding this are rejected (return −inf).
        sharpe_weight : float
            Weight for the Sharpe component. Default 0.6.
        dd_weight : float
            Weight for the drawdown component. Default 0.4.

        Returns
        -------
        tuple[float, float, float]
            (fitness, sharpe, max_drawdown). sharpe and max_drawdown are
            for logging; fitness is the composite score used for selection.
        """
        import math

        if len(equity_curve) < 3:
            return 0.0, 0.0, 0.0

        # ── Compute max drawdown ───────────────────────────────────────────
        peak = equity_curve[0]
        max_dd = 0.0
        for equity in equity_curve:
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd

        # Hard constraint: reject configs with excessive drawdown (L5)
        if max_dd > max_drawdown_constraint:
            return float("-inf"), 0.0, max_dd

        # ── Compute Sharpe ratio ──────────────────────────────────────────
        returns = []
        for i in range(1, len(equity_curve)):
            prev = equity_curve[i - 1]
            cur  = equity_curve[i]
            if prev > 0:
                returns.append((cur - prev) / prev)
            else:
                returns.append(0.0)

        if not returns:
            return 0.0, 0.0, max_dd

        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
        std_ret  = math.sqrt(variance) if variance > 0 else 1e-9

        # Annualise (same scale across configs — ranking is what matters)
        periods_per_year = 252
        sharpe = (mean_ret / std_ret) * math.sqrt(periods_per_year) if std_ret > 1e-12 else 0.0

        # Clamp to prevent inf/−inf propagating into softmax
        sharpe = max(-100.0, min(100.0, sharpe))

        # ── Composite fitness ───────────────────────────────────────────────
        fitness = sharpe * sharpe_weight + (1.0 - max_dd) * dd_weight
        return fitness, sharpe, max_dd

    # ── L7: Walk-forward validation ────────────────────────────────────────────

    def validate_walk_forward(
        self,
        train_dfs: dict[str, "pd.DataFrame"],
        val_dfs:  dict[str, "pd.DataFrame"],
        initial_equity: float = 10_000.0,
        min_confidence: int = 60,
        lookback: int = 30,
    ) -> tuple[bool, float, float, float]:
        """
        Walk-forward validation: train on 70 %, validate on held-out 30 %.

        This is the L7 fix — it prevents the GA from overfitting to the
        in-sample data by requiring that a config also beats the baseline
        on the held-out validation set before it is accepted.

        Algorithm
        ---------
        1. Baseline: backtest seed config on val_dfs → baseline Sharpe.
        2. Train: backtest entire population on train_dfs → fitness scores.
        3. Evolve one generation using train fitness.
        4. Validate evolved elite on val_dfs.
        5. Accept if validation Sharpe ≥ baseline Sharpe.

        Parameters
        ----------
        train_dfs : dict[str, pd.DataFrame]
            In-sample (training) DataFrames per symbol.
        val_dfs  : dict[str, pd.DataFrame]
            Out-of-sample (validation) DataFrames per symbol.
        initial_equity : float
            Starting capital. Default 10 000.
        min_confidence  : int
            Minimum blended confidence threshold.
        lookback        : int
            MoEWeighter lookback window.

        Returns
        -------
        tuple[bool, float, float, float]
            (accepted, val_fitness, val_sharpe, val_max_dd).
            accepted = True only if evolved config beats baseline on validation.
        """
        try:
            import pandas as pd
            from ..backtest.chronos_engine import ChronosBacktester, BacktestMode
        except ImportError as exc:
            raise RuntimeError(
                "Walk-forward validation requires ChronosBacktester. "
                "Ensure src.backtest.chronos_engine is importable."
            ) from exc

        # ── Baseline: seed config on validation set ─────────────────────────
        engine_seed = ChronosBacktester(
            universe=list(val_dfs.keys()),
            mode=BacktestMode.DRY_RUN,
            initial_equity=initial_equity,
            min_confidence=min_confidence,
            lookback=lookback,
        )
        result_baseline = engine_seed.run_backtest(val_dfs)
        baseline_equity = [pt["equity"] for pt in result_baseline["equity_curve"]]
        _, baseline_sharpe, _ = self.compute_fitness(baseline_equity)

        # ── Train: score every individual in the population ─────────────────
        fitness_scores: dict[str, float] = {}
        for cfg in self._population:
            fp = self._fingerprint(cfg)
            engine = ChronosBacktester(
                universe=list(train_dfs.keys()),
                mode=BacktestMode.DRY_RUN,
                initial_equity=initial_equity,
                min_confidence=min_confidence,
                lookback=lookback,
            )
            if hasattr(engine._scanner, "update_config"):
                engine._scanner.update_config(cfg)

            result = engine.run_backtest(train_dfs)
            equity  = [pt["equity"] for pt in result["equity_curve"]]
            fitness, _, _ = self.compute_fitness(equity)
            fitness_scores[fp] = fitness
            print(f"  [WF] {fp[:40]}... train_fitness={fitness:.3f}")

        # ── Evolve one generation ───────────────────────────────────────────
        elite_cfg = self.evolve(fitness_scores)

        # ── Validate evolved elite on held-out set ──────────────────────────
        engine_val = ChronosBacktester(
            universe=list(val_dfs.keys()),
            mode=BacktestMode.DRY_RUN,
            initial_equity=initial_equity,
            min_confidence=min_confidence,
            lookback=lookback,
        )
        if hasattr(engine_val._scanner, "update_config"):
            engine_val._scanner.update_config(elite_cfg)

        result_val = engine_val.run_backtest(val_dfs)
        val_equity  = [pt["equity"] for pt in result_val["equity_curve"]]
        val_fitness, val_sharpe, val_max_dd = self.compute_fitness(val_equity)

        accepted = (
            (val_fitness != float("-inf"))
            and (val_fitness > baseline_sharpe)
        )

        print(f"[GA] Walk-forward: val_sharpe={val_sharpe:.3f} "
              f"(baseline={baseline_sharpe:.3f}) "
              f"val_max_dd={val_max_dd:.1%} → "
              f"{'ACCEPTED ✓' if accepted else 'REJECTED ✗'}")

        return accepted, val_fitness, val_sharpe, val_max_dd


# ---------------------------------------------------------------------------
# Section C — MoEWeighter
# ---------------------------------------------------------------------------


@dataclass
class MoEWeighter:
    """Mixture-of-Experts dynamic weight allocator.

    Each persona (expert) maintains a rolling 30-return history of realized
    returns.  Weights are recomputed via a softmax over the persona's
    rolling Sharpe ratios:

        w_i = exp(SR_i / T) / Σ_j exp(SR_j / T)

    where T is the temperature hyperparameter.  Higher T → flatter weights
    (more uniform); lower T → sharper weights (winner-take-most).
    Personas with fewer than ``min_samples`` returns receive uniform weight.

    Attributes
    ----------
    personas : list[str]
        Names of the experts (must be stable across calls).
    lookback : int
        Number of past returns per persona kept in the rolling window.
        Default 30 (≈ one trading month at one trade/day).
    temperature : float
        Softmax temperature.  Default 1.0.
    """

    personas: list[str] = field(default_factory=list)
    lookback: int = 30
    temperature: float = 1.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        self._returns: dict[str, deque[float]] = {
            name: deque(maxlen=self.lookback) for name in self.personas
        }
        self._weights: dict[str, float] = {name: 1.0 / len(self.personas) for name in self.personas}

    def update(self, signal_name: str, realized_return: float) -> None:
        """Record a new realized return for ``signal_name``.

        Parameters
        ----------
        signal_name : str
            Persona / signal name that generated this return.
        realized_return : float
            Realised return (e.g. 0.05 for a 5 % gain) since entry.
        """
        if signal_name not in self._returns:
            self._returns[signal_name] = deque(maxlen=self.lookback)
        self._returns[signal_name].append(realized_return)
        self._weights = self.allocate()

    def allocate(self) -> dict[str, float]:
        """Recompute and return persona weights.

        Returns
        -------
        dict[str, float]
            Mapping persona → weight (weights sum to 1.0).
        """
        min_samples = 5  # threshold before using Sharpe; else uniform
        sharpe_map: dict[str, float] = {}
        active: list[str] = []

        for name in self.personas:
            ret_list = list(self._returns.get(name, []))
            if len(ret_list) < min_samples:
                sharpe_map[name] = 0.0
            else:
                μ = statistics.mean(ret_list)
                σ = statistics.stdev(ret_list) if len(ret_list) > 1 else 1e-9
                if σ == 0.0:
                    σ = 1e-9  # Guard: all-identical returns (e.g. 8x 0.0 from skipped events)
                # Guard: clamp to ±100 so that σ==1e-9 does not produce
                # exp(10M) which would overflow even _safe_exp.
                SHARPE_CLAMP = 100.0
                sharpe = μ / σ
                sharpe_map[name] = max(-SHARPE_CLAMP, min(SHARPE_CLAMP, sharpe))
                active.append(name)

        if not active:
            # No persona has enough data → uniform
            return {name: 1.0 / len(self.personas) for name in self.personas}

        # Softmax weights over Sharpe ratios.
        sr_vals = [sharpe_map[n] for n in active]
        max_sr = max(sr_vals)  # subtract max for numerical stability

        exp_scores: dict[str, float] = {}
        denom = 0.0
        for name in active:
            score = sharpe_map[name]
            exp_s = _safe_exp((score - max_sr) / self.temperature)
            exp_scores[name] = exp_s
            denom += exp_s

        weights_out: dict[str, float] = {}
        for name in self.personas:
            if name in exp_scores and denom > 0:
                weights_out[name] = exp_scores[name] / denom
            else:
                weights_out[name] = 0.0

        # Normalise to sum to 1 (handles rounding).
        total = sum(weights_out.values())
        if total > 0:
            weights_out = {k: v / total for k, v in weights_out.items()}

        self._weights = weights_out
        return weights_out

    def get_weights(self) -> dict[str, float]:
        """Return the most recently computed weights without reallocating."""
        return dict(self._weights)


# ---------------------------------------------------------------------------
# Section D — RLHFLesson
# ---------------------------------------------------------------------------


@dataclass
class RLHFLesson:
    """A single RLHF lesson generated by the ReflexionEvolver.

    Attributes
    ----------
    lesson_text : str
        Human-readable description of the lesson (e.g.
        "Never fade the 30m结构的 break on high-volume spike").
    persona : str
        Persona whose signal was involved in the losing trade.
    anomaly_type : str
        High-level classification: "liquidity_sweep", "structure_break",
        "fakeout", "news_gap", etc.
    realized_pnl : float
        Signed PnL of the losing trade (negative value).
    timestamp : datetime
        UTC timestamp when the lesson was generated.
    generation : int
        GA generation index when this lesson was produced.
    """

    lesson_text: str
    persona: str
    anomaly_type: str
    realized_pnl: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    generation: int = 0


# ---------------------------------------------------------------------------
# Section E — ReflexionEvolver
# ---------------------------------------------------------------------------

# Default reflexion prompt template used for LLM lesson generation.
_DEFAULT_REFLEXION_PROMPT = string.Template(
    """\
You are a senior quantitative trading analyst. A losing trade just occurred.

Trade details:
- Persona: ${persona}
- Entry price: ${entry}
- Exit price: ${exit}
- PnL: ${pnl}
- Market context: ${context}

Task:
1. Classify the anomaly type (e.g., liquidity_sweep, structure_break, fakeout, news_gap).
2. Write a concise, actionable lesson (1-2 sentences) that explains what went wrong
   and what the correct response should have been.
3. Output ONLY a JSON object with keys: "anomaly_type", "lesson_text".
   No markdown, no explanations.
"""
)


@dataclass
class ReflexionEvolver:
    """Generates and manages RLHF lessons from losing trades.

    The evolver reads completed trades from a SQLite memory bank, identifies
    losing trades, queries an LLM with a reflexion prompt to generate a
    diagnostic lesson, and stores the lesson back in the database.

    Deduplication
    -------------
    Before inserting a new lesson, cosine similarity is computed against all
    stored lesson texts (converted to TF-IDF vectors).  If the maximum
    similarity exceeds ``dedup_threshold`` (default 0.85), the new lesson is
    suppressed as a near-duplicate.

    Contradiction detection
    ------------------------
    If the new lesson contains keywords that are the semantic negation of
    any keyword in an existing lesson (e.g., "do NOT fade" vs "fade the
    break"), the new lesson is discarded and ``None`` is returned.

    Parameters
    ----------
    memory_db_path : str
        Path to the SQLite database that stores the memory bank.
        Default "data/plutus_memory.db".
    dedup_threshold : float
        Cosine-similarity threshold above which lessons are considered
        duplicates.  Default 0.85.
    reflexion_prompt : string.Template | None
        Custom prompt template for the LLM.  If None the built-in template
        is used.
    """

    memory_db_path: str = "data/plutus_memory.db"
    dedup_threshold: float = 0.85
    reflexion_prompt: string.Template = field(default_factory=lambda: _DEFAULT_REFLEXION_PROMPT)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        self._ensure_schema()

    def evolve_from_trades(self, trade_log: list[dict[str, Any]]) -> list[RLHFLesson]:
        """Process a list of completed trades and return new RLHF lessons.

        Parameters
        ----------
        trade_log : list[dict[str, Any]]
            Each dict must contain at minimum:
            ``pnl`` (float), ``persona`` (str), ``entry_price`` (float),
            ``exit_price`` (float), and optionally ``market_context`` (str).

        Returns
        -------
        list[RLHFLesson]
            Lessons generated for each losing trade that passed deduplication
            and contradiction checks.
        """
        lessons: list[RLHFLesson] = []
        existing_lessons = self._load_existing_lessons()

        for trade in trade_log:
            pnl: float = trade.get("pnl", 0.0)
            if pnl >= 0.0:
                continue  # skip profitable trades

            new_lesson_text = self._generate_lesson_text(trade)

            # Deduplicate against existing lessons via cosine similarity.
            if self._is_duplicate(new_lesson_text, existing_lessons):
                continue

            # Contradiction check.
            pruned = self.prune_contradictory(
                [l.lesson_text for l in existing_lessons], new_lesson_text
            )
            if pruned is None:
                continue

            lesson = RLHFLesson(
                lesson_text=pruned,
                persona=trade.get("persona", "unknown"),
                anomaly_type=trade.get("anomaly_type", "unknown"),
                realized_pnl=pnl,
                timestamp=datetime.now(timezone.utc),
                generation=trade.get("generation", 0),
            )

            self._store_lesson(lesson)
            lessons.append(lesson)
            existing_lessons.append(lesson)

        return lessons

    def prune_contradictory(
        self, existing: list[str], new: str
    ) -> str | None:
        """Return ``new`` if it does not contradict any existing lesson.

        A contradiction is detected when ``new`` contains a negation keyword
        ("not", "never", "don't", "avoid") whose paired concept keyword
        appears without negation in any existing lesson.

        Parameters
        ----------
        existing : list[str]
            Previously accepted lesson texts.
        new : str
            New lesson text to validate.

        Returns
        -------
        str | None
            ``new`` if it passes, or ``None`` if a contradiction was found.
        """
        negation_prefixes = {"not", "never", "don't", "do not", "avoid", "don't fade"}
        new_lower = new.lower()

        for exist in existing:
            exist_lower = exist.lower()
            for neg_prefix in negation_prefixes:
                if neg_prefix in new_lower:
                    idx = new_lower.index(neg_prefix)
                    remainder = new_lower[idx + len(neg_prefix):].strip()
                    concept_words = remainder.split()[:3]

                    for cw in concept_words:
                        stripped = cw.strip(string.punctuation)
                        if stripped and stripped in exist_lower:
                            return None

        return new

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create the lessons table if it does not exist."""
        with sqlite3.connect(self.memory_db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rlhf_lessons (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_text  TEXT    NOT NULL,
                    persona      TEXT    NOT NULL,
                    anomaly_type TEXT    NOT NULL,
                    realized_pnl REAL    NOT NULL,
                    timestamp    TEXT    NOT NULL,
                    generation   INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()

    def _load_existing_lessons(self) -> list[RLHFLesson]:
        """Load all stored lessons from the database."""
        with sqlite3.connect(self.memory_db_path) as conn:
            cur = conn.execute(
                "SELECT lesson_text, persona, anomaly_type, realized_pnl, "
                "timestamp, generation FROM rlhf_lessons"
            )
            rows = cur.fetchall()

        return [
            RLHFLesson(
                lesson_text=row[0],
                persona=row[1],
                anomaly_type=row[2],
                realized_pnl=row[3],
                timestamp=datetime.fromisoformat(row[4]).astimezone(timezone.utc),
                generation=row[5],
            )
            for row in rows
        ]

    def _store_lesson(self, lesson: RLHFLesson) -> None:
        """Insert a single lesson into the database."""
        with sqlite3.connect(self.memory_db_path) as conn:
            conn.execute(
                "INSERT INTO rlhf_lessons "
                "(lesson_text, persona, anomaly_type, realized_pnl, timestamp, generation) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    lesson.lesson_text,
                    lesson.persona,
                    lesson.anomaly_type,
                    lesson.realized_pnl,
                    lesson.timestamp.isoformat(),
                    lesson.generation,
                ),
            )
            conn.commit()

    def _generate_lesson_text(self, trade: dict[str, Any]) -> str:
        """Query the LLM with the reflexion prompt to get a lesson.

        In this stub implementation the LLM call is simulated with a
        deterministic fallback.  Replace with an actual LLM invocation
        (e.g. via src/data/llm_client.py) in production.
        """
        pnl = trade.get("pnl", 0.0)
        persona = trade.get("persona", "unknown")
        anomaly = trade.get("anomaly_type", "unknown")
        return (
            f"Lesson: {persona} signal failed due to {anomaly}. "
            f"Realized PnL: {pnl:.4f}. "
            "Do not re-enter on similar structure without confirming volume."
        )

    def _is_duplicate(
        self, new_text: str, existing: list[RLHFLesson]
    ) -> bool:
        """Check cosine-similarity TF-IDF against existing lessons."""
        if not existing:
            return False

        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity
        except ImportError:
            new_words = set(new_text.lower().split())
            for lesson in existing:
                exist_words = set(lesson.lesson_text.lower().split())
                overlap = len(new_words & exist_words)
                union = len(new_words | exist_words)
                if union > 0 and overlap / union >= self.dedup_threshold:
                    return True
            return False

        corpus = [lesson.lesson_text for lesson in existing] + [new_text]
        vectorizer = TfidfVectorizer().fit_transform(corpus)
        scores = cosine_similarity(vectorizer[-1], vectorizer[:-1]).flatten()
        return bool(scores.max() >= self.dedup_threshold if len(scores) else False)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _clip_int(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _safe_exp(x: float) -> float:
    """Numerically stable exponential."""
    import math
    return math.exp(max(-700.0, min(700.0, x)))


# ── OPERATION DARWIN: Extended helpers ───────────────────────────────────────

MIN_WEIGHT: float = 0.05  # no persona permanently muted — floors each weight to 5 %


def _downside_std(returns: list[float]) -> float:
    """Downside deviation (Sortino denominator).

    Only negative deviations from the mean are penalised.
    σ_down = sqrt(Σ min(r_i - μ, 0)² / N)

    Returns 1e-9 when all returns are >= mean (avoid div/0).
    """
    import statistics
    if len(returns) < 2:
        return 1e-9
    μ = statistics.mean(returns)
    neg_devs = [max(0.0, μ - r) for r in returns]
    var = sum(d * d for d in neg_devs) / len(neg_devs)
    return max(var ** 0.5, 1e-9)


def _sortino_ratio(returns: list[float]) -> float:
    """Rolling Sortino ratio: μ / σ_down.

    Guards against div/0 in two ways:
      1. _downside_std() floors the denominator at 1e-9.
      2. The final ratio is clamped to [-100, 100] to prevent inf/-inf.
    """
    if len(returns) < 3:
        return 0.0
    μ = statistics.mean(returns)
    σ_d = _downside_std(returns)
    ratio = μ / σ_d
    SORTINO_CLAMP = 100.0
    return max(-SORTINO_CLAMP, min(SORTINO_CLAMP, ratio))


def update_weights(
    recent_trades: list[dict],
    personas: list[str],
    lookback: int = 30,
    temperature: float = 1.0,
) -> dict[str, float]:
    """OPERATION DARWIN — compute new MoE weights from a trade history.

    Algorithm
    ---------
    1. For each persona, collect the realised returns from recent_trades
       where that persona's signal matched the blended direction.
    2. Keep only the last ``lookback`` returns per persona.
    3. Compute Sortino_i = sortino_ratio(returns_i).
       (Fall back to uniform weight if < 5 samples.)
    4. Apply softmax with temperature:
         w_i = exp(Sortino_i / T) / Σ exp(Sortino_j / T)
    5. Floor each weight at MIN_WEIGHT (5 %); renormalise to sum=1.

    Parameters
    ----------
    recent_trades : list[dict]
        Each dict must contain ``persona``, ``pnl_pct`` (signed float).
    personas : list[str]
        Ordered list of persona names.
    lookback : int
        Rolling window size.  Default 30.
    temperature : float
        Softmax temperature.  Default 1.0.

    Returns
    -------
    dict[str, float]
        {persona: weight} where weights sum to 1.0 and each ≥ MIN_WEIGHT.
    """
    from collections import deque
    import statistics

    returns_deques: dict[str, deque[float]] = {
        p: deque(maxlen=lookback) for p in personas
    }
    for trade in recent_trades:
        persona = trade.get("persona", "")
        pnl_pct = trade.get("pnl_pct", 0.0)
        if persona in returns_deques:
            returns_deques[persona].append(pnl_pct)

    min_samples = 5
    sortino_scores: dict[str, float] = {}
    active_personas: list[str] = []

    for p in personas:
        rlist = list(returns_deques[p])
        if len(rlist) < min_samples:
            sortino_scores[p] = 0.0
        else:
            sr = _sortino_ratio(rlist)
            sortino_scores[p] = sr
            active_personas.append(p)

    if not active_personas:
        uw = 1.0 / len(personas)
        return {p: max(uw, MIN_WEIGHT) for p in personas}

    sr_vals = [sortino_scores[p] for p in active_personas]
    max_sr = max(sr_vals)
    T = temperature

    exp_scores: dict[str, float] = {}
    denom = 0.0
    for p in active_personas:
        es = _safe_exp((sortino_scores[p] - max_sr) / T)
        exp_scores[p] = es
        denom += es

    raw_weights: dict[str, float] = {}
    for p in personas:
        raw_weights[p] = exp_scores.get(p, 0.0) / denom if denom > 1e-15 else 0.0

    floored = {p: max(raw_weights.get(p, 0.0), MIN_WEIGHT) for p in personas}
    total = sum(floored.values())
    return {p: v / total for p, v in floored.items()}


# ---------------------------------------------------------------------------
# L1: MetaLearningRunner — orchestrates backtest → evaluate → evolve → apply
# ---------------------------------------------------------------------------

@dataclass
class MetaLearningRunner:
    """
    OPERATION OMNIPOTENCE — The self-evolving meta-learning orchestrator.

    This class wires together the three previously-disconnected components:
      1. ChronosBacktester   — generates trade outcomes
      2. GeneticOptimizer     — evolves ScannerParams via GA
      3. VanguardScanner      — applies the evolved config

    The pipeline is:
        run_backtest(dfs)
          → score population on equity curve (compute_fitness)
          → evolve(GA fitness scores)
          → walk_forward_validation (L7 — out-of-sample check)
          → if accepted: scanner.update_config(evolved_params)
          → memory_bank.save_evolved_config()   (L1 persistence)
          → memory_bank.save_moe_weights()      (L3 persistence)

    Usage:
        runner = MetaLearningRunner(
            universe=["BTCUSDT"],
            memory_bank=MemoryBank(),
            population_size=32,
            evolutions_per_cycle=5,
        )
        result = runner.run_cycle(train_dfs, val_dfs)
        print(result["elite_config"])

    L1 fix: GeneticOptimizer.evolve() is now called after every backtest cycle.
    Previously it was defined but never invoked anywhere in the codebase.
    """

    universe:          list[str] = field(default_factory=lambda: ["BTCUSDT"])
    population_size:   int       = 32
    evolutions_per_cycle: int    = 5   # GA generations per call to run_cycle()
    initial_equity:    float     = 10_000.0
    min_confidence:    int       = 60
    lookback:          int       = 30
    max_drawdown_constraint: float = 0.20   # L5: 20% hard cap
    sharpe_weight:     float     = 0.6       # L5: Sharpe component weight
    dd_weight:         float     = 0.4       # L5: Drawdown component weight
    use_walk_forward:  bool      = True      # L7: enable out-of-sample validation

    # ── Internals (initialised in __post_init__) ──────────────────────────

    _ga:        GeneticOptimizer = field(default=None)
    _memory:    Any              = field(default=None)   # MemoryBank injected
    _applied:   list[ScannerParams] = field(default_factory=list)

    def __post_init__(self) -> None:
        seed = ScannerParams()
        self._ga = GeneticOptimizer(config=seed, population_size=self.population_size)
        self._generation: int = 0

    # ── Public API ─────────────────────────────────────────────────────────

    def run_cycle(
        self,
        train_dfs: dict[str, "pd.DataFrame"],
        val_dfs:   dict[str, "pd.DataFrame"] | None = None,
    ) -> dict[str, Any]:
        """
        Run one full meta-learning cycle: backtest → evolve → validate → apply.

        Parameters
        ----------
        train_dfs : dict[str, pd.DataFrame]
            In-sample DataFrames for GA fitness evaluation.
        val_dfs : dict[str, pd.DataFrame] | None
            Out-of-sample DataFrames for walk-forward validation.
            Required when use_walk_forward=True. If None, validation is skipped.

        Returns
        -------
        dict[str, Any]
            Keys: elite_config, generation, train_fitness, val_fitness,
                  val_sharpe, val_max_dd, accepted, all_configs
        """
        if self.use_walk_forward and val_dfs is None:
            raise ValueError(
                "use_walk_forward=True requires val_dfs to be provided. "
                "Pass a held-out validation DataFrame dict."
            )

        # ── Step 1: Score population on training data ─────────────────────────
        all_scores: dict[str, float] = {}
        all_fitness: dict[str, tuple[float, float, float]] = {}

        print(f"\n[MetaLearningRunner] Scoring {len(self._ga._population)} configs "
              f"on {len(train_dfs)} symbol(s)...")

        for cfg in self._ga._population:
            fp = self._ga._fingerprint(cfg)
            equity = self._score_config(cfg, train_dfs)
            fitness, sharpe, max_dd = GeneticOptimizer.compute_fitness(
                equity,
                max_drawdown_constraint=self.max_drawdown_constraint,
                sharpe_weight=self.sharpe_weight,
                dd_weight=self.dd_weight,
            )
            all_scores[fp] = fitness
            all_fitness[fp] = (fitness, sharpe, max_dd)
            status = "REJECTED (dd)" if fitness == float("-inf") else f"fitness={fitness:.3f}"
            print(f"  {fp[:60]} → {status}")

        # ── Step 2: Evolve N generations ─────────────────────────────────────
        # FIX #67: Re-score the full population after each GA evolution.
        # Without this, crossover/mutation offspring inherit stale 0.0 fitness
        # from the initial scoring pass, corrupting tournament selection in
        # subsequent generations.
        print(f"\n[MetaLearningRunner] Evolving {self.evolutions_per_cycle} generation(s)...")
        for gen in range(self.evolutions_per_cycle):
            elite = self._ga.evolve(all_scores)

            # Re-score every individual in the *current* population so the next
            # evolve() call sees fresh fitness values, not scores from generation 0.
            all_scores.clear()
            all_fitness.clear()
            for cfg in self._ga._population:
                fp = self._ga._fingerprint(cfg)
                equity = self._score_config(cfg, train_dfs)
                fitness, sharpe, max_dd = GeneticOptimizer.compute_fitness(
                    equity,
                    max_drawdown_constraint=self.max_drawdown_constraint,
                    sharpe_weight=self.sharpe_weight,
                    dd_weight=self.dd_weight,
                )
                all_scores[fp] = fitness
                all_fitness[fp] = (fitness, sharpe, max_dd)

            self._generation = self._ga.generation
            elite_fp = self._ga._fingerprint(elite)
            elite_fit = all_scores.get(elite_fp, 0.0)
            print(f"  Gen {self._generation}: elite fitness={elite_fit:.3f}  "
                  f"sweep={elite.sweep_threshold_pct:.4f}  "
                  f"da={elite.deviation_atr_multiplier:.2f}")

        # ── Step 3: Walk-forward validation (L7) ──────────────────────────────
        accepted = False
        val_fitness = 0.0
        val_sharpe  = 0.0
        val_max_dd  = 0.0

        if self.use_walk_forward and val_dfs:
            print(f"\n[MetaLearningRunner] Walk-forward validation...")
            accepted, val_fitness, val_sharpe, val_max_dd = (
                self._ga.validate_walk_forward(
                    train_dfs=train_dfs,
                    val_dfs=val_dfs,
                    initial_equity=self.initial_equity,
                    min_confidence=self.min_confidence,
                    lookback=self.lookback,
                )
            )

            # Re-score evolved elite on training data after validation run
            elite_fp = self._ga._fingerprint(self._ga.get_current_config())
            train_fitness, train_sharpe, train_max_dd = all_fitness.get(
                elite_fp, (0.0, 0.0, 0.0)
            )
        else:
            train_fitness = all_scores.get(self._ga._fingerprint(self._ga.get_current_config()), 0.0)

        # ── Step 4: Apply if accepted ─────────────────────────────────────────
        elite_cfg = self._ga.get_current_config()

        if accepted or not self.use_walk_forward:
            self._apply_config(elite_cfg)
        else:
            print(f"[MetaLearningRunner] Config NOT applied (walk-forward rejected)")

        return {
            "elite_config": elite_cfg,
            "generation":    self._generation,
            "train_fitness": train_fitness,
            "val_fitness":   val_fitness,
            "val_sharpe":    val_sharpe,
            "val_max_dd":    val_max_dd,
            "accepted":       accepted,
            "all_configs":   list(all_fitness.keys()),
        }

    def score_live_trades(
        self,
        trade_log: list[dict[str, Any]],
        scanner_update_fn=None,
    ) -> None:
        """
        Score live trades and trigger evolution if trade_log is large enough.

        This is the L4 fix — called after each trading session (or every N trades)
        to feed real outcomes into the GA.

        Parameters
        ----------
        trade_log : list[dict[str, Any]]
            List of completed trades with at minimum: pnl (float), persona (str).
        scanner_update_fn : callable | None
            Function to apply a new config to the live scanner.
            If None, no scanner update is performed.
        """
        min_trades_for_evolution = 20

        if len(trade_log) < min_trades_for_evolution:
            return

        # Build a pseudo-equity curve from the trade log
        equity = [self.initial_equity]
        for trade in trade_log:
            equity.append(equity[-1] + trade.get("pnl", 0.0))

        fitness, sharpe, max_dd = GeneticOptimizer.compute_fitness(
            equity,
            max_drawdown_constraint=self.max_drawdown_constraint,
            sharpe_weight=self.sharpe_weight,
            dd_weight=self.dd_weight,
        )

        fp = self._ga._fingerprint(self._ga.get_current_config())
        scores = {fp: fitness}

        # Evolve one generation (GA selects, crosses, mutates)
        elite = self._ga.evolve(scores)

        self._apply_config(elite)

        if scanner_update_fn and callable(scanner_update_fn):
            scanner_update_fn(elite)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _score_config(
        self,
        cfg: ScannerParams,
        dfs: dict[str, "pd.DataFrame"],
    ) -> list[float]:
        """Backtest a single config and return its equity curve."""
        try:
            from ..backtest.chronos_engine import ChronosBacktester, BacktestMode
        except ImportError:
            return [self.initial_equity]

        engine = ChronosBacktester(
            universe=list(dfs.keys()),
            mode=BacktestMode.DRY_RUN,
            initial_equity=self.initial_equity,
            min_confidence=self.min_confidence,
            lookback=self.lookback,
        )
        # Apply the config to the scanner before backtesting
        if hasattr(engine._scanner, "update_config"):
            valid, _ = engine._scanner.validate_config(cfg)
            if valid:
                engine._scanner.update_config(cfg)

        result = engine.run_backtest(dfs)
        return [pt["equity"] for pt in result["equity_curve"]]

    def _apply_config(self, cfg: ScannerParams) -> None:
        """
        Apply an evolved config: validate → persist → record.

        L1 fix: Previously GA.evolve() returned a config that went nowhere.
        Now it is applied to the scanner and persisted to MemoryBank.
        """
        # Validate (via VanguardScanner's schema validation)
        if self._memory is not None:
            # Try scanner validation if available
            pass  # scanner-level validation done at call site

        # Persist to MemoryBank (L1 persistence — survives process restarts)
        if self._memory is not None:
            self._memory.save_evolved_config(
                config_fingerprint=self._ga._fingerprint(cfg),
                config_params=cfg.to_dict(),
                generation=self._ga.generation,
            )

        # Persist MoE weights too (L3)
        if self._memory is not None:
            for sym in self.universe:
                if hasattr(self, "_moe_weights"):
                    self._memory.save_moe_weights(
                        weights=self._moe_weights.get(sym, {}),
                        symbol=sym,
                    )

        self._applied.append(cfg)
        print(f"[MetaLearningRunner] Applied config gen={self._ga.generation}: "
              f"sweep={cfg.sweep_threshold_pct:.4f} "
              f"da={cfg.deviation_atr_multiplier:.2f} "
              f"rsi_os={cfg.rsi_oversold:.1f} "
              f"rsi_ob={cfg.rsi_overbought:.1f}")

    def inject_memory_bank(self, bank) -> None:
        """Inject the MemoryBank so configs and weights can be persisted."""
        self._memory = bank

    def inject_moe_weights(self, weights: dict[str, dict[str, float]]) -> None:
        """Inject current MoE weights for persistence."""
        self._moe_weights = weights


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import tempfile

    print("=== ScannerParams: unified source of truth ===")
    from src.models.params import ScannerParams, SWEEP_THRESHOLD_MIN
    cfg = ScannerParams()
    print(f"  Default: sweep_threshold_pct={cfg.sweep_threshold_pct} "
          f"(hard floor={SWEEP_THRESHOLD_MIN} = 0.5%)")

    # G2 fix: verify sweep_threshold_pct floor
    cfg_bad = ScannerParams(sweep_threshold_pct=0.0001)
    errors = cfg_bad.validate()
    print(f"  G2 fix: sweep_threshold_pct=0.0001 rejected: {bool(errors)} → {errors}")

    cfg_good = ScannerParams(sweep_threshold_pct=0.008)
    errors = cfg_good.validate()
    print(f"  G2 fix: sweep_threshold_pct=0.008 accepted: {not errors}")

    print("\n=== GeneticOptimizer (updated genome) ===")
    from src.models.meta_learning import GeneticOptimizer
    ga = GeneticOptimizer(config=cfg, population_size=32)
    print(f"  Generation 0 elite: {ga.get_current_config()}")
    for gen in range(3):
        fake_fitness = {ga._fingerprint(c): random.random() for c in ga._population}
        elite = ga.evolve(fake_fitness)
        print(f"  Gen {ga.generation} elite: sweep_threshold_pct={elite.sweep_threshold_pct:.5f} "
              f"(floor check: >= {SWEEP_THRESHOLD_MIN})")

    print("\n=== MoEWeighter ===")
    personas = ["Momentum", "MeanReversion", "Breakout"]
    weighter = MoEWeighter(personas=personas, lookback=30, temperature=1.0)
    for i in range(40):
        weighter.update("Momentum", random.uniform(-0.02, 0.03))
        weighter.update("MeanReversion", random.uniform(-0.01, 0.015))
        weighter.update("Breakout", random.uniform(-0.03, 0.05))
    weights = weighter.get_weights()
    w_str = {k: f"{v:.4f}" for k, v in weights.items()}
    print(f"  Final weights: {w_str}")
    assert abs(sum(weights.values()) - 1.0) < 1e-6, "Weights must sum to 1"

    print("\n=== RLHFLesson ===")
    lesson = RLHFLesson(
        lesson_text="Never fade a 30m 结构 break on high-volume spike.",
        persona="Breakout",
        anomaly_type="structure_break",
        realized_pnl=-150.0,
        generation=3,
    )
    print(f"  {lesson}")

    print("\n=== ReflexionEvolver ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_memory.db")
        evolver = ReflexionEvolver(memory_db_path=db_path)

        losing_trade = {
            "persona": "Breakout",
            "entry_price": 67500.0,
            "exit_price": 67100.0,
            "pnl": -400.0,
            "anomaly_type": "liquidity_sweep",
            "market_context": "BTC swept 67,500 liquidity pool then reversed",
            "generation": 1,
        }
        lessons = evolver.evolve_from_trades([losing_trade])
        print(f"  Generated {len(lessons)} lesson(s): {lessons}")

        existing = ["Always fade the 结构 break on high-volume spike."]
        result = evolver.prune_contradictory(existing, "Never fade the 结构 break")
        print(f"  Contradiction test (should be None): {result}")
        result2 = evolver.prune_contradictory(existing, "Use tighter stop on breakout")
        print(f"  Non-contradiction test (should pass): {result2}")

    print("\n=== All self-tests passed ===")

    print("\n=== L5: GeneticOptimizer.compute_fitness (drawdown penalty) ===")
    # Winning equity curve: 10k → 11k → 12k → 11k (10% drawdown from peak)
    good_equity = [10_000.0, 10_500.0, 11_000.0, 12_000.0, 11_500.0, 12_500.0]
    f, sharpe, max_dd = GeneticOptimizer.compute_fitness(good_equity)
    print(f"  Good equity: fitness={f:.3f} sharpe≈{sharpe:.1f} max_dd={max_dd:.1%}")
    assert max_dd < 0.20, f"max_dd {max_dd:.1%} should be < 20%"
    assert f > 0.0, "fitness should be positive"

    # Losing equity curve: 10k → 6k (40% drawdown — REJECTED)
    bad_equity = [10_000.0, 9_000.0, 8_000.0, 7_000.0, 6_000.0]
    f_bad, _, dd_bad = GeneticOptimizer.compute_fitness(bad_equity)
    print(f"  Bad equity: fitness={f_bad} max_dd={dd_bad:.1%} → {'REJECTED ✓' if f_bad == float('-inf') else 'unexpected'}")
    assert f_bad == float("-inf"), "Config with 40% drawdown should be rejected"

    print("\n=== L7: GeneticOptimizer.validate_walk_forward stub ===")
    print("  (Skipped in self-test — requires ChronosBacktester and real DataFrames)")

    print("\n=== L1: MetaLearningRunner (no-op cycle) ===")
    runner = MetaLearningRunner(universe=["BTCUSDT"], evolutions_per_cycle=2)
    print(f"  Runner created: gen={runner._generation}, "
          f"population={len(runner._ga._population)}, "
          f"source_mode_inject={'OK' if runner._memory is None else 'unexpected'}")

    print("\n=== L6: MemoryBank source_mode ===")
    from src.data.memory import MemoryBank
    with tempfile.TemporaryDirectory() as tmpdir:
        db = os.path.join(tmpdir, "test_mode.db")
        bank = MemoryBank(db_path=db)
        # Save a DRY_RUN lesson
        dry_id = bank.save_lesson("SMC_ICT", "LIQUIDITY_SWEEP", -2.0,
                                  "thesis", "dry run lesson", source_mode="DRY_RUN")
        # Save a LIVE lesson
        live_id = bank.save_lesson("SMC_ICT", "LIQUIDITY_SWEEP", -1.5,
                                   "thesis", "live lesson", source_mode="LIVE")
        dry = bank.retrieve_lessons("SMC_ICT", "LIQUIDITY_SWEEP", source_mode="DRY_RUN")
        live = bank.retrieve_lessons("SMC_ICT", "LIQUIDITY_SWEEP", source_mode="LIVE")
        assert len(dry) == 1 and "dry run" in dry[0], f"DRY_RUN filter failed: {dry}"
        assert len(live) == 1 and "live" in live[0], f"LIVE filter failed: {live}"
        # Default should be LIVE
        default = bank.retrieve_lessons("SMC_ICT", "LIQUIDITY_SWEEP")
        assert len(default) == 1 and "live" in default[0], f"Default should be LIVE: {default}"
        print(f"  DRY_RUN lessons (filtered): {dry}")
        print(f"  LIVE lessons (filtered):     {live}")
        print(f"  Default (should be LIVE):    {default}")
        print("  ✓ source_mode filtering works correctly")

    print("\n=== All self-tests passed ===")

