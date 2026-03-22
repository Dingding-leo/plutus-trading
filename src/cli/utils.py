"""
Shared utilities for CLI command modules.

All imports use explicit src. prefix to ensure they resolve correctly
regardless of whether the CLI is imported as `cli` (from src/)
or `src.cli` (from project root).
"""

from src.config import SYMBOLS, TRADING_PAIRS
from src.data import binance_client, coingecko_client
from src.data.coin_tiers import normalize_symbol
from src.analysis import indicators, volume_profile, market_context
from src.execution import position_sizer, decision_engine, trade_plan
from src.storage import daily_logger, feedback_logger
from src.data.llm_client import get_llm_macro_context
from src.backtest.hybrid_strategy import HybridWorkflowStrategy


def risk_level_to_pos_mult(risk_level: str) -> float:
    """Convert risk level string to position multiplier."""
    return position_sizer.get_position_multiplier(risk_level.upper())
