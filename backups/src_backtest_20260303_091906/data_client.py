"""
Unified data client for backtesting - supports both OKX and Binance with proper historical fetching.
"""

from typing import List, Dict
from datetime import datetime, timedelta
import requests
import time
import os
import json

from ..data import binance_client


# Symbol mapping: OKX format -> Binance format
SYMBOL_MAP = {
    "BTC-USDT": "BTCUSDT",
    "ETH-USDT": "ETHUSDT",
    "SOL-USDT": "SOLUSDT",
    "DOGE-USDT": "DOGEUSDT",
    "XRP-USDT": "XRPUSDT",
    "ADA-USDT": "ADAUSDT",
    "AVAX-USDT": "AVAXUSDT",
    "DOT-USDT": "DOTUSDT",
    "MATIC-USDT": "MATICUSDT",
    "LINK-USDT": "LINKUSDT",
    "ATOM-USDT": "ATOMUSDT",
    "UNI-USDT": "UNIUSDT",
    "LTC-USDT": "LTCUSDT",
    "ETC-USDT": "ETCUSDT",
    "XLM-USDT": "XLMUSDT",
    "NEAR-USDT": "NEARUSDT",
    "APT-USDT": "APTUSDT",
    "ARB-USDT": "ARBUSDT",
    "OP-USDT": "OPUSDT",
    "SUI-USDT": "SUIUSDT",
    "PEPE-USDT": "PEPEUSDT",
    "WIF-USDT": "WIFUSDT",
    "BONK-USDT": "BONKUSDT",
    "FTM-USDT": "FTMUSDT",
    "SEI-USDT": "SEIUSDT",
    "TIA-USDT": "TIAUSDT",
    "INJ-USDT": "INJUSDT",
    "RENDER-USDT": "RENDERUSDT",
    "FET-USDT": "FETUSDT",
    "IMX-USDT": "IMXUSDT",
}


def normalize_symbol(symbol: str) -> str:
    """Convert symbol to Binance format."""
    if symbol in SYMBOL_MAP:
        return SYMBOL_MAP[symbol]
    return symbol.replace("-", "")


# Cache directory for historical data
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data_cache")


class DataClient:
    """Unified data client that can fetch from multiple sources."""

    def __init__(self, use_cache: bool = True):
        self.use_cache = use_cache
        os.makedirs(CACHE_DIR, exist_ok=True)

    def _get_cache_path(self, symbol: str, timeframe: str) -> str:
        return os.path.join(CACHE_DIR, f"{symbol}_{timeframe}.json")

    def _load_from_cache(self, symbol: str, timeframe: str, start_ts: int, end_ts: int) -> List[dict]:
        cache_path = self._get_cache_path(symbol, timeframe)
        if not os.path.exists(cache_path):
            return None
        try:
            with open(cache_path, 'r') as f:
                data = json.load(f)
            filtered = [c for c in data if start_ts <= c['timestamp'] <= end_ts]
            return filtered
        except:
            return None

    def _save_to_cache(self, symbol: str, timeframe: str, data: List[dict]):
        if not self.use_cache:
            return
        cache_path = self._get_cache_path(symbol, timeframe)
        try:
            with open(cache_path, 'w') as f:
                json.dump(data, f)
        except:
            pass

    def get_candles(self, symbol: str, timeframe: str, limit: int = 100) -> List[dict]:
        binance_symbol = normalize_symbol(symbol)
        candles = binance_client.fetch_klines(binance_symbol, timeframe, limit)
        return [
            {
                "timestamp": c["open_time"],
                "datetime": datetime.fromtimestamp(c["open_time"] / 1000),
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "volume": c["volume"],
                "quote_volume": c.get("quote_volume", 0),
            }
            for c in candles
        ]

    def fetch_history(
        self,
        symbol: str,
        timeframe: str,
        start_date: str = None,
        end_date: str = None,
        max_candles: int = None
    ) -> List[dict]:
        if end_date:
            end_ts = int(datetime.strptime(end_date, "%Y-%m-%d").timestamp() * 1000)
        else:
            end_ts = int(datetime.now().timestamp() * 1000)

        if start_date:
            start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000)
        else:
            start_ts = int((datetime.now() - timedelta(days=90)).timestamp() * 1000)

        if self.use_cache:
            cached = self._load_from_cache(symbol, timeframe, start_ts, end_ts)
            if cached:
                print(f"  [CACHED] {symbol} {timeframe}")
                return cached

        binance_symbol = normalize_symbol(symbol)
        all_candles = []
        max_per_request = 1000
        current_ts = start_ts

        while current_ts < end_ts:
            url = f"https://api.binance.com/api/v3/klines"
            params = {
                "symbol": binance_symbol,
                "interval": timeframe,
                "startTime": current_ts,
                "endTime": end_ts,
                "limit": max_per_request,
            }

            try:
                resp = requests.get(url, params=params, timeout=30)
                data = resp.json()

                if isinstance(data, dict) and data.get('code'):
                    print(f"  [ERROR] {symbol} {timeframe}: {data.get('msg', 'Unknown error')}")
                    break

                if not data:
                    break

                for row in data:
                    all_candles.append({
                        "timestamp": int(row[0]),
                        "datetime": datetime.fromtimestamp(row[0] / 1000),
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                        "quote_volume": float(row[7]) if len(row) > 7 else 0,
                    })

                last_ts = data[-1][0]
                if last_ts <= current_ts:
                    break
                current_ts = last_ts + 1

                if max_candles and len(all_candles) >= max_candles:
                    break
                time.sleep(0.1)

            except Exception as e:
                print(f"  [ERROR] {symbol} {timeframe}: {e}")
                break

        all_candles.sort(key=lambda x: x["timestamp"])

        if max_candles and len(all_candles) > max_candles:
            all_candles = all_candles[-max_candles:]

        if all_candles:
            self._save_to_cache(symbol, timeframe, all_candles)

        return all_candles


data_client = DataClient()


def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    start_date: str = None,
    end_date: str = None,
    max_candles: int = None
) -> List[dict]:
    return data_client.fetch_history(symbol, timeframe, start_date, end_date, max_candles)


def get_all_okx_futures() -> List[str]:
    """Get list of all USDT-margined futures from OKX."""
    url = "https://www.okx.com/api/v5/market/tickers"
    params = {"instType": "SWAP"}

    try:
        resp = requests.get(url, params=params, timeout=30)
        data = resp.json()

        if data.get("code") != "0":
            return []

        symbols = []
        for item in data.get("data", []):
            inst_id = item.get("instId", "")
            # Filter for USDT-margined perpetual swaps
            if inst_id.endswith("-USDT-SWAP"):
                symbol = inst_id.replace("-USDT-SWAP", "-USDT")
                symbols.append(symbol)

        return sorted(symbols)
    except Exception as e:
        print(f"Error fetching OKX futures: {e}")
        return []


COMMON_FUTURES = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "DOGE-USDT", "XRP-USDT",
    "ADA-USDT", "AVAX-USDT", "DOT-USDT", "MATIC-USDT", "LINK-USDT",
    "ATOM-USDT", "UNI-USDT", "LTC-USDT", "ETC-USDT", "XLM-USDT",
    "NEAR-USDT", "APT-USDT", "ARB-USDT", "OP-USDT", "SUI-USDT",
    "PEPE-USDT", "WIF-USDT", "BONK-USDT", "FTM-USDT", "SEI-USDT",
    "TIA-USDT", "INJ-USDT", "RENDER-USDT", "FET-USDT", "IMX-USDT",
]
