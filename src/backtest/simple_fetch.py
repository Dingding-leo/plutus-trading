"""
Simple historical data fetcher.
"""

import requests
from datetime import datetime, timedelta
from typing import List


def fetch_binance_history(
    symbol: str,
    timeframe: str,
    start_date: str,
    end_date: str = None,
    max_candles: int = None
) -> List[dict]:
    """Fetch historical candles from Binance."""

    # Parse dates
    start = datetime.strptime(start_date, "%Y-%m-%d")
    if end_date:
        end = datetime.strptime(end_date, "%Y-%m-%d")
    else:
        end = datetime.now()

    start_ts = int(start.timestamp() * 1000)
    end_ts = int(end.timestamp() * 1000)

    all_candles = []
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
                # Handle both string and int timestamps
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

    return all_candles
