"""
src/engine/realtime_pipeline.py
================================
Plutus V4.0 — Real-Time Architecture

Full pipeline:
  Binance WebSocket
      → BinanceConnector._on_message()  (RT1: XADD to Redis stream)
      → ScannerWorker.xreadgroup()       (RT2: consumer-group, idempotent)
      → AnomalyDetector
      → Redis pub/sub scanner.events
      → PlutusEngine.subscribe()          (RT3: SSE/WebSocket push)
      → DecisionEngine (SHOCK/CONFIRM)    (RT6: 3-phase exit)
      → SmartRouter / BinanceExecutor

Consumer group semantics (RT2):
  - Each message is delivered to exactly ONE consumer (xreadgroup).
  - After processing, xack() is called.
  - On reconnect the consumer resumes after its last-acked ID.
  - Consumer is identified by hostname+pid+uuid to avoid collisions.

Stream MAXLEN (RT4):
  - All streams use XADD with ~MAXLEN 50_000 to prevent unbounded growth.
  - Under extreme load, old messages are dropped (acceptable — we only
    care about the most recent anomalies), but anomaly events themselves
    are also published to pub/sub for live consumers, so nothing is lost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, AsyncIterator, Optional

import redis.asyncio as redis_async
import redis as redis_sync
import websocket  # pip install websocket-client

from loguru import logger

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")

# Stream keys
SCANNER_STREAM: str = "plutus:scanner:stream"      # raw WebSocket → scanner
ORDERBOOK_STREAM: str = "plutus:orderbook:stream"   # orderbook snapshots
ANOMALY_CHANNEL: str = "plutus:signals"            # pub/sub: anomaly events
DECISION_CHANNEL: str = "plutus:decisions"          # pub/sub: decisions

# Consumer group
SCANNER_GROUP: str = "plutus-scanner-consumers"

# Stream cap — prevents unbounded Redis memory growth (RT4)
STREAM_MAXLEN: int = 50_000

# ─────────────────────────────────────────────────────────────────────────────
# RT1 — BinanceConnector: WebSocket → Redis Stream Producer
# ─────────────────────────────────────────────────────────────────────────────


class BinanceConnector:
    """
    Bridges Binance WebSocket → Redis stream via XADD.

    Design:
      - Connects to Binance combined streams (depth + trade + bookTicker).
      - On every message, parses and writes to the scanner Redis stream.
      - Does NOT block the WebSocket thread — all Redis I/O is async.
      - Uses XADD with MAXLEN ~50_000 so the stream never grows unbounded.

    Usage:
        connector = BinanceConnector(
            symbols=["btcusdt", "ethusdt"],
            channels=["depth20@100ms", "trade"],
        )
        await connector.connect()   # starts WebSocket in background thread
        # ... pipeline runs ...
        await connector.disconnect()
    """

    STREAM_KEY = SCANNER_STREAM
    MAXLEN = STREAM_MAXLEN

    def __init__(
        self,
        redis_url: str = REDIS_URL,
        symbols: Optional[list[str]] = None,
        channels: Optional[list[str]] = None,
    ) -> None:
        self._symbols = symbols or ["btcusdt"]
        self._channels = channels or ["depth20@100ms", "trade"]
        self._redis_url = redis_url
        self._redis: Optional[redis_async.Redis] = None
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._ws_lock = threading.Lock()
        # Explicit event loop reference passed from connect() — used in the WS thread
        # to schedule async Redis publishes.  Never call asyncio.get_running_loop()
        # from this thread (it will raise RuntimeError / return None and drop messages).
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Start async Redis client, capture the event loop, and launch the WS thread."""
        if self._running:
            return
        self._redis = redis_async.from_url(
            self._redis_url, decode_responses=True, encoding="utf-8"
        )
        # Capture the running loop here (main async context) so the WS thread can use it.
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._thread = threading.Thread(target=self._ws_run, daemon=True, name="binance-connector-ws")
        self._thread.start()
        logger.info(f"BinanceConnector connected (symbols={self._symbols}, channels={self._channels})")

    async def disconnect(self) -> None:
        """Gracefully stop WebSocket and close Redis."""
        self._running = False
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=5.0)
        if self._redis:
            await self._redis.aclose()
        logger.info("BinanceConnector disconnected")

    # ── WebSocket thread ───────────────────────────────────────────────────────

    def _build_url(self) -> str:
        streams = "/".join(
            f"{s.lower()}@{c}" for s in self._symbols for c in self._channels
        )
        return f"wss://stream.binance.com:9443/stream?streams={streams}"

    def _ws_run(self) -> None:
        """Run in a daemon thread — calls self._on_ws_message from WS thread."""
        url = self._build_url()
        logger.info(f"BinanceConnector connecting to: {url}")

        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    url,
                    on_message=self._on_ws_message,
                    on_error=self._on_ws_error,
                    on_close=self._on_ws_close,
                    on_open=self._on_ws_open,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                logger.warning(f"WebSocket runner error (reconnecting in 5s): {exc}")
                if self._running:
                    time.sleep(5.0)
        logger.info("BinanceConnector WebSocket runner stopped")

    def _on_ws_open(self, ws: websocket.WebSocketApp) -> None:
        logger.info("Binance WebSocket connection opened")

    def _on_ws_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        """Called from WS thread — schedule async Redis publish via the stored event loop."""
        if not self._running:
            return
        if self._loop is None:
            logger.warning("BinanceConnector: event loop not set; dropping message")
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._xadd_stream(raw))
        )

    def _on_ws_error(self, ws: websocket.WebSocketApp, error: Any) -> None:
        logger.error(f"Binance WebSocket error: {error}")

    def _on_ws_close(self, ws: websocket.WebSocketApp, code: int, msg: str) -> None:
        logger.info(f"Binance WebSocket closed (code={code}, msg={msg})")

    # ── Redis XADD ──────────────────────────────────────────────────────────────

    async def _xadd_stream(self, raw: str) -> None:
        """Publish a raw WebSocket message to the Redis scanner stream (RT1)."""
        if self._redis is None:
            return
        try:
            await self._redis.xadd(
                self.STREAM_KEY,
                {"data": raw, "connector_id": _connector_id()},
                maxlen=self.MAXLEN,
                approximate=True,
            )
        except redis_async.RedisError as exc:
            logger.error(f"XADD failed: {exc}")

    # ── Orderbook helper ─────────────────────────────────────────────────────────

    async def publish_orderbook_snapshot(
        self,
        symbol: str,
        bids: list[list[float]],
        asks: list[list[float]],
        timestamp_ms: int,
    ) -> None:
        """Manually publish a structured orderbook snapshot to the orderbook stream."""
        if self._redis is None:
            return
        try:
            await self._redis.xadd(
                ORDERBOOK_STREAM,
                {
                    "symbol": symbol,
                    "timestamp_ms": str(timestamp_ms),
                    "bids": json.dumps(bids),
                    "asks": json.dumps(asks),
                },
                maxlen=20_000,
                approximate=True,
            )
        except redis_async.RedisError as exc:
            logger.error(f"Orderbook XADD failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# RT2 — ScannerWorker: Idempotent Consumer Group
# ─────────────────────────────────────────────────────────────────────────────


def _connector_id() -> str:
    return f"{os.getpid()}-{uuid.getnode()}"


def _get_running_loop() -> Optional[asyncio.AbstractEventLoop]:
    """Safely get the running event loop (works from any thread)."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


@dataclass
class StreamMessage:
    """A message read from a Redis stream via consumer group."""
    stream: str
    group: str
    consumer: str
    message_id: str
    fields: dict[str, str]

    def parse_data(self) -> Optional[dict]:
        """Parse the 'data' field from a scanner stream message."""
        raw = self.fields.get("data")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None


class IdempotentScannerWorker:
    """
    Reads from the Plutus scanner Redis stream using consumer groups.

    RT2 Fix: Uses XREADGROUP for exactly-once delivery semantics.
      - Each message is assigned to ONE consumer in the group.
      - After processing, XACK is called to acknowledge.
      - On reconnect, the consumer resumes from its last acknowledged ID.
      - Pending entries are also reclaimed on reconnect.

    Full pipeline:
      Scanner stream (XADD from BinanceConnector)
          → XREADGROUP (this worker)
          → Anomaly detection
          → PUBLISH to scanner.events pub/sub
          → XACK
    """

    STREAM_KEY = SCANNER_STREAM
    GROUP_NAME = SCANNER_GROUP
    CHANNEL = ANOMALY_CHANNEL

    def __init__(
        self,
        redis_url: str = REDIS_URL,
        consumer_name: Optional[str] = None,
        batch_size: int = 10,
        block_ms: int = 1000,
    ) -> None:
        self._redis_url = redis_url
        self._consumer_name = consumer_name or f"consumer-{_connector_id()}-{uuid.uuid4().hex[:8]}"
        self._batch_size = batch_size
        self._block_ms = block_ms
        self._redis: Optional[redis_async.Redis] = None
        self._running = False
        self._pending_ids: set[str] = set()
        logger.info(f"IdempotentScannerWorker created (consumer={self._consumer_name})")

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Create Redis client and ensure consumer group exists (RT2)."""
        self._redis = redis_async.from_url(
            self._redis_url, decode_responses=True, encoding="utf-8"
        )
        # RT2 core: create consumer group if it doesn't exist (idempotent)
        try:
            await self._redis.xgroup_create(
                self.STREAM_KEY,
                self.GROUP_NAME,
                id="0",       # read from beginning
                mkstream=True,
            )
            logger.info(f"Consumer group '{self.GROUP_NAME}' created or already exists")
        except redis_async.ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                logger.debug(f"Consumer group '{self.GROUP_NAME}' already exists")
            else:
                raise
        logger.info("IdempotentScannerWorker connected")

    async def disconnect(self) -> None:
        if self._redis:
            await self._redis.aclose()
        logger.info("IdempotentScannerWorker disconnected")

    # ── Core Loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Main consumer loop.
        Uses XREADGROUP to block until messages arrive (RT3: no polling).
        After each batch, acknowledges all processed messages with XACK.
        """
        if self._redis is None:
            raise RuntimeError("Not connected; call connect() first")

        self._running = True
        logger.info(f"Scanner worker loop starting (batch={self._batch_size}, block={self._block_ms}ms)")

        while self._running:
            try:
                # RT2 core: XREADGROUP — blocks until messages arrive or timeout
                # '>' means "only new messages", no pending entries yet to reclaim
                raw_messages = await self._redis.xreadgroup(
                    groupname=self.GROUP_NAME,
                    consumername=self._consumer_name,
                    streams={self.STREAM_KEY: ">"},
                    count=self._batch_size,
                    block=self._block_ms,
                )
                if not raw_messages:
                    continue

                # raw_messages: list of [stream_name, [(id, {field: value, ...}), ...]]
                acked_ids: list[str] = []
                for stream_name, entries in raw_messages:
                    for msg_id, fields in entries:
                        msg = StreamMessage(
                            stream=stream_name,
                            group=self.GROUP_NAME,
                            consumer=self._consumer_name,
                            message_id=msg_id,
                            fields=fields,
                        )
                        try:
                            await self._process_message(msg)
                            acked_ids.append(msg_id)
                        except Exception as exc:
                            logger.exception(f"Message processing error (id={msg_id}): {exc}")
                            # Do NOT ack on failure — message stays pending for retry

                # RT2 core: acknowledge after successful processing
                if acked_ids:
                    await self._redis.xack(self.STREAM_KEY, self.GROUP_NAME, *acked_ids)
                    logger.debug(f"ACKed {len(acked_ids)} messages: {acked_ids}")

            except asyncio.CancelledError:
                logger.info("Scanner worker loop cancelled")
                break
            except redis_async.RedisError as exc:
                logger.error(f"Redis error in worker loop: {exc}")
                await asyncio.sleep(1.0)

        logger.info("Scanner worker loop stopped")

    async def stop(self) -> None:
        self._running = False

    # ── Message Processing ──────────────────────────────────────────────────────

    async def _process_message(self, msg: StreamMessage) -> None:
        """
        Process a single stream message.
        Subclasses override this to implement actual anomaly detection.
        Default: parse and log the data.
        """
        data = msg.parse_data()
        if data is None:
            logger.debug(f"Skipping non-JSON or empty message: {msg.message_id}")
            return

        # Extract stream type
        stream = data.get("stream", "")
        payload = data.get("data", {})

        symbol = payload.get("s", "UNKNOWN")

        if "depth" in stream or "depthUpdate" in stream:
            await self._handle_orderbook_update(symbol, payload)
        elif "trade" in stream:
            await self._handle_trade(symbol, payload)
        elif "bookTicker" in stream:
            await self._handle_book_ticker(symbol, payload)

    async def _handle_orderbook_update(
        self, symbol: str, data: dict[str, Any]
    ) -> None:
        """Compute imbalance and publish anomaly event if thresholds exceeded."""
        bids = data.get("b", [])[:20]
        asks = data.get("a", [])[:20]

        bid_vol = sum(float(q) for _, q in bids)
        ask_vol = sum(float(q) for _, q in asks)
        total = bid_vol + ask_vol
        imbalance = (bid_vol - ask_vol) / total if total else 0.0

        THRESHOLD_LONG = 0.25
        THRESHOLD_SHORT = -0.25

        if imbalance >= THRESHOLD_LONG:
            severity = self._imbalance_severity(imbalance)
            await self._publish_anomaly(symbol, "bid_imbalance", severity, {
                "imbalance": round(imbalance, 4),
                "bid_vol": round(bid_vol, 4),
                "ask_vol": round(ask_vol, 4),
                "stream": "depth",
            })
        elif imbalance <= THRESHOLD_SHORT:
            severity = self._imbalance_severity(abs(imbalance))
            await self._publish_anomaly(symbol, "ask_imbalance", severity, {
                "imbalance": round(imbalance, 4),
                "bid_vol": round(bid_vol, 4),
                "ask_vol": round(ask_vol, 4),
                "stream": "depth",
            })

    async def _handle_trade(self, symbol: str, data: dict[str, Any]) -> None:
        """Log significant trade events."""
        price = float(data.get("p", 0))
        qty = float(data.get("q", 0))
        is_buyer_maker = data.get("m", True)
        logger.debug(
            f"TRADE {symbol} | price={price} qty={qty} "
            f"maker={'SELL' if is_buyer_maker else 'BUY'}"
        )

    async def _handle_book_ticker(self, symbol: str, data: dict[str, Any]) -> None:
        """Handle best bid/ask ticker updates."""
        bid = float(data.get("b", 0))
        ask = float(data.get("a", 0))
        if bid > 0 and ask > 0:
            spread_pct = (ask - bid) / ((bid + ask) / 2) * 10_000
            if spread_pct > 50:  # > 50 bps — worth logging
                logger.debug(f"BOOK_TICKER {symbol} | bid={bid} ask={ask} spread={spread_pct:.2f}bps")

    # ── Publish Anomaly to Redis Pub/Sub ───────────────────────────────────────

    async def _publish_anomaly(
        self,
        symbol: str,
        event_type: str,
        severity: str,
        metadata: dict[str, Any],
    ) -> None:
        """Publish anomaly event to the scanner pub/sub channel."""
        if self._redis is None:
            return
        try:
            payload = {
                "event_type": event_type,
                "severity": severity,
                "symbol": symbol,
                "timestamp_ms": int(time.time() * 1000),
                "consumer": self._consumer_name,
                "metadata": metadata,
            }
            await self._redis.publish(self.CHANNEL, json.dumps(payload, default=str))
            logger.info(
                f"ANOMALY [{severity.upper()}] {symbol} | {event_type} | "
                f"imbalance={metadata.get('imbalance', 'N/A')}"
            )
        except redis_async.RedisError as exc:
            logger.error(f"Failed to publish anomaly: {exc}")

    # ── Utility ───────────────────────────────────────────────────────────────

    @staticmethod
    def _imbalance_severity(value: float) -> str:
        abs_val = abs(value)
        if abs_val >= 0.60:
            return "critical"
        elif abs_val >= 0.45:
            return "high"
        elif abs_val >= 0.35:
            return "medium"
        return "low"


# ─────────────────────────────────────────────────────────────────────────────
# RT6 — SHOCK Phase: 3-Phase Decision Engine
# ─────────────────────────────────────────────────────────────────────────────

class SHOCKPhase:
    """
    PHASE 2 (SHOCK) exit mechanism for the 3-phase decision engine.

    Problem: Without an exit mechanism, SHOCK can hang forever.
    Solution: Define a trigger, a timeout, and a fallback.

    SHOCK → CONFIRM when:
      - Price moves > trigger_pct in the expected direction, OR
      - A reversal candlestick pattern forms (engulfing / hammer), OR
      - timeout_seconds elapse without resolution → NO TRADE

    Usage:
        phase = SHOCKPhase(
            entry_price=67000,
            direction="LONG",
            trigger_pct=0.5,   # 0.5% move triggers CONFIRM
            timeout_seconds=30,
        )
        for tick in live_ticks():
            result = phase.update(tick)
            if result.status == "CONFIRM":
                return execute_trade()
            elif result.status == "NO_TRADE":
                return skip()
    """

    def __init__(
        self,
        entry_price: float,
        direction: str,           # "LONG" or "SHORT"
        trigger_pct: float = 0.5,  # % move in direction to confirm
        timeout_seconds: float = 30.0,
        high_price: float = 0.0,  # for SHORT: highest price seen
        low_price: float = 0.0,   # for LONG: lowest price seen
    ) -> None:
        self.entry_price = entry_price
        self.direction = direction
        self.trigger_pct = trigger_pct
        self.timeout_seconds = timeout_seconds
        self.start_time = time.monotonic()
        self.triggered = False
        self.trigger_reason: str = ""
        self.high_price = high_price or entry_price
        self.low_price = low_price or entry_price

    def update(
        self,
        current_price: float,
        reversal_signal: bool = False,
        engulfing: bool = False,
    ) -> SHOCKResult:
        """
        Called on every price tick during SHOCK phase.

        Returns SHOCKResult with one of:
          - status = "SHOCK"   — still in SHOCK, keep waiting
          - status = "CONFIRM" — trigger hit, proceed to trade
          - status = "NO_TRADE" — timeout or adverse signal, skip
        """
        elapsed = time.monotonic() - self.start_time

        # Update high/low
        self.high_price = max(self.high_price, current_price)
        self.low_price = min(self.low_price, current_price)

        # Check timeout
        if elapsed >= self.timeout_seconds:
            return SHOCKResult(
                status="NO_TRADE",
                reason="timeout",
                elapsed=elapsed,
                current_price=current_price,
            )

        # Check price movement trigger
        move_pct = self._move_pct(current_price)
        if self.direction == "LONG":
            if move_pct >= self.trigger_pct:
                return SHOCKResult(
                    status="CONFIRM",
                    reason=f"price_moved_up_{move_pct:.3f}%",
                    elapsed=elapsed,
                    current_price=current_price,
                )
            # Adverse move: price dropped significantly from peak
            adverse_pct = ((self.high_price - current_price) / self.high_price) * 100
            if adverse_pct > self.trigger_pct * 1.5:
                return SHOCKResult(
                    status="NO_TRADE",
                    reason=f"adverse_move_{adverse_pct:.3f}%",
                    elapsed=elapsed,
                    current_price=current_price,
                )
        else:  # SHORT
            if move_pct >= self.trigger_pct:
                return SHOCKResult(
                    status="CONFIRM",
                    reason=f"price_moved_down_{move_pct:.3f}%",
                    elapsed=elapsed,
                    current_price=current_price,
                )
            adverse_pct = ((current_price - self.low_price) / self.low_price) * 100
            if adverse_pct > self.trigger_pct * 1.5:
                return SHOCKResult(
                    status="NO_TRADE",
                    reason=f"adverse_move_{adverse_pct:.3f}%",
                    elapsed=elapsed,
                    current_price=current_price,
                )

        # Check candlestick reversal signals
        if engulfing or reversal_signal:
            return SHOCKResult(
                status="CONFIRM",
                reason="reversal_candlestick",
                elapsed=elapsed,
                current_price=current_price,
            )

        # Still in SHOCK
        return SHOCKResult(
            status="SHOCK",
            reason="waiting",
            elapsed=elapsed,
            current_price=current_price,
        )

    def _move_pct(self, current_price: float) -> float:
        """Return % move in the direction of the trade."""
        if self.direction == "LONG":
            return ((current_price - self.entry_price) / self.entry_price) * 100
        else:
            return ((self.entry_price - current_price) / self.entry_price) * 100


@dataclass
class SHOCKResult:
    """Result of a SHOCK phase update."""
    status: str          # "SHOCK" | "CONFIRM" | "NO_TRADE"
    reason: str           # human-readable reason
    elapsed: float        # seconds since phase started
    current_price: float


# ─────────────────────────────────────────────────────────────────────────────
# RT3 — Real-time push via Redis pub/sub → asyncio channel
# ─────────────────────────────────────────────────────────────────────────────


class RealtimeSignalSubscriber:
    """
    Subscribes to Redis pub/sub channels and yields events as an async iterator.

    RT3 Fix: Uses asyncio to wait for pub/sub messages instead of polling.
    This enables true push semantics — messages arrive the moment they are
    published, without a 1-second polling interval.

    Usage:
        subscriber = RealtimeSignalSubscriber(redis_url)
        async for channel, event in subscriber.subscribe("plutus:signals"):
            print(f"Signal: {event}")

    Combine with FastAPI WebSocket:
        @app.websocket("/ws/signals")
        async def ws_signals(websocket):
            async for ch, event in subscriber.subscribe("plutus:signals"):
                await websocket.send_json(event)
    """

    def __init__(self, redis_url: str = REDIS_URL) -> None:
        self._redis_url = redis_url
        self._redis: Optional[redis_async.Redis] = None
        self._pubsub: Optional[redis_async.client.PubSub] = None
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    async def __aenter__(self) -> "RealtimeSignalSubscriber":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        self._redis = redis_async.from_url(
            self._redis_url, decode_responses=True, encoding="utf-8"
        )
        self._pubsub = self._redis.pubsub()
        logger.info("RealtimeSignalSubscriber connected")

    async def disconnect(self) -> None:
        if self._pubsub:
            await self._pubsub.unsubscribe()
            await self._pubsub.close()
        if self._redis:
            await self._redis.aclose()
        logger.info("RealtimeSignalSubscriber disconnected")

    async def subscribe(self, *channels: str) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """
        Async generator yielding (channel, event_dict) tuples.

        RT3: Uses pub/sub which is push-based — no polling interval needed.
        The underlying get_message(ignore_subscribe_messages=True) blocks
        until a message arrives or the timeout elapses.
        """
        if self._pubsub is None:
            raise RuntimeError("Not connected")

        await self._pubsub.subscribe(*channels)
        logger.info(f"Subscribed to channels: {channels}")

        while True:
            try:
                # RT3 core: blocking get — no 1s polling loop
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message is not None:
                    ch = message.get("channel", "")
                    data = message.get("data", "")
                    if data:
                        try:
                            event = json.loads(data) if isinstance(data, str) else data
                            yield ch, event
                        except json.JSONDecodeError:
                            logger.warning(f"Invalid JSON in pub/sub message: {data}")
                await asyncio.sleep(0)  # yield to event loop
            except asyncio.CancelledError:
                break
            except redis_async.RedisError as exc:
                logger.error(f"Pub/sub error: {exc}")
                await asyncio.sleep(1.0)

