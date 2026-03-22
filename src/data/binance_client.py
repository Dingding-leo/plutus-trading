"""
Binance API client for fetching OHLCV data.
"""

import json
import time
import os
import threading
from typing import Optional, List

import requests
import pandas as pd
import concurrent.futures
from pathlib import Path
from .. import config


# ─── Rate Limiter ───────────────────────────────────────────────────────────────

class RateLimiter:
    """
    Module-level rate limiter to avoid exceeding Binance API call limits.

    Uses a semaphore + background replenishment thread so that waiting threads
    do NOT block each other (C2 fix: no lock held during sleep).
    """
    _sem: threading.Semaphore | None = None
    _timer: threading.Timer | None = None
    _lock = threading.Lock()
    MIN_INTERVAL = 0.2  # seconds — 5 calls / second max
    _initial_permits = 1  # always maintain exactly 1 available permit

    @classmethod
    def wait(cls) -> None:
        # Lazy-init to avoid class-level side-effects at import time
        with cls._lock:
            if cls._sem is None:
                cls._sem = threading.Semaphore(cls._initial_permits)
                cls._start_replenisher()
        # acquire() blocks only until a permit is available — no sleeping while holding the lock
        cls._sem.acquire()

    @classmethod
    def _start_replenisher(cls) -> None:
        """Background thread that releases one permit every MIN_INTERVAL seconds."""
        def replenish() -> None:
            if cls._sem is None:
                return
            cls._sem.release()
            with cls._lock:
                if cls._timer is not None and cls._timer.is_alive():
                    cls._timer.cancel()
                cls._timer = threading.Timer(cls.MIN_INTERVAL, replenish)
                cls._timer.daemon = True
                cls._timer.name = "rate_limiter_replenisher"
                cls._timer.start()

        with cls._lock:
            if cls._timer is not None and cls._timer.is_alive():
                cls._timer.cancel()
            cls._timer = threading.Timer(cls.MIN_INTERVAL, replenish)
            cls._timer.daemon = True
            cls._timer.name = "rate_limiter_replenisher"
            cls._timer.start()

    @classmethod
    def close(cls) -> None:
        """Stop the replenisher. Call at application shutdown."""
        with cls._lock:
            if cls._timer is not None:
                cls._timer.cancel()
                cls._timer = None


# ─── Write Rate Limiter (#20) ──────────────────────────────────────────────────
# Binance enforces stricter limits on write endpoints (order placement, cancellation).
# Weight-based: POST orders = 1, DELETE orders = 1.
# Limit: 1200 weights/minute → conservatively target ~600/min (1 permit every 0.1 s).
#
# The semaphore is shared across all threads; a background replenisher adds permits
# so no caller ever blocks indefinitely.

class WriteRateLimiter:
    """
    Module-level rate limiter for Binance write (POST / DELETE) endpoints.

    Binance write limit is ~1200 weights/minute.  We target 600 weight-min
    (1 POST order ≈ 1 weight) to leave headroom, giving a permit every 0.1 s.

    Usage::

        WriteRateLimiter.wait()   # blocks until a permit is available
        client.place_order(...)
    """
    _sem: threading.Semaphore | None = None
    _timer: threading.Timer | None = None
    _lock = threading.Lock()
    MIN_INTERVAL = 0.1           # seconds between write permits (≈ 600/min)
    _initial_permits = 1

    @classmethod
    def wait(cls) -> None:
        with cls._lock:
            if cls._sem is None:
                cls._sem = threading.Semaphore(cls._initial_permits)
                cls._start_replenisher()
        cls._sem.acquire()

    @classmethod
    def _start_replenisher(cls) -> None:
        def replenish() -> None:
            if cls._sem is None:
                return
            cls._sem.release()
            with cls._lock:
                if cls._timer is not None and cls._timer.is_alive():
                    cls._timer.cancel()
                cls._timer = threading.Timer(cls.MIN_INTERVAL, replenish)
                cls._timer.daemon = True
                cls._timer.name = "write_rate_limiter_replenisher"
                cls._timer.start()

        with cls._lock:
            if cls._timer is not None and cls._timer.is_alive():
                cls._timer.cancel()
            cls._timer = threading.Timer(cls.MIN_INTERVAL, replenish)
            cls._timer.daemon = True
            cls._timer.name = "write_rate_limiter_replenisher"
            cls._timer.start()

    @classmethod
    def close(cls) -> None:
        """Stop the replenisher. Call at application shutdown."""
        with cls._lock:
            if cls._timer is not None:
                cls._timer.cancel()
                cls._timer = None


# ─── Valid intervals ────────────────────────────────────────────────────────────

VALID_INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"}


def _interval_to_ms(interval: str) -> int:
    """Convert interval string to milliseconds."""
    multipliers = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    unit = interval[-1]
    value = int(interval[:-1])
    return value * multipliers.get(unit, 1) * 1000

def fetch_klines(
    symbol: str = "BTCUSDT",
    interval: str = "1h",
    limit: int = 200,
    retries: int = 3,
    market: str = "futures",
    start_time: int = None,
    end_time: int = None,
) -> List[dict]:
    """
    Fetch OHLCV klines from Binance API with optional date range.

    Args:
        symbol: Trading pair (e.g., 'BTCUSDT')
        interval: Timeframe (e.g., '1h', '4h', '1d', '5m', '15m', '30m')
        limit: Number of candles per API call (max 1000)
        retries: Number of retry attempts on failure
        market: 'futures' (default) or 'spot'
        start_time: Start timestamp in ms (optional)
        end_time: End timestamp in ms (optional)

    Returns:
        List of candles as dicts with keys: open_time, open, high, low, close, volume, close_time
    """
    # Normalise and validate inputs
    symbol = symbol.upper().strip()
    interval = interval.lower().strip()
    if interval not in VALID_INTERVALS:
        raise ValueError(f"Invalid interval '{interval}'. Valid: {sorted(VALID_INTERVALS)}")
    # Binance API caps at 1000 per call; we handle pagination internally
    limit = max(1, min(limit, 1000))

    # Check Local Data Lake First
    local_file = Path(f"data/historical/{symbol}_{interval}.csv")
    if local_file.exists():
        try:
            df = pd.read_csv(local_file)
            
            if 'timestamp' in df.columns:
                df = df.sort_values('timestamp')
            elif 'open_time' in df.columns:
                df = df.sort_values('open_time')
            
            # Simple fallback to limit logic - taking last `limit` rows
            # In a full backtest, you'd want start/end filtering here
            # Ensure timestamp column is numeric (ms) for comparison
            ts_col = df['timestamp']
            if ts_col.dtype == 'object' or str(ts_col.dtype).startswith('datetime'):
                df['ts_ms'] = pd.to_datetime(ts_col, utc=True).astype('int64') // 1_000_000
            else:
                df['ts_ms'] = ts_col.astype('int64')

            if start_time:
                df = df[df['ts_ms'] >= start_time]
            if end_time:
                df = df[df['ts_ms'] <= end_time]
            df_slice = df.tail(limit) if limit else df

            timestamps = df_slice['timestamp'].values
            opens = df_slice['open'].values
            highs = df_slice['high'].values
            lows = df_slice['low'].values
            closes = df_slice['close'].values
            volumes = df_slice['volume'].values
            close_times = df_slice['close_time'].values if 'close_time' in df_slice.columns else None

            candles = []
            for i in range(len(timestamps)):
                ts = timestamps[i]
                if isinstance(ts, str):
                    ts = int(pd.Timestamp(ts).timestamp() * 1000)
                ct = int(close_times[i]) if close_times is not None and pd.notna(close_times[i]) else int(ts) + 60000
                candles.append({
                    "open_time": ts,
                    "open": float(opens[i]),
                    "high": float(highs[i]),
                    "low": float(lows[i]),
                    "close": float(closes[i]),
                    "volume": float(volumes[i]),
                    "close_time": ct,
                    "quote_volume": float(df_slice['quote_asset_volume'].values[i]) if 'quote_asset_volume' in df_slice.columns else 0.0,
                    "trades": int(df_slice['number_of_trades'].values[i]) if 'number_of_trades' in df_slice.columns else 0,
                })
            
            print(f"[DATA LAKE] Loaded {len(candles)} candles from local cache for {symbol} {interval}")
            return candles
            
        except (FileNotFoundError, KeyError, ValueError) as e:
            print(f"[DATA LAKE ERROR] Failed to load local cache for {symbol} {interval}: {e}. Falling back to API.")

    # Fallback to Binance API with pagination support
    if market == "futures":
        url = f"{config.BINANCE_FUTURES_URL}/fapi/v1/klines"
    else:
        url = f"{config.BINANCE_BASE_URL}/api/v3/klines"

    all_candles = []
    current_start = start_time
    batch_limit = min(limit, 1000)  # API max is 1000
    max_iterations = 1000
    iteration = 0

    while True:
        iteration += 1
        if iteration > max_iterations:
            print(f"[WARNING] Pagination hit max_iterations={max_iterations} for {symbol} {interval}. "
                  "Possible edge case or malformed data. Stopping to prevent infinite loop.")
            break
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": batch_limit,
        }
        if current_start:
            params["startTime"] = current_start
        if end_time:
            params["endTime"] = end_time

        raw_batch = None
        for attempt in range(retries):
            try:
                RateLimiter.wait()
                response = requests.get(url, params=params, timeout=20)

                # FIX #46: Handle HTTP 429 (rate limit exceeded) with Retry-After backoff.
                # Binance may return Retry-After (seconds) or Weight-Limit-Exceeded.
                # Respect the server's backoff signal instead of crashing.
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    print(f"[RATE LIMIT] HTTP 429 received. Retrying after {retry_after}s (attempt {attempt + 1}/{retries}).")
                    if attempt < retries - 1:
                        time.sleep(retry_after)
                        continue
                    else:
                        raise Exception(f"Rate limit (HTTP 429) persisted after {retries} attempts. "
                                        "Reduce request frequency or use local data lake.")

                response.raise_for_status()
                raw_batch = response.json()
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise Exception(f"Failed to fetch klines after {retries} attempts: {e}")
            except json.JSONDecodeError as e:
                raise Exception(f"Invalid JSON response from Binance: {e}")

        if not isinstance(raw_batch, list) or len(raw_batch) == 0:
            break  # No more data

        for row in raw_batch:
            if not isinstance(row, list) or len(row) < 9:
                continue
            try:
                all_candles.append({
                    "open_time": row[0],
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "close_time": row[6],
                    "quote_volume": float(row[7]),
                    "trades": row[8],
                })
            except (ValueError, TypeError):
                continue

        # Pagination: if we got a full batch, there might be more data
        last_close_time = raw_batch[-1][6]

        if len(raw_batch) < batch_limit:
            break  # Received fewer than max = last batch
        # Break if we've fetched past the end of the requested range
        if end_time and last_close_time >= end_time:
            break  # Reached end of requested range
        # Use last candle's close_time + 1ms as next start for pagination
        next_start = last_close_time + 1
        if current_start and next_start <= current_start:
            break  # Safety guard against infinite loop
        current_start = next_start

    return all_candles


def get_price_data(
    symbol: str,
    timeframes: List[str] = None,
    market: str = "futures"
) -> dict:
    """
    Fetch price data for multiple timeframes.

    Args:
        symbol: Trading pair
        timeframes: List of timeframes (default: ['1h', '4h', '1d'])
        market: 'futures' or 'spot'

    Returns:
        Dict mapping timeframe to list of candles
    """
    if timeframes is None:
        timeframes = ["1h", "4h", "1d"]

    def fetch_tf(tf: str) -> tuple:
        try:
            return tf, fetch_klines(symbol, tf, config.DEFAULT_LIMIT, market=market)
        except Exception as e:
            print(f"Error fetching {symbol} {tf}: {e}")
            return tf, []

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(timeframes)) as executor:
        results = executor.map(fetch_tf, timeframes)

    return dict(results)


def get_current_price(symbol: str = "BTCUSDT", market: str = "futures") -> Optional[float]:
    """
    Get current price for a symbol.

    Args:
        symbol: Trading pair
        market: 'futures' or 'spot'

    Returns:
        Current price or None on error
    """
    if market == "futures":
        url = f"{config.BINANCE_FUTURES_URL}/fapi/v1/ticker/price"
    else:
        url = f"{config.BINANCE_BASE_URL}/api/v3/ticker/price"
    params = {"symbol": symbol}

    try:
        # ── #21: Route through RateLimiter to prevent 429 on price queries ──────
        RateLimiter.wait()
        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()
        return float(response.json()["price"])
    except Exception as e:
        print(f"Error fetching price for {symbol}: {e}")
        return None


def get_24h_stats(symbol: str = "BTCUSDT", market: str = "futures") -> Optional[dict]:
    """
    Get 24h trading stats.

    Args:
        symbol: Trading pair
        market: 'futures' or 'spot'

    Returns:
        Dict with priceChangePercent, volume, highPrice, lowPrice, etc. or None on error
    """
    if market == "futures":
        url = f"{config.BINANCE_FUTURES_URL}/fapi/v1/ticker/24hr"
    else:
        url = f"{config.BINANCE_BASE_URL}/api/v3/ticker/24hr"
    params = {"symbol": symbol}

    try:
        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()

        data = response.json()
        return {
            "price_change_pct": float(data["priceChangePercent"]),
            "volume": float(data["volume"]),
            "quote_volume": float(data["quoteVolume"]),
            "high_price": float(data["highPrice"]),
            "low_price": float(data["lowPrice"]),
            "last_price": float(data["lastPrice"]),
        }
    except Exception as e:
        print(f"Error fetching 24h stats for {symbol}: {e}")
        return None
