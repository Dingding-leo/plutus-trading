"""
src/execution/router/ — Institutional Execution Layer
=====================================================

Module structure (mirrors the sections from order_router.py):

  base.py          — Slice dataclass (shared type) + _default_slice_executor
  twap.py          — TWAPExecutor
  vwap.py          — VWAPExecutor
  limit_queue.py   — LimitOrderQueue
  smart_router.py  — SmartRouter
  market_impact.py — MarketImpactModel
  market_executor.py — MarketExecutor

This __init__.py re-exports everything from the original order_router.py
for backward compatibility.  Once each class is extracted into its own
file, this module will forward to those files instead.

Backward-compatible import:
    from src.execution.order_router import SmartRouter
    from src.execution.router import SmartRouter   # same thing
"""

from src.execution.order_router import (
    Slice,
    TWAPExecutor,
    VWAPExecutor,
    LimitOrderQueue,
    SmartRouter,
    MarketImpactModel,
    MarketExecutor,
    _default_slice_executor,
)

__all__ = [
    "Slice",
    "TWAPExecutor",
    "VWAPExecutor",
    "LimitOrderQueue",
    "SmartRouter",
    "MarketImpactModel",
    "MarketExecutor",
    "_default_slice_executor",
]
