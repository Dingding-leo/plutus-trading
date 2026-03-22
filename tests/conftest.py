"""
Shared pytest fixtures for Plutus Trading tests.
"""
import pytest
import numpy as np
from unittest.mock import Mock, patch
from pathlib import Path
import tempfile
import sqlite3

# ─── LLM Fixtures ───────────────────────────────────────────────

@pytest.fixture
def mock_llm_response():
    """Factory for mock LLM responses."""
    def _make(decision="NO_TRADE", symbol="NONE", risk_level="MODERATE", reason="test"):
        return {
            "decision": decision,
            "symbol": symbol,
            "risk_level": risk_level,
            "reason": reason,
        }
    return _make


@pytest.fixture
def mock_llm_client():
    """Mock LLM client that returns configurable responses."""
    mock = Mock()
    mock.chat.return_value = '{"macro_regime": "NEUTRAL", "btc_strength": "NEUTRAL", "volatility_warning": "LOW"}'
    mock.async_chat = Mock(return_value='{"macro_regime": "NEUTRAL", "btc_strength": "NEUTRAL", "volatility_warning": "LOW"}')
    return mock


# ─── Market Data Fixtures ───────────────────────────────────────

@pytest.fixture
def sample_candles_200():
    """200 realistic 1h candles for indicator tests."""
    np.random.seed(42)
    base_price = 67000
    returns = np.random.randn(200) * 100
    closes = base_price + np.cumsum(returns)

    candles = []
    for i, close in enumerate(closes):
        high = close * (1 + abs(np.random.rand() * 0.02))
        low = close * (1 - abs(np.random.rand() * 0.02))
        candles.append({
            "open_time": 1000 * (1700000000 + i * 3600),
            "open": close * (1 - np.random.rand() * 0.01),
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000 + np.random.rand() * 500,
            "close_time": 1000 * (1700000000 + (i + 1) * 3600),
            "quote_volume": close * 1000,
            "trades": int(100 + np.random.rand() * 50),
        })
    return candles


@pytest.fixture
def sample_candles_50():
    """50 candles for short-period indicator tests."""
    np.random.seed(42)
    base_price = 67000
    closes = base_price + np.cumsum(np.random.randn(50) * 50)

    candles = []
    for i, close in enumerate(closes):
        high = close * 1.01
        low = close * 0.99
        candles.append({
            "open_time": 1000 * (1700000000 + i * 3600),
            "open": close * 0.999,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000.0,
            "close_time": 1000 * (1700000000 + (i + 1) * 3600),
        })
    return candles


# ─── Memory Bank Fixtures ───────────────────────────────────────

@pytest.fixture
def temp_memory_db(tmp_path):
    """Temporary SQLite DB for MemoryBank tests."""
    db_path = tmp_path / "test_memory.db"
    from src.data.memory import MemoryBank
    bank = MemoryBank(db_path=db_path)
    yield bank
    bank.close()


# ─── Config Fixtures ────────────────────────────────────────────

@pytest.fixture
def sample_config():
    """Sample trading configuration."""
    return {
        "equity": 10000.0,
        "risk_pct": 0.01,
        "pos_mult": 0.5,
        "stop_distance": 0.02,
        "coin_type": "major",
        "training_mode": True,
    }
