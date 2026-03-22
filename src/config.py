"""
Configuration module for Plutus Trading System.
"""

import os
from pathlib import Path

# Project paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = PROJECT_ROOT / "memory"
DAILY_ANALYSIS_DIR = MEMORY_DIR / "daily_analysis"
FEEDBACK_DIR = MEMORY_DIR / "feedback"

# Ensure directories exist
DAILY_ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)

# API Configuration
BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_FUTURES_URL = "https://fapi.binance.com"
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"

# Data source preference
DEFAULT_DATA_SOURCE = "futures"  # 'futures' or 'spot'

# Trading pairs
TRADING_PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
SYMBOLS = {
    "BTCUSDT": {"name": "Bitcoin", "type": "major"},
    "ETHUSDT": {"name": "Ethereum", "type": "major"},
    "SOLUSDT": {"name": "Solana", "type": "small"},
}

# Timeframes
TIMEFRAMES = ["1h", "4h", "1d", "5m", "15m", "30m"]

# Default parameters
DEFAULT_LIMIT = 200
DEFAULT_RISK_PCT = 0.01  # 1% risk per trade

# Position sizing
RISK_MULTIPLIERS = {
    "LOW": (1.0, 1.2),
    "MODERATE": (0.7, 1.0),
    "HIGH": (0.3, 0.5),
}

# Leverage buffers
LEVERAGE_BUFFERS = {
    "major": 0.005,  # 0.5% for BTC, ETH
    "small": 0.015,  # 1.5% for alts
}

# Position caps (as multiple of equity)
POSITION_CAP_TRAINING = 1.0
POSITION_CAP_ADVANCED = 1.5

# Gate A: Small stop penalty threshold
# Unified to 0.5% per CLAUDE.md (authoritative source)
# This is the authoritative value — all code must use this.
SMALL_STOP_THRESHOLD = 0.005  # 0.5%

# Volume profile settings
VOLUME_PROFILE_BINS = 50
LVN_THRESHOLD = 20  # percentile
HVN_THRESHOLD = 80  # percentile

# Risk classification thresholds
HIGH_RISK_ATR_MULTIPLIER = 1.5
EXTREME_FEAR_THRESHOLD = 20
EXTREME_GREED_THRESHOLD = 80
