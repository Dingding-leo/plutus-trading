"""
Binance API client for fetching OHLCV data.
"""

import json
import requests
import time
from typing import Optional, List
from .. import config


def fetch_klines(
    symbol: str = "BTCUSDT",
    interval: str = "1h",
    limit: int = 200,
    retries: int = 3,
    market: str = "futures"
) -> List[dict]:
    """
    Fetch OHLCV klines from Binance API.

    Args:
        symbol: Trading pair (e.g., 'BTCUSDT')
        interval: Timeframe (e.g., '1h', '4h', '1d', '5m', '15m', '30m')
        limit: Number of candles to fetch
        retries: Number of retry attempts on failure
        market: 'futures' (default) or 'spot'

    Returns:
        List of candles as dicts with keys: open_time, open, high, low, close, volume, close_time
    """
    if market == "futures":
        url = f"{config.BINANCE_FUTURES_URL}/fapi/v1/klines"
    else:
        url = f"{config.BINANCE_BASE_URL}/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }

    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()

            raw_data = response.json()

            # Validate response is a list
            if not isinstance(raw_data, list):
                raise ValueError(f"Unexpected response type: {type(raw_data)}")

            # Convert to list of dicts with validation
            candles = []
            for row in raw_data:
                # Validate row has enough elements
                if not isinstance(row, list) or len(row) < 9:
                    continue
                try:
                    candles.append({
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
                except (ValueError, TypeError) as e:
                    # Skip malformed rows
                    continue

            return candles

        except requests.exceptions.RequestException as e:
            if attempt < retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff
                time.sleep(wait_time)
            else:
                raise Exception(f"Failed to fetch klines after {retries} attempts: {e}")
        except json.JSONDecodeError as e:
            raise Exception(f"Invalid JSON response from Binance: {e}")

    return []


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

    data = {}
    for tf in timeframes:
        try:
            data[tf] = fetch_klines(symbol, tf, config.DEFAULT_LIMIT, market=market)
        except Exception as e:
            print(f"Error fetching {symbol} {tf}: {e}")
            data[tf] = []

    return data


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
        response = requests.get(url, params=params, timeout=10)
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
        response = requests.get(url, params=params, timeout=10)
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
