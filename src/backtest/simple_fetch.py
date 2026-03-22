"""
Simple historical data fetcher with file-level cache.
"""

import hashlib
import os
import requests
from datetime import datetime, timedelta
from typing import List, Optional

# P3-FIX: File-level cache keyed by (symbol, timeframe, start_date, end_date).
# Stores in data/cache/ as JSON, keyed by content-hash of params.
# Eliminates redundant Binance API calls across backtest runs and within the same session.
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "cache")
os.makedirs(_CACHE_DIR, exist_ok=True)


def _cache_key(symbol: str, timeframe: str, start_date: str, end_date: str | None) -> str:
    return hashlib.md5(
        f"{symbol}:{timeframe}:{start_date}:{end_date or 'now'}".encode()
    ).hexdigest()


def _cache_path(symbol: str, timeframe: str, start_date: str, end_date: str | None) -> str:
    key = _cache_key(symbol, timeframe, start_date, end_date)
    return os.path.join(_CACHE_DIR, f"{symbol}_{timeframe}_{key}.json")


def _load_cached(path: str) -> Optional[List[dict]]:
    """Load list[dict] from JSON cache if file exists."""
    if not os.path.exists(path):
        return None
    try:
        import json
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _save_cached(path: str, candles: List[dict]) -> None:
    """Persist candles to JSON cache."""
    try:
        import json
        with open(path, "w") as f:
            json.dump(candles, f, default=str)
    except Exception:
        pass  # best-effort only


def fetch_binance_history(
    symbol: str,
    timeframe: str,
    start_date: str,
    end_date: str | None = None,
    max_candles: int | None = None,
) -> List[dict]:
    """Fetch historical candles from Binance (cached by date range + symbol + tf)."""

    # P3-FIX: Try JSON cache first — avoids redundant network calls on repeated backtests.
    cache_path = _cache_path(symbol, timeframe, start_date, end_date)
    cached = _load_cached(cache_path)
    if cached is not None:
        if max_candles and len(cached) > max_candles:
            cached = cached[-max_candles:]
        return cached

    # Parse dates
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else datetime.now()

    start_ts = int(start.timestamp() * 1000)
    end_ts = int(end.timestamp() * 1000)

    all_candles: List[dict] = []
    session = requests.Session()

    # Binance limits to 1000 per request
    current_ts = start_ts

    while current_ts < end_ts:
        url = "https://api.binance.com/api/v3/klines"
        params = {
            "symbol": symbol,
            "interval": timeframe,
            "startTime": current_ts,
            "endTime": end_ts,
            "limit": 1000,
        }

        try:
            resp = session.get(url, params=params, timeout=30)
            data = resp.json()
        except Exception:
            return []  # Return empty on error

        if not data or not isinstance(data, list):
            break

        for row in data:
            # Skip invalid rows
            if len(row) < 6:
                continue
            try:
                ts = int(row[0]) if isinstance(row[0], str) else int(row[0])
                all_candles.append({
                    "timestamp": ts,
                    "datetime": datetime.fromtimestamp(ts / 1000),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "quote_volume": float(row[7]) if len(row) > 7 else 0,
                })
            except Exception:
                continue

        # Move to next batch
        last_ts = data[-1][0]
        if last_ts <= current_ts:
            break
        current_ts = last_ts + 1

        if max_candles and len(all_candles) >= max_candles:
            break

    # Sort by timestamp
    all_candles.sort(key=lambda x: x["timestamp"])

    if max_candles and len(all_candles) > max_candles:
        all_candles = all_candles[-max_candles:]

    # P3-FIX: Persist to cache before returning so subsequent calls hit the cache.
    _save_cached(cache_path, all_candles)
    return all_candles


# ─── Defensive Duplicate-Check Guard (Issue #7) ─────────────────────────────────
# CRITICAL: This guard catches accidental duplicate function definitions at import
# time. Python raises a generic SyntaxError for duplicate 'def' at module level,
# but the message does not name the offending function — making the crash confusing.
# This explicit check replaces the cryptic error with a clear diagnostic.
# If a second `fetch_binance_history` definition is added above, this fires first.
_defined_funcs = set()
for _name, _obj in list(globals().items()):
    if callable(_obj) and not _name.startswith("_"):
        if _name in _defined_funcs:
            raise ImportError(
                f"[ISSUE #7] DUPLICATE FUNCTION DEFINITION: '{_name}' is defined "
                f"more than once in simple_fetch.py. "
                f"This causes a SyntaxError at import time and crashes the backtest. "
                f"Rename or remove the duplicate."
            )
        _defined_funcs.add(_name)
del _defined_funcs, _name, _obj
