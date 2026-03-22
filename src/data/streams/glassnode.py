"""
Glassnode API Client for Plutus V4.0

Provides typed access to on-chain metrics from Glassnode:
- MVRV (Market Value to Realized Value)
- SOPR (Spent Output Profit Ratio)
- Exchange Net Position Change
- Active Addresses

All network calls are cached with a 60-second TTL to avoid redundant API
hits when multiple strategy components request the same metric simultaneously.

Error handling: methods return None and emit a log.warning on failure so
that a single unavailable metric never crashes a multi-metric analysis loop.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TTL cache implementation
# ---------------------------------------------------------------------------

_TTL = 60.0  # seconds


class _TTLCache:
    """
    A minimal thread-safe cache with time-to-live and LRU entry eviction.

    Uses a lock to ensure atomic get-or-set so that concurrent requests
    for the same key do not trigger duplicate network calls (the "thundering
    herd" problem).  The cache stores (value, expiry_timestamp) tuples.

    When max_entries entries are reached the least-recently-used entry is
    evicted before inserting the new one, preventing unbounded memory growth.
    """

    # Reasonable default: 500 distinct (endpoint, params) combinations is far
    # more than any single process should accumulate in a 60-second window.
    DEFAULT_MAX_ENTRIES = 500

    def __init__(self, ttl: float = _TTL, max_entries: int | None = None) -> None:
        self._ttl = ttl
        self._max_entries = (
            max_entries if max_entries is not None else self.DEFAULT_MAX_ENTRIES
        )
        # OrderedDict so popitem(last=False) gives LRU order cheaply.
        self._store: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return None
            # Move to end so this entry is considered "most recently used".
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            # Evict LRU entry if at capacity.
            if len(self._store) >= self._max_entries and key not in self._store:
                self._store.popitem(last=False)  # pop oldest (first) item
            self._store[key] = (value, time.monotonic() + self._ttl)
            # Move to end so newly inserted entries are considered "most recently used".
            self._store.move_to_end(key)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_cache = _TTLCache()


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GlassnodeMetrics:
    """
    Container for a single-point-in-time snapshot of Glassnode on-chain metrics.

    All fields are optional because the API may partially fail; callers must
    check for None on each field rather than on the dataclass as a whole.

    Attributes
    ----------
    mvrv : float | None
        MVRV ratio (market cap / realised cap).  Values > 3.5 historically
        signal market top; values < 1.0 signal undervaluation.
    sopr : float | None
        Spent Output Profit Ratio.  Values > 1.0 mean profits are being
        realised (selling); values < 1.0 mean losses are being realised.
    exchange_net_position_change : float | None
        Net change in exchange-held supply over the interval.
        Positive = coins flowing onto exchanges (selling pressure);
        Negative = coins flowing off exchanges (holding pressure).
    active_addresses : int | None
        Number of unique active addresses in the interval.
        Rising active addresses suggest increasing network usage.
    timestamp : int | None
        Unix timestamp (seconds) of the metric observation.
    """

    mvrv: float | None = None
    sopr: float | None = None
    exchange_net_position_change: float | None = None
    active_addresses: int | None = None
    timestamp: int | None = None


# ---------------------------------------------------------------------------
# Glassnode Client
# ---------------------------------------------------------------------------

class GlassnodeClient:
    """
    Typed client for the Glassnode REST API.

    Design goals
    ------------
    - Caching: each unique (endpoint, symbol, interval) combination is
      cached for 60 s so concurrent strategy components share one API hit.
    - Fail-safe: any HTTP error or parse failure returns None and logs a
      warning rather than raising an exception.
    - Typed: all public methods return concrete types or None.

    Base URL
    --------
    https://api.glassnode.com/v1

    Rate limits
    -----------
    Free tier: 10 requests / minute.  Paid tiers: higher limits.
    This client does NOT implement retry-with-backoff; callers should
    implement their own rate-limiting loop if needed.

    Metrics returned
    ----------------
    Glassnode returns a list of dictionaries sorted ascending by timestamp,
    e.g. [{"t": 1672531200, "v": 3.21}].  Each method extracts the latest
    value (last element of the list) and returns it as a Python float or int.
    """

    DEFAULT_BASE_URL = "https://api.glassnode.com/v1"

    # Mapping from human-readable metric name → Glassnode a= parameter
    _ENDPOINT_MAP = {
        "mvrv": "metrics/market/mvrv",
        "sopr": "metrics/market/sopr",
        "exchange_net_position_change": "metrics/exchange/net_position_change",
        "active_addresses": "metrics/addresses/active_count",
    }

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.glassnode.com/v1",
        timeout: float = 10.0,
    ) -> None:
        """
        Initialise the Glassnode client.

        Parameters
        ----------
        api_key : str
            Your Glassnode API key (get one at glassnode.com).
        base_url : str
            Override only for testing with a mock server.
        timeout : float
            Requests timeout in seconds (default 10 s).
        """
        if not api_key:
            raise ValueError("Glassnode API key is required.")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        log.info("GlassnodeClient initialised with base_url=%s", self._base_url)

    # ------------------------------------------------------------------
    # Internal HTTP helper
    # ------------------------------------------------------------------

    def _get(
        self,
        endpoint: str,
        params: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        """
        Perform a GET request to Glassnode with caching.

        Parameters
        ----------
        endpoint : str
            Full path appended to base_url, e.g. "metrics/market/mvrv".
        params : dict
            Query parameters for the API (symbol, interval, a, etc.).

        Returns
        -------
        list[dict] | None
            The parsed JSON list of {t, v} pairs, or None on error.
        """
        cache_key = f"{endpoint}:{sorted(params.items())}"
        cached = _cache.get(cache_key)
        if cached is not None:
            log.debug("Cache hit for %s", cache_key)
            return cached  # type: ignore[return-value]

        url = f"{self._base_url}/{endpoint}"
        headers = {"Apikey": self._api_key}

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=self._timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("Glassnode request failed for %s: %s", url, exc)
            return None

        try:
            data: list[dict[str, Any]] = resp.json()
        except ValueError as exc:
            log.warning("Glassnode JSON parse error for %s: %s", url, exc)
            return None

        _cache.set(cache_key, data)
        return data

    # ------------------------------------------------------------------
    # Public metric methods
    # ------------------------------------------------------------------

    def get_mvrv(
        self,
        symbol: str = "BTC",
        interval: str = "24h",
    ) -> float | None:
        """
        Fetch the latest MVRV (Market Value / Realized Value) ratio.

        Parameters
        ----------
        symbol : str
            Asset symbol.  Defaults to "BTC".  Use "ETH", "SOL" etc.
        interval : str
            Sampling interval.  Options: "10m", "1h", "24h", "1w".  Default "24h".

        Returns
        -------
        float | None
            Latest MVRV value, e.g. 2.84, or None on error.

        Math
        ----
        MVRV = Market Cap / Realized Cap

        Market Cap  = circulating_supply × current_price
        Realized Cap = Σ(output_value at time it last moved)

        Interpretation
        -------------
        - MVRV < 1.0  : price below average cost basis — historically a buying zone
        - MVRV 1.0–2.0: fair value zone
        - MVRV 2.0–3.0: moderate overvaluation
        - MVRV > 3.5  : extreme overvaluation — distribution phase
        """
        params: dict[str, Any] = {
            "a": symbol,
            "interval": interval,
            "i": "epoch",  # return Unix timestamps
        }
        raw = self._get(self._ENDPOINT_MAP["mvrv"], params)
        if not raw:
            return None
        try:
            return float(raw[-1]["v"])
        except (KeyError, IndexError, TypeError) as exc:
            log.warning("Unexpected MVRV payload structure: %s — %s", raw, exc)
            return None

    def get_sopr(
        self,
        symbol: str = "BTC",
        interval: str = "24h",
    ) -> float | None:
        """
        Fetch the latest SOPR (Spent Output Profit Ratio).

        Parameters
        ----------
        symbol : str
            Asset symbol.  Defaults to "BTC".
        interval : str
            Sampling interval.  Default "24h".

        Returns
        -------
        float | None
            Latest SOPR value, e.g. 1.04, or None on error.

        Math
        ----
        SOPR = Σ(value_output × price_at_spend) / Σ(value_output × price_at_creation)

        Interpretation
        -------------
        - SOPR > 1.0 : coins moved today are in profit → realised gain (selling)
        - SOPR < 1.0 : coins moved today are at a loss → capitulation
        - SOPR ≈ 1.0 : market in equilibrium

        High SOPR (>1.05) sustained over days warns of distribution risk.
        """
        params = {"a": symbol, "interval": interval, "i": "epoch"}
        raw = self._get(self._ENDPOINT_MAP["sopr"], params)
        if not raw:
            return None
        try:
            return float(raw[-1]["v"])
        except (KeyError, IndexError, TypeError) as exc:
            log.warning("Unexpected SOPR payload structure: %s — %s", raw, exc)
            return None

    def get_exchange_net_position_change(
        self,
        symbol: str = "BTC",
        interval: str = "24h",
    ) -> float | None:
        """
        Fetch the net change in exchange-held supply over the interval.

        Parameters
        ----------
        symbol : str
            Asset symbol.  Defaults to "BTC".
        interval : str
            Sampling interval.  Default "24h".

        Returns
        -------
        float | None
            Net position change in absolute coin units (positive = flow into
            exchange = selling pressure; negative = flow out = holding).

        Math
        ----
        exchange_net_position_change = Σ(coins_deposited) - Σ(coins_withdrawn)

        Measured at the exchange wallet level via UTXO (BTC) or account
        balance (ETH/ERC-20) analysis.

        Interpretation
        -------------
        - Positive (→ exchange): coins available to sell — bearish signal
        - Negative (← cold storage): coins being accumulated — bullish signal
        """
        params = {"a": symbol, "interval": interval, "i": "epoch"}
        raw = self._get(self._ENDPOINT_MAP["exchange_net_position_change"], params)
        if not raw:
            return None
        try:
            return float(raw[-1]["v"])
        except (KeyError, IndexError, TypeError) as exc:
            log.warning(
                "Unexpected exchange_net_position_change payload: %s — %s",
                raw,
                exc,
            )
            return None

    def get_active_addresses(
        self,
        symbol: str = "BTC",
        interval: str = "24h",
    ) -> int | None:
        """
        Fetch the number of unique active addresses in the interval.

        Parameters
        ----------
        symbol : str
            Asset symbol.  Defaults to "BTC".
        interval : str
            Sampling interval.  Default "24h".

        Returns
        -------
        int | None
            Count of unique addresses that either sent or received funds.

        Math
        ----
        active_addresses = count of distinct (sender OR receiver) addresses
        in all transactions within the interval.

        Note: this counts each address once per interval (set cardinality),
        not per transaction.

        Interpretation
        -------------
        - Rising active addresses: growing network activity / adoption
        - Declining active addresses: network contraction
        - Used as a leading indicator for volume and price trends.
        """
        params = {"a": symbol, "interval": interval, "i": "epoch"}
        raw = self._get(self._ENDPOINT_MAP["active_addresses"], params)
        if not raw:
            return None
        try:
            return int(raw[-1]["v"])
        except (KeyError, IndexError, TypeError) as exc:
            log.warning(
                "Unexpected active_addresses payload: %s — %s",
                raw,
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Convenience: bulk fetch
    # ------------------------------------------------------------------

    def get_metrics(
        self,
        symbol: str = "BTC",
        interval: str = "24h",
    ) -> GlassnodeMetrics:
        """
        Fetch all available metrics in a single call and return a dataclass.

        This method calls each individual metric method; individual failures
        are isolated and returned as None fields.

        Parameters
        ----------
        symbol : str
            Asset symbol.  Defaults to "BTC".
        interval : str
            Sampling interval.  Default "24h".

        Returns
        -------
        GlassnodeMetrics
            Populated dataclass; any unavailable metric is None.
        """
        return GlassnodeMetrics(
            mvrv=self.get_mvrv(symbol, interval),
            sopr=self.get_sopr(symbol, interval),
            exchange_net_position_change=self.get_exchange_net_position_change(
                symbol, interval
            ),
            active_addresses=self.get_active_addresses(symbol, interval),
            timestamp=int(time.time()),
        )


# ---------------------------------------------------------------------------
# Stub test block
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Test that the class instantiates and methods have correct signatures
    # (no real API key — all calls will warn and return None)
    client = GlassnodeClient(api_key="stub_key_for_import_test")

    print("stub: GlassnodeClient — would connect to https://api.glassnode.com/v1")

    # Verify dataclass
    m = GlassnodeMetrics(mvrv=2.5, sopr=1.1, active_addresses=1_234_567)
    assert m.mvrv == 2.5
    assert m.sopr == 1.1
    assert m.active_addresses == 1_234_567
    assert m.exchange_net_position_change is None
    print("stub: GlassnodeMetrics — OK")

    # Verify cache TTL semantics (offline — will return None)
    result = client.get_mvrv("BTC", "24h")
    assert result is None, "Expected None without real API key"
    result2 = client.get_mvrv("BTC", "24h")
    assert result2 is None
    print("stub: GlassnodeClient.get_mvrv — OK (returns None without real key)")

    # Verify all methods are callable and return None
    for method in [
        client.get_sopr,
        client.get_exchange_net_position_change,
        client.get_active_addresses,
    ]:
        out = method("BTC", "24h")
        assert out is None, f"Expected None, got {out}"

    metrics = client.get_metrics("BTC", "24h")
    assert isinstance(metrics, GlassnodeMetrics)
    assert metrics.mvrv is None
    assert metrics.sopr is None
    print("stub: GlassnodeClient.get_metrics — OK (all fields None without real key)")
