"""
LLM Decision Logger - Saves detailed logs for analysis.
"""

import json
import os
from datetime import datetime
from pathlib import Path

LOG_DIR = Path("/Users/austinliu/Documents/Personal Projects/Plutus/logs/backtest")


def save_llm_decision(decision: dict, symbol: str, candle_data: dict, result: str = None):
    """Save LLM decision to log file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Use date-based filename
    date_str = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"llm_decisions_{date_str}.jsonl"

    entry = {
        "timestamp": datetime.now().isoformat(),
        "symbol": symbol,
        "decision": decision.get("decision"),
        "order_type": decision.get("order_type"),
        "limit_price": decision.get("limit_price"),
        "entry_price": decision.get("limit_price") or candle_data.get("current_price"),
        "stop_loss": decision.get("stop_loss"),
        "take_profit": decision.get("take_profit"),
        "invalidation": decision.get("invalidation"),
        "rr": decision.get("rr"),
        "risk_level": decision.get("risk_level"),
        "stop_distance_pct": decision.get("stop_distance_pct"),
        "reason": decision.get("reason"),
        "result": result,
        # Market context at time of decision
        "candle_time": candle_data.get("datetime"),
        "current_price": candle_data.get("current_price"),
        "trend": candle_data.get("trend"),
        "rsi": candle_data.get("rsi"),
        "support": candle_data.get("support"),
        "resistance": candle_data.get("resistance"),
        "fear_greed": candle_data.get("fear_greed"),  # Real value or None
    }

    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")

    return log_file


def save_backtest_result(result_summary: dict, params: dict):
    """Save backtest result summary."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now().strftime("%Y-%m-%d")
    result_file = LOG_DIR / f"backtest_results_{date_str}.json"

    entry = {
        "timestamp": datetime.now().isoformat(),
        "params": params,
        "results": result_summary,
    }

    with open(result_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


def get_llm_decisions_for_date(date_str: str = None):
    """Get all LLM decisions for a specific date."""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    log_file = LOG_DIR / f"llm_decisions_{date_str}.jsonl"

    if not log_file.exists():
        return []

    decisions = []
    with open(log_file, "r") as f:
        for line in f:
            decisions.append(json.loads(line))

    return decisions
