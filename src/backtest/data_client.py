"""
Unified data client for backtesting - supports both OKX and Binance with proper historical fetching.
"""

from typing import List, Dict, Optional
from datetime import datetime, timedelta
import requests
import time
import os
import json
import math

from .. import config
from ..data import binance_client
from ..data.coin_tiers import normalize_symbol


# ─── Timeframe helpers ──────────────────────────────────────────────────────────

def _interval_to_ms(interval: str) -> int:
    """Convert interval string (e.g. '1h', '5m', '1d') to milliseconds."""
    multipliers = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    unit = interval[-1]
    value = int(interval[:-1])
    return value * multipliers.get(unit, 1) * 1000


def _floor_to_complete_candle(ts_ms: int, interval: str) -> int:
    """
    Floor a timestamp to the start of its containing complete candle.

    CRITICAL: Never pass datetime.now() directly as end_ts — always floor to the
    last COMPLETE candle to avoid look-ahead bias. Backtests must never "see"
    candles whose close_time is in the future or equal to the current moment.
    """
    interval_ms = _interval_to_ms(interval)
    return (ts_ms // interval_ms) * interval_ms


def _now_ms() -> int:
    return int(time.time() * 1000)

# Binance API headers (required for fapi endpoints)
BINANCE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Cache directory for historical data
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data_cache")


class DataClient:
    """
    Unified data client that can fetch from multiple sources.

    Args:
        provider: Data source - 'binance' (default) or 'okx'
        use_cache: Whether to cache fetched data to disk
    """

    def __init__(self, provider: str = "binance", use_cache: bool = True):
        self.provider = provider.lower()
        self.use_cache = use_cache
        os.makedirs(CACHE_DIR, exist_ok=True)

        if self.provider == "okx":
            from ..data.okx_client import OKXClient
            self._okx_client = OKXClient()

    def _get_cache_path(self, symbol: str, timeframe: str, market: str = "futures") -> str:
        # Normalize symbol and include market so spot/futures don't collide
        normalized = normalize_symbol(symbol)
        return os.path.join(CACHE_DIR, f"{normalized}_{timeframe}_{market}.json")

    def _load_from_cache(self, symbol: str, timeframe: str, start_ts: int, end_ts: int, market: str = "futures") -> Optional[List[dict]]:
        cache_path = self._get_cache_path(symbol, timeframe, market)
        if not os.path.exists(cache_path):
            return None
        try:
            with open(cache_path, 'r') as f:
                data = json.load(f)

            # FIX #42: Never serve candles whose close_time has not yet passed.
            # The Binance API returns the current (incomplete) candle on every request.
            # Serving it from cache poisons backtests with look-ahead bias.
            # Filter out any candle where close_time >= current time.
            now_ms = _now_ms()
            interval_ms = _interval_to_ms(timeframe)
            # The "safe" boundary is the start of the current (potentially incomplete) candle.
            candle_boundary = _floor_to_complete_candle(now_ms, timeframe)
            # Only keep candles whose open_time ends before the current incomplete candle.
            filtered = [
                c for c in data
                if start_ts <= c.get('timestamp', 0) <= end_ts
                and c.get('timestamp', 0) < candle_boundary
            ]
            return filtered if filtered else None
        except Exception:
            return None

    def _save_to_cache(self, symbol: str, timeframe: str, data: List[dict], market: str = "futures"):
        if not self.use_cache:
            return
        # FIX #42 (save path): Strip any incomplete candles before persisting to disk.
        # This ensures cached data is always "clean" for future backtests.
        now_ms = _now_ms()
        candle_boundary = _floor_to_complete_candle(now_ms, timeframe)
        clean_data = [c for c in data if c.get('timestamp', 0) < candle_boundary]
        cache_path = self._get_cache_path(symbol, timeframe, market)
        try:
            with open(cache_path, 'w') as f:
                json.dump(clean_data, f)
        except Exception:
            pass

    def get_candles(self, symbol: str, timeframe: str, limit: int = 100, market: str = "futures") -> List[dict]:
        binance_symbol = normalize_symbol(symbol)
        candles = binance_client.fetch_klines(binance_symbol, timeframe, limit, market=market)
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
        max_candles: int = None,
        provider: str = None,
        market: str = "futures",
    ) -> List[dict]:
        """
        Fetch historical OHLCV data.

        Args:
            symbol: Trading pair (e.g., 'BTCUSDT', 'BTC-USDT')
            timeframe: Timeframe (e.g., '1h', '15m', '4h')
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            max_candles: Maximum candles to fetch
            provider: 'binance' or 'okx' (default: self.provider)
            market: 'futures' (default) or 'spot' — only for Binance provider

        Returns:
            List of candle dicts
        """
        provider = provider or self.provider

        if end_date:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            end_ts = int(end_dt.timestamp() * 1000)
        else:
            # FIX #41: Floor end_ts to the last COMPLETE candle boundary.
            # Using now() directly introduces look-ahead bias: the backtest would
            # accidentally include the current incomplete candle, whose close price
            # is not yet settled. Always floor to the completed period.
            end_ts = _floor_to_complete_candle(_now_ms(), timeframe)

        if start_date:
            start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000)
        else:
            start_ts = int((datetime.now() - timedelta(days=90)).timestamp() * 1000)

        if self.use_cache:
            cached = self._load_from_cache(symbol, timeframe, start_ts, end_ts, market=market)
            if cached:
                print(f"  [CACHED] {symbol} {timeframe} ({market})")
                return cached

        if provider == "okx":
            return self._fetch_okx(symbol, timeframe, start_ts, end_ts, max_candles)
        else:
            return self._fetch_binance(symbol, timeframe, start_ts, end_ts, max_candles=max_candles, market=market)

    def _fetch_binance(
        self,
        symbol: str,
        timeframe: str,
        start_ts: int,
        end_ts: int,
        max_candles: int = None,
        market: str = "futures",
    ) -> List[dict]:
        """Fetch from Binance spot or futures."""
        binance_symbol = normalize_symbol(symbol)
        all_candles = []
        current_ts = start_ts

        if market == "futures":
            base_url = config.BINANCE_FUTURES_URL  # https://fapi.binance.com
            path = "/fapi/v1/klines"
        else:
            base_url = config.BINANCE_BASE_URL  # https://api.binance.com
            path = "/api/v3/klines"

        retries = 3
        attempt = 0

        while current_ts < end_ts and attempt < retries:
            url = f"{base_url}{path}"
            params = {
                "symbol": binance_symbol,
                "interval": timeframe,
                "startTime": current_ts,
                "endTime": end_ts,
                "limit": 1000,
            }

            try:
                resp = requests.get(url, params=params, headers=BINANCE_HEADERS, timeout=30)

                # Rate-limit or invalid response — retry
                if resp.status_code != 200 or not resp.text.strip():
                    attempt += 1
                    wait = 2 ** attempt
                    time.sleep(wait)
                    continue

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
                attempt = 0  # Reset retries on success

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
            self._save_to_cache(symbol, timeframe, all_candles, market=market)

        return all_candles

    def _fetch_okx(
        self,
        symbol: str,
        timeframe: str,
        start_ts: int,
        end_ts: int,
        max_candles: int = None,
    ) -> List[dict]:
        """Fetch from OKX."""
        # Convert Binance symbol to OKX format
        n = normalize_symbol(symbol)
        okx_symbol = f"{n.replace('USDT', '-USDT')}"

        # Convert timeframe
        okx_tf = timeframe.lower()

        all_candles = []
        current_before = end_ts  # OKX 'before' param gets older data

        while current_before > start_ts:
            try:
                candles = self._okx_client.get_candles(
                    okx_symbol, okx_tf, limit=100, before=current_before
                )

                if not candles:
                    break

                for row in candles:
                    ts = int(row[0])
                    if ts < start_ts:
                        continue
                    if ts > end_ts:
                        continue
                    all_candles.append({
                        "timestamp": ts,
                        "datetime": datetime.fromtimestamp(ts / 1000),
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                        "quote_volume": float(row[6]),
                    })

                last_ts = int(candles[-1][0])
                if last_ts >= current_before:
                    break
                current_before = last_ts

                if max_candles and len(all_candles) >= max_candles:
                    break
                time.sleep(0.2)

            except Exception as e:
                print(f"  [ERROR] {symbol} {timeframe} (OKX): {e}")
                break

        all_candles.sort(key=lambda x: x["timestamp"])

        if max_candles and len(all_candles) > max_candles:
            all_candles = all_candles[-max_candles:]

        if all_candles:
            self._save_to_cache(symbol, timeframe, all_candles, market="okx")

        return all_candles


# Default: Binance USDT-M Futures
data_client = DataClient(provider="binance", use_cache=True)


def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    start_date: str = None,
    end_date: str = None,
    max_candles: int = None
) -> List[dict]:
    return data_client.fetch_history(symbol, timeframe, start_date, end_date, max_candles)


def get_all_okx_futures() -> List[str]:
    """
    Get list of all USDT-margined futures from OKX.

    Returns:
        List of symbols in Binance format (no hyphen), e.g., ['BTCUSDT', 'ETHUSDT']
    """
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
                raw_symbol = inst_id.replace("-USDT-SWAP", "-USDT")
                # Normalize to Binance format (no hyphen)
                symbol = normalize_symbol(raw_symbol)
                symbols.append(symbol)

        return sorted(set(symbols))
    except Exception as e:
        print(f"Error fetching OKX futures: {e}")
        return []


# ─── Survivorship Bias Configuration ────────────────────────────────────────────
#
# CRITICAL BUG (#6): COMMON_FUTURES excludes delisted/collapsed assets.
# This creates SURVIVORSHIP BIAS: backtests only see assets that survived to today.
#
# Impact:
# - Assets that collapsed (e.g., LUNA, FTX tokens, Luna Classic) are absent.
# - Assets that were delisted mid-period (e.g., older altcoins) are absent.
# - Win rates and Sharpe ratios are inflated because only "successful" assets
#   survive in the dataset.
#
# Set INCLUDE_HISTORICAL_ASSETS=True to use the full historical universe
# (including assets that no longer trade). This requires a historical data
# provider that preserves delisted symbols (e.g., CoinAPI, Kaiko, or a
# self-maintained dataset that stores historical ticker data).
#
# When INCLUDE_HISTORICAL_ASSETS=False (default), this list is restricted to
# assets currently trading on Binance — backtests are representative of
# TODAY's available universe, not the historical universe.
# ─────────────────────────────────────────────────────────────────────────────
INCLUDE_HISTORICAL_ASSETS: bool = False

# All common futures - NO HYPHEN format (Binance standard).
# NOTE: This list only includes assets that are STILL TRADING today.
# If INCLUDE_HISTORICAL_ASSETS=True, also query get_all_okx_futures() at
# runtime and union the results to capture assets that have since been delisted.
COMMON_FUTURES = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT",
    "ADAUSDT", "AVAXUSDT", "DOTUSDT", "MATICUSDT", "LINKUSDT",
    "ATOMUSDT", "UNIUSDT", "LTCUSDT", "ETCUSDT", "XLMUSDT",
    "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT",
    "PEPEUSDT", "WIFUSDT", "BONKUSDT", "FTMUSDT", "SEIUSDT",
    "TIAUSDT", "INJUSDT", "RENDERUSDT", "FETUSDT", "IMXUSDT",
]
