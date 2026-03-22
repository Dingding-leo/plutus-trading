"""
OKX API client for fetching historical OHLCV data.
"""

import random
import requests
import time
from typing import Optional, List
from datetime import datetime, timedelta
from .. import config


class OKXClient:
    """OKX API Client for historical data."""

    BASE_URL = "https://www.okx.com"

    def __init__(self, api_key: str = None, secret: str = None, password: str = None):
        self.api_key = api_key
        self.secret = secret
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "Plutus/1.0"
        })

    def _get_bar_url(self) -> str:
        return f"{self.BASE_URL}/api/v5/market/history-candles"

    def get_candles(
        self,
        instId: str,
        bar: str = "1h",
        limit: int = 100,
        after: int = None,
        before: int = None
    ) -> list:
        """
        Get historical candles from OKX.

        Args:
            instId: Instrument ID (e.g., 'BTC-USDT')
            bar: Timeframe (1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w)
            limit: Number of candles (max 100)
            after: Cursor for after (older)
            before: Cursor for before (newer)

        Returns:
            List of candles [timestamp, open, high, low, close, volume, quote_volume]
        """
        # OKX uses lowercase: 1h, 15m, 1d, etc.
        bar = bar.lower()

        url = self._get_bar_url()
        params = {
            "instId": instId,
            "bar": bar,
            "limit": min(limit, 100),
        }

        if after:
            params["after"] = after
        if before:
            params["before"] = before

        response = self.session.get(url, params=params, timeout=30)

        if response.status_code == 429:
            raise requests.exceptions.HTTPError("OKX rate limited")
        if response.status_code >= 500:
            raise requests.exceptions.HTTPError(f"OKX server error: {response.status_code}")

        data = response.json()

        if data.get("code") != "0":
            raise requests.exceptions.HTTPError(f"OKX API Error: {data.get('msg', 'Unknown error')}")

        return data.get("data", [])

    def fetch_history(
        self,
        instId: str,
        bar: str = "1h",
        start_time: int = None,
        end_time: int = None,
        max_candles: int = None
    ) -> List[dict]:
        """
        Fetch historical data with time range.

        Args:
            instId: Instrument ID (e.g., 'BTC-USDT')
            bar: Timeframe
            start_time: Start timestamp (ms)
            end_time: End timestamp (ms)
            max_candles: Maximum candles to fetch

        Returns:
            List of candle dicts
        """
        all_candles = []
        current_after = None

        # Convert to ms if datetime
        if isinstance(start_time, datetime):
            start_time = int(start_time.timestamp() * 1000)
        if isinstance(end_time, datetime):
            end_time = int(end_time.timestamp() * 1000)

        # Prevent infinite loops
        max_iterations = 1000
        iterations = 0

        while True:
            iterations += 1
            if iterations > max_iterations:
                break

            # Fetch candles
            try:
                if end_time:
                    candles = self.get_candles(instId, bar, 100, before=end_time)
                else:
                    candles = self.get_candles(instId, bar, 100, after=current_after)
            except requests.exceptions.HTTPError as e:
                print(f"OKX API error: {e}. Stopping fetch.")
                break

            if not candles:
                break

            # Parse candles
            for c in candles:
                # OKX /api/v5/market/history-candels returns ts[0] in milliseconds (int).
                # Normalize to ms explicitly so the contract is unambiguous regardless of
                # endpoint version — if OKX ever switches to seconds, the // operator
                # floors the float and produces the same ms integer, making the mismatch
                # surface immediately as wildly wrong timestamps rather than silently.
                ts_raw = c[0]
                ts = int(ts_raw) if int(ts_raw) > 10**12 else int(ts_raw) * 1000
                candle = {
                    "timestamp": ts,
                    # ts is always ms; datetime.fromtimestamp needs seconds → divide by 1000.
                    "datetime": datetime.fromtimestamp(ts / 1000),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5]),
                    "quote_volume": float(c[6]),
                }

                # Filter by time range
                if start_time and ts < start_time:
                    continue
                if end_time and ts > end_time:
                    continue

                all_candles.append(candle)

            # Update cursor
            current_after = candles[-1][0]

            # Check limits
            if max_candles and len(all_candles) >= max_candles:
                break

            # Stop if we've gone past start time
            if start_time and int(candles[-1][0]) < start_time:
                break

            # Small delay to avoid rate limits (jitter-based backoff)
            time.sleep(0.2 * (0.5 + random.random()))

        # Sort by timestamp
        all_candles.sort(key=lambda x: x["timestamp"])
        return all_candles


# Default client instance
okx_client = OKXClient()


def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    start_date: str = None,
    end_date: str = None,
    max_candles: int = None
) -> List[dict]:
    """
    Fetch OHLCV data from OKX.

    Args:
        symbol: Trading pair (e.g., 'BTC-USDT', 'ETH-USDT')
        timeframe: Timeframe (1m, 5m, 15m, 30m, 1h, 4h, 1d)
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        max_candles: Maximum candles to fetch

    Returns:
        List of candle dicts
    """
    # Parse dates
    start_time = None
    end_time = None

    if start_date:
        start_time = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000)
    if end_date:
        end_time = int(datetime.strptime(end_date, "%Y-%m-%d").timestamp() * 1000)

    # OKX uses different symbols
    symbol_map = {
        "BTCUSDT": "BTC-USDT",
        "ETHUSDT": "ETH-USDT",
        "SOLUSDT": "SOL-USDT",
    }

    inst_id = symbol_map.get(symbol, symbol.replace("USDT", "-USDT"))

    return okx_client.fetch_history(
        inst_id,
        timeframe,
        start_time,
        end_time,
        max_candles
    )
