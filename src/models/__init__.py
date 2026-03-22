"""Plutus V4.0 — Alpha Research Models.

Exports the meta-learning components:
- GeneticOptimizer  — evolves ScannerParams across generations
- QLearnConfig       — reinforcement-learning hyperparameters for Q-learning (deferred)
- MoEWeighter        — dynamic persona weight allocation via softmax Sharpe
- ScannerParams      — unified scanner parameter bundle (single source of truth)
- ParamSchema        — type/range enforcement for all GA-evolvable params
- RLHFLesson         — replay-buffer entry for RLHF reflexion
- ReflexionEvolver   — generates & deduplicates lessons from losing trades

Architecture note
-----------------
ScannerParams (in src/models/params.py) is the SINGLE SOURCE OF TRUTH for all
12 GA-evolvable scanner parameters.  GeneticOptimizer.evolve() returns ScannerParams.
VanguardScanner.update_config() accepts ScannerParams and validates against ParamSchema.
All other modules import ScannerParams from here.
"""

from __future__ import annotations

from src.models.params import (
    DEFERRED_NOTE,
    ParamSchema,
    QLearnConfig,
    ScannerConfig,    # backward-compat alias — same class as ScannerParams
    ScannerParams,
    SCANNER_PARAM_SCHEMAS,
    SWEEP_THRESHOLD_MAX,
    SWEEP_THRESHOLD_MIN,
)

__all__ = [
    # Core unified params
    "ScannerParams",
    "ScannerConfig",       # backward-compat alias — same class as ScannerParams
    "ParamSchema",
    "SCANNER_PARAM_SCHEMAS",
    # GA constraints
    "SWEEP_THRESHOLD_MIN",
    "SWEEP_THRESHOLD_MAX",
    # Deferred Q-learning
    "QLearnConfig",
    "DEFERRED_NOTE",
    # Meta-learning components
    "GeneticOptimizer",
    "MoEWeighter",
    "RLHFLesson",
    "ReflexionEvolver",
]
