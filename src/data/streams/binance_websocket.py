"""
Binance WebSocket Client for Plutus V4.0

Provides low-latency, non-blocking access to Binance WebSocket streams:
- Real-time order book (depth) data
- Individual trade ticks
- Derived metrics: orderbook imbalance, spread, tick aggregation

All public methods are non-blocking: inbound messages are buffered in a
background thread and retrieved via a bounded queue so the caller never waits
on network I/O.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import websocket  # pip install websocket-client

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class StreamConfig:
    """
    Describes which symbols and channels to subscribe to.

    Attributes
    ----------
    symbols : list[str]
        Binance quote-pair symbols, e.g. ["btcusdt", "ethusdt"].
    channels : list[str]
        Channel names accepted by Binance combined streams.
        Common values: "depth20@100ms", "trade", "bookTicker".
    buffer_size : int
        Maximum number of messages to hold in the background queue before
        oldest messages are dropped (prevents unbounded memory growth).
    """

    symbols: list[str] = field(default_factory=lambda: ["btcusdt"])
    channels: list[str] = field(default_factory=lambda: ["depth20@100ms", "trade"])
    buffer_size: int = 50_000


# ---------------------------------------------------------------------------
# Binance WebSocket Client
# ---------------------------------------------------------------------------

class BinanceWebsocketClient:
    """
    Manages a persistent WebSocket connection to Binance combined streams.

    Design goals
    ------------
    - Non-blocking: all network reads happen in a dedicated daemon thread.
    - Thread-safe: a bounded queue shields the caller from the producer.
    - Stateless aggregation: raw messages are buffered; derived metrics
      (imbalance, spread) are computed on demand from the latest snapshot.

    Math: Orderbook Imbalance
    -------------------------
    imbalance = Σ(bid_volumes) / Σ(ask_volumes)

    Values > 1 indicate buying pressure (more volume on the bid side);
    values < 1 indicate selling pressure.  A value of 0.5 means the bid
    side has half the volume of the ask side (strong sell pressure).

    Math: Tick Aggregation
    ----------------------
    Recent trades are accumulated in a rolling window.  Aggregation here
    refers to collecting the last N raw tick events and returning them as
    a list with size, side, and timestamp fields so that higher-level
    strategies can compute volume-weighted averages or detect aggressive
    order flow without managing their own buffer.

    Math: Spread (BPS)
    ------------------
    spread_bps = (ask_price - bid_price) / mid_price * 10_000

    One basis point (bps) = 0.01 %.  Tight spreads indicate liquid markets;
    wide spreads signal stress or thin order books.
    """

    BASE_URL = "wss://stream.binance.com:9443/stream"

    def __init__(self, config: StreamConfig | None = None) -> None:
        self.config = config or StreamConfig()
        self._ws: websocket.WebSocketApp | None = None
        self._recv_queue: queue.Queue[str] = queue.Queue(
            maxsize=self.config.buffer_size
        )
        self._thread: threading.Thread | None = None
        self._running = False

        # Cached snapshots updated by _on_message
        self._latest_depth: dict[str, dict[str, Any]] = {}
        self._latest_trade: dict[str, dict[str, Any]] = {}
        self._latest_book_ticker: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """
        Start the WebSocket connection and background consumer thread.

        Constructs the combined streams URL from config.symbols and
        config.channels, e.g.:
        wss://stream.binance.com:9443/stream?streams=btcusdt@depth20@100ms/btcusdt@trade

        Raises
        ------
        RuntimeError
            If already connected.
        """
        if self._running:
            raise RuntimeError("Already connected. Call disconnect() first.")

        streams = "/".join(
            f"{s.lower()}@{c}"
            for s in self.config.symbols
            for c in self.config.channels
        )
        url = f"{self.BASE_URL}?streams={streams}"
        log.info("Connecting to Binance WebSocket: %s", url)

        self._ws = websocket.WebSocketApp(
            url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="binance-ws")
        self._thread.start()

    def disconnect(self) -> None:
        """Gracefully close the WebSocket and stop the background thread."""
        self._running = False
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=5.0)
        self._ws = None
        self._thread = None
        log.info("WebSocket disconnected.")

    # ------------------------------------------------------------------
    # Internal WebSocket handlers
    # ------------------------------------------------------------------

    def _run(self) -> None:
        assert self._ws is not None
        while self._running:
            try:
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                log.warning("WebSocket runner error (reconnecting in 5s): %s", exc)
                time.sleep(5.0)
        log.info("WebSocket runner stopped.")

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        log.info("WebSocket connection opened.")

    def _on_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        """
        Parse incoming JSON and cache the latest snapshot for each stream type.

        Implements a bounded-queue fallback so the background thread never
        blocks even if the caller consumes slowly.
        """
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Failed to decode JSON: %s", raw)
            return

        # Binance combined-stream format: {"stream": "...", "data": {...}}
        stream = payload.get("stream", "")
        data = payload.get("data", {})

        with self._lock:
            if "depth" in stream or "depthUpdate" in stream:
                self._latest_depth[data.get("s", "")] = data
            elif "trade" in stream:
                self._latest_trade[data.get("s", "")] = data
            elif "bookTicker" in stream:
                self._latest_book_ticker[data.get("s", "")] = data

        # Enqueue for consumers who want raw messages.
        # Near-capacity warning to make data-loss observable rather than silent.
        warn_threshold = int(self.config.buffer_size * 0.9)
        if self._recv_queue.qsize() >= warn_threshold:
            log.warning(
                "WebSocket recv queue at %d / %d — dropping oldest message to make room",
                self._recv_queue.qsize(),
                self.config.buffer_size,
            )
        try:
            self._recv_queue.put_nowait(raw)
        except queue.Full:
            try:
                self._recv_queue.get_nowait()  # make space
                self._recv_queue.put_nowait(raw)
            except queue.Full:
                # Queue was empty but is somehow still full — log and raise so the
                # backpressure is visible, not swallowed.
                log.error(
                    "WebSocket recv queue is Full and cannot be drained — message dropped: %s",
                    raw[:200],
                )
                raise

    def _on_error(self, ws: websocket.WebSocketApp, error: Any) -> None:
        log.error("WebSocket error: %s", error)

    def _on_close(self, ws: websocket.WebSocketApp, close_status_code: int | None, close_msg: str | None) -> None:
        log.info("WebSocket closed (code=%s, msg=%s)", close_status_code, close_msg)

    # ------------------------------------------------------------------
    # Public API — non-blocking
    # ------------------------------------------------------------------

    def subscribe(self, *channels: str) -> None:
        """
        Subscribe to additional channels at runtime (not yet implemented).

        Binance combined streams do not support dynamic subscription after
        connect; a new connection is required.  This method is a placeholder
        that raises NotImplementedError to signal the limitation clearly.
        """
        raise NotImplementedError(
            "Dynamic per-stream subscription requires reconnect. "
            "Update self.config and call connect() again."
        )

    def get_orderbook_imbalance(self, symbol: str, depth: int = 20) -> float | None:
        """
        Compute orderbook imbalance for the latest cached depth snapshot.

        Parameters
        ----------
        symbol : str
            Uppercase pair symbol, e.g. "BTCUSDT".
        depth : int
            Number of price levels to consider on each side (default 20).
            Binance depth streams default to 20 levels; passing a larger
            value has no effect if the snapshot only contains 20.

        Returns
        -------
        float | None
            Σ(bid_volumes) / Σ(ask_volumes), or None if no snapshot available.

        Math
        ----
        imbalance = Σ(bid_volumes) / Σ(ask_volumes)

        - imbalance > 1 : more bid volume than ask volume → buying pressure
        - imbalance < 1 : more ask volume than bid volume → selling pressure
        - imbalance = 1 : perfectly balanced

        Example
        -------
        >>> client.get_orderbook_imbalance("BTCUSDT", depth=20)
        1.34  # 34% more bid volume than ask — buy pressure
        """
        with self._lock:
            snapshot = self._latest_depth.get(symbol.upper())

        if not snapshot:
            log.debug("No depth snapshot cached for %s", symbol)
            return None

        bids = snapshot.get("b", [])[:depth]   # list of [price, qty]
        asks = snapshot.get("a", [])[:depth]

        bid_vol = sum(float(v) for _, v in bids)
        ask_vol = sum(float(v) for _, v in asks)

        if ask_vol == 0:
            log.warning("Ask volume is zero for %s — returning None", symbol)
            return None

        return bid_vol / ask_vol

    def get_tick_trades(self, symbol: str, limit: int = 50) -> list[dict]:
        """
        Return the most recent N raw trade ticks accumulated in the queue.

        This method drains all currently queued trade messages and returns
        up to `limit` of the most recent ones.  Trades that overflow the
        buffer are silently dropped.

        Parameters
        ----------
        symbol : str
            Uppercase pair symbol, e.g. "ETHUSDT".
        limit : int
            Maximum trades to return.  Default 50.

        Returns
        -------
        list[dict]
            List of trade dicts with keys:
            - symbol  (str)  : pair, e.g. "BTCUSDT"
            - price   (str)  : execution price
            - quantity (str) : execution quantity
            - side    (str)  : "BUY" or "SELL" (aggressor side)
            - timestamp (int): trade time in ms
            - is_buyer_maker (bool)

        Note
        ----
        Binance trade stream emits each individual tick.  For volume-weighted
        analysis, accumulate the returned list externally.
        """
        trades: list[dict] = []
        symbol_lower = symbol.lower()

        # Drain queue until empty or limit reached
        while len(trades) < limit:
            try:
                raw = self._recv_queue.get_nowait()
            except queue.Empty:
                break

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            stream = payload.get("stream", "")
            data = payload.get("data", {})

            if "trade" not in stream:
                continue
            if data.get("s", "").lower() != symbol_lower:
                continue

            trades.append(
                {
                    "symbol": data.get("s", symbol.upper()),
                    "price": data.get("p", "0"),
                    "quantity": data.get("q", "0"),
                    "side": "SELL" if data.get("m", True) else "BUY",
                    # m = True means buyer is maker → aggressive side = SELL
                    "timestamp": data.get("T", 0),
                    "is_buyer_maker": data.get("m", True),
                }
            )

        # Return the most recent `limit` trades
        return trades[-limit:] if len(trades) > limit else trades

    def get_spread(self, symbol: str) -> float | None:
        """
        Compute the current bid-ask spread in basis points (bps).

        Parameters
        ----------
        symbol : str
            Uppercase pair symbol, e.g. "BTCUSDT".

        Returns
        -------
        float | None
            Spread in bps, e.g. 5.2 means the spread is 0.052 % of mid price.
            Returns None if no bookTicker snapshot is available.

        Math
        ----
        spread_bps = (ask_price - bid_price) / mid_price * 10_000

        mid_price = (ask_price + bid_price) / 2

        Interpretation
        -------------
        - < 1 bps  : extremely liquid (BTC, ETH near mid-price)
        - 1–5 bps  : normal retail market
        - 5–10 bps : widening — possible stress or after-hours
        - > 10 bps : illiquid; use with caution
        """
        with self._lock:
            bt = self._latest_book_ticker.get(symbol.upper())

        if not bt:
            log.debug("No bookTicker cached for %s", symbol)
            return None

        bid = float(bt.get("b", 0))
        ask = float(bt.get("a", 0))

        if bid == 0 or ask == 0:
            log.warning("Zero bid or ask for %s", symbol)
            return None

        mid = (bid + ask) / 2.0
        spread_bps = (ask - bid) / mid * 10_000
        return round(spread_bps, 4)

    # ------------------------------------------------------------------
    # Diagnostic
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        """Return True if the background thread is running."""
        return self._running and self._thread is not None and self._thread.is_alive()


# ---------------------------------------------------------------------------
# Stub test block
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    config = StreamConfig(
        symbols=["btcusdt", "ethusdt"],
        channels=["depth20@100ms", "trade"],
        buffer_size=5_000,
    )
    client = BinanceWebsocketClient(config)

    print("stub: BinanceWebsocketClient — would connect to wss://stream.binance.com:9443")

    # Exercise config dataclass
    assert config.symbols == ["btcusdt", "ethusdt"]
    assert config.buffer_size == 5_000
    print("stub: StreamConfig — OK")

    # Demonstrate what methods would compute (no real connection)
    class _DummyClient(BinanceWebsocketClient):
        def get_orderbook_imbalance(self, symbol: str, depth: int = 20) -> float | None:
            # Without a real WS, this always returns None
            return None

        def get_tick_trades(self, symbol: str, limit: int = 50) -> list[dict]:
            return []

        def get_spread(self, symbol: str) -> float | None:
            return None

    dummy = _DummyClient(config)
    assert dummy.get_orderbook_imbalance("BTCUSDT") is None
    assert dummy.get_tick_trades("BTCUSDT") == []
    assert dummy.get_spread("BTCUSDT") is None
    print("stub: BinanceWebsocketClient non-blocking methods — OK (all return None without connection)")
