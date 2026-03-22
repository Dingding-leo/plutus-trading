"""
src/execution/risk/ — Risk Management
=====================================

Module structure:

  guard.py     — RiskGuard (main enforcement class)
  snapshots.py — EquitySnapshot, SessionSnapshot

This __init__.py re-exports everything from the original risk_limits.py
for backward compatibility.  Once RiskGuard and snapshots are extracted
into their own files, this module will forward to those files instead.

Backward-compatible import:
    from src.execution.risk_limits import RiskGuard
    from src.execution.risk import RiskGuard           # same thing
"""

from src.execution.risk_limits import (
    RiskLimitExceeded,
    RiskEnvironment,
    EquitySnapshot,
    SessionSnapshot,
    RiskGuard,
    load_risk_config,
)

__all__ = [
    "RiskLimitExceeded",
    "RiskEnvironment",
    "EquitySnapshot",
    "SessionSnapshot",
    "RiskGuard",
    "load_risk_config",
]
