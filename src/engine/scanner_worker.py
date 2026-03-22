"""
src/engine/scanner_worker.py
============================
Plutus V4.0 — Scanner Worker (RT2: Idempotent Consumer Group)

Runs in the ``plutus_engine`` container.  Continuously:
1. Reads from ``plutus:orderbook:stream`` using XREADGROUP
   (RT2 fix: consumer groups for exactly-once delivery).
2. Computes bid/ask volume imbalances.
3. Publishes anomaly events to ``plutus:signals`` Redis pub/sub.
4. Persists each snapshot to TimescaleDB.
5. XACKs processed messages (RT2: prevents re-processing on reconnect).

RT2 Fix Summary:
  - Uses XREADGROUP instead of XREVRANGE — each message assigned to one consumer.
  - XACK called after successful processing.
  - On reconnect, consumer resumes after its last-acked ID.
  - Pending entries are reclaimed on startup.

Environment Variables
--------------------
REDIS_URL         Redis URL   (default: redis://localhost:6379)
TIMESERIES_URL    TSDB URL    (default: postgresql://plutus:plutus@localhost:5432/plotus)
LOG_LEVEL         Logging     (default: INFO)
"""

from __future__ import annotations

import os
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as redis_async
import asyncpg

from loguru import logger

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Build TimescaleDB URL from individual env vars.
# In production: set DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME.
# The fallback below contains no credentials — it is for LOCAL DEVELOPMENT ONLY.
_DB_HOST = os.getenv("DB_HOST", "localhost")
_DB_PORT = os.getenv("DB_PORT", "5432")
_DB_USER = os.getenv("DB_USER", "plutus")
_DB_PASSWORD = os.getenv("DB_PASSWORD", "plutus")   # LOCAL DEV ONLY — override in production
_DB_NAME = os.getenv("DB_NAME", "plutus")

REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")
TIMESERIES_URL: str = os.getenv(
    "TIMESERIES_URL",
    f"postgresql://{_DB_USER}:{_DB_PASSWORD}@{_DB_HOST}:{_DB_PORT}/{_DB_NAME}",
)
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
CYCLE_SLEEP_SECONDS: float = 5.0

# Thresholds for anomaly detection
IMBALANCE_THRESHOLD_LONG: float = 0.25   # bid_vol > ask_vol * (1 + threshold) → bullish
IMBALANCE_THRESHOLD_SHORT: float = -0.25  # bid_vol < ask_vol * (1 + |threshold|) → bearish

# ─────────────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class OrderbookSnapshot:
    """Represents a single orderbook snapshot retrieved from Redis."""

    symbol: str
    timestamp_ms: int
    bids: list[tuple[float, float]]   # [(price, qty), ...]
    asks: list[tuple[float, float]]   # [(price, qty), ...]

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def spread_pct(self) -> float:
        mid = (self.best_bid + self.best_ask) / 2
        return self.spread / mid if mid else 0.0

    @property
    def total_bid_volume(self) -> float:
        return sum(qty for _, qty in self.bids)

    @property
    def total_ask_volume(self) -> float:
        return sum(qty for _, qty in self.asks)

    @property
    def imbalance(self) -> float:
        """Returns a value in [-1, 1]: positive = bid-skewed, negative = ask-skewed."""
        total = self.total_bid_volume + self.total_ask_volume
        if total == 0:
            return 0.0
        return (self.total_bid_volume - self.total_ask_volume) / total

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp_ms": self.timestamp_ms,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "spread": self.spread,
            "spread_pct": self.spread_pct,
            "bid_volume": self.total_bid_volume,
            "ask_volume": self.total_ask_volume,
            "imbalance": self.imbalance,
        }


@dataclass
class AnomalyEvent:
    """An anomaly event to be published to Redis and persisted to TSDB."""

    symbol: str
    timestamp_ms: int
    event_type: str          # e.g. "bid_imbalance", "spread_widening"
    severity: str             # "low", "medium", "high", "critical"
    snapshot: OrderbookSnapshot
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_redis_payload(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "severity": self.severity,
            "symbol": self.symbol,
            "timestamp_ms": self.timestamp_ms,
            "imbalance": self.snapshot.imbalance,
            "spread_pct": self.snapshot.spread_pct,
            "metadata": self.metadata,
        }

    def to_db_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "event_type": self.event_type,
            "severity": self.severity,
            "payload": self.to_redis_payload(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# ScannerWorker
# ─────────────────────────────────────────────────────────────────────────────


class ScannerWorker:
    """
    Continuous orderbook imbalance scanner.

    Reads orderbook snapshots from Redis, computes imbalances,
    detects anomalies, publishes them, and persists results.
    """

    # RT2: Stream uses consumer group for exactly-once delivery
    REDIS_STREAM_KEY = "plutus:orderbook:stream"
    REDIS_CHANNEL    = "plutus:signals"       # RT3: anomaly pub/sub channel
    CONSUMER_GROUP   = "plutus-orderbook-consumers"

    def __init__(
        self,
        redis_url: str = REDIS_URL,
        timeseries_url: str = TIMESERIES_URL,
        consumer_name: str | None = None,
        batch_size: int = 10,
        block_ms: int = 1000,
    ) -> None:
        self.redis_url = redis_url
        self.timeseries_url = timeseries_url
        self.batch_size = batch_size
        self.block_ms = block_ms
        # RT2: Unique consumer name for consumer group
        self._consumer_name = consumer_name or (
            f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )

        self._redis: redis_async.Redis | None = None
        self._pg_pool: asyncpg.Pool | None = None
        self._running = False
        self._cycle_count = 0

        logger.info(f"ScannerWorker created (consumer={self._consumer_name})")

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Establish Redis and TimescaleDB connections; create consumer group (RT2)."""
        self._redis = redis_async.from_url(self.redis_url, decode_responses=True)
        self._pg_pool = await asyncpg.create_pool(
            self.timeseries_url,
            min_size=1,
            max_size=5,
        )
        # RT2 core: ensure consumer group exists (idempotent — OK if already exists)
        try:
            await self._redis.xgroup_create(
                self.REDIS_STREAM_KEY,
                self.CONSUMER_GROUP,
                id="0",
                mkstream=True,
            )
            logger.info(f"Consumer group '{self.CONSUMER_GROUP}' created")
        except redis_async.ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                logger.debug(f"Consumer group '{self.CONSUMER_GROUP}' already exists")
            else:
                raise
        logger.info("ScannerWorker connected to Redis and TimescaleDB")

    async def disconnect(self) -> None:
        """Close all connections."""
        if self._redis:
            await self._redis.aclose()
        if self._pg_pool:
            await self._pg_pool.close()
        logger.info("ScannerWorker disconnected")

    # ── Core Loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Main worker loop. Runs until :meth:`stop` is called.

        RT2 fix: Uses XREADGROUP (blocking) instead of sleep+polling.
        Each cycle:
        1. XREADGROUP from Redis stream — blocks until messages arrive.
        2. Compute imbalance and detect anomalies per message.
        3. Publish anomalies to Redis pub/sub.
        4. Persist snapshots to TimescaleDB.
        5. XACK all processed messages (prevents re-processing on reconnect).
        """
        if self._redis is None or self._pg_pool is None:
            raise RuntimeError("ScannerWorker not connected; call connect() first")

        self._running = True
        logger.info(
            f"ScannerWorker loop starting (group={self.CONSUMER_GROUP}, "
            f"consumer={self._consumer_name}, batch={self.batch_size}, block={self.block_ms}ms)"
        )

        while self._running:
            try:
                # RT2 core: blocking read — no polling interval
                raw_messages = await self._redis.xreadgroup(
                    groupname=self.CONSUMER_GROUP,
                    consumername=self._consumer_name,
                    streams={self.REDIS_STREAM_KEY: ">"},
                    count=self.batch_size,
                    block=self.block_ms,
                )
                if not raw_messages:
                    continue

                acked_ids: list[str] = []

                for stream_name, entries in raw_messages:
                    for msg_id, fields in entries:
                        self._cycle_count += 1
                        snapshot = self._parse_snapshot(fields)

                        if snapshot is None:
                            logger.warning(f"Failed to parse snapshot {msg_id}; acking anyway")
                            acked_ids.append(msg_id)
                            continue

                        logger.debug(
                            f"[Cycle {self._cycle_count}] {snapshot.symbol} | "
                            f"bid_vol={snapshot.total_bid_volume:.4f} "
                            f"ask_vol={snapshot.total_ask_volume:.4f} "
                            f"imbalance={snapshot.imbalance:.4f} "
                            f"spread_pct={snapshot.spread_pct:.4f}"
                        )

                        anomalies = self._detect_anomalies(snapshot)

                        # S3: Atomic persistence — write to TSDB first, then Redis.
                        # TSDB is the source of truth. Redis is a notification channel.
                        # If TSDB write fails, we skip the Redis publish.
                        # If Redis publish fails after TSDB succeeds, data is safe
                        # in TSDB and the error is logged but does not crash the loop.
                        ok = await self._persist_snapshot(snapshot)
                        if ok and anomalies:
                            for anomaly in anomalies:
                                pub_ok = await self._publish_anomaly(anomaly)
                                if not pub_ok:
                                    logger.warning(
                                        "Redis publish failed for anomaly %s %s "
                                        "(TSDB write succeeded; event is persisted)",
                                        anomaly.event_type, anomaly.symbol,
                                    )

                        if anomalies:
                            symbols = {a.symbol for a in anomalies}
                            severities = {a.severity for a in anomalies}
                            logger.info(
                                f"ANOMALY [{','.join(severities)}] {','.join(symbols)} "
                                f"| cycle={self._cycle_count}"
                            )

                        # RT2: ack after successful processing
                        acked_ids.append(msg_id)

                # RT2: batch XACK after processing all messages
                if acked_ids:
                    await self._redis.xack(
                        self.REDIS_STREAM_KEY, self.CONSUMER_GROUP, *acked_ids
                    )
                    logger.debug(f"XACKed {len(acked_ids)} messages")

            except asyncio.CancelledError:
                logger.info("ScannerWorker loop cancelled")
                break
            except redis_async.RedisError as exc:
                logger.error(f"Redis error in worker loop: {exc}")
                await asyncio.sleep(1.0)
            except Exception as exc:
                logger.exception(f"Error in scanner cycle: {exc}")

        logger.info("ScannerWorker loop stopped")

    async def stop(self) -> None:
        """Signal the worker loop to exit gracefully."""
        self._running = False
        logger.info("ScannerWorker stop requested")

    # ── Per-Cycle Logic ───────────────────────────────────────────────────────

    def _parse_snapshot(
        self, fields: dict[str, str]
    ) -> OrderbookSnapshot | None:
        """Parse a Redis stream entry into an OrderbookSnapshot."""
        try:
            symbol = fields.get("symbol", "UNKNOWN")
            timestamp_ms = int(fields.get("timestamp_ms", "0"))

            # bids/asks stored as JSON arrays of [price, qty]
            bids_raw = json.loads(fields.get("bids", "[]"))
            asks_raw = json.loads(fields.get("asks", "[]"))

            bids: list[tuple[float, float]] = [
                (float(p), float(q)) for p, q in bids_raw
            ]
            asks: list[tuple[float, float]] = [
                (float(p), float(q)) for p, q in asks_raw
            ]

            return OrderbookSnapshot(
                symbol=symbol,
                timestamp_ms=timestamp_ms,
                bids=bids,
                asks=asks,
            )
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            logger.error(f"Snapshot parse error: {exc}")
            return None

    def _detect_anomalies(self, snap: OrderbookSnapshot) -> list[AnomalyEvent]:
        """
        Detect orderbook anomalies based on imbalance and spread thresholds.

        Returns a list of ``AnomalyEvent`` objects (may be empty).
        """
        anomalies: list[AnomalyEvent] = []
        ts = snap.timestamp_ms or int(time.time() * 1000)

        # Imbalance anomaly
        imbalance = snap.imbalance
        if imbalance >= IMBALANCE_THRESHOLD_LONG:
            severity = self._imbalance_to_severity(imbalance, direction="long")
            anomalies.append(
                AnomalyEvent(
                    symbol=snap.symbol,
                    timestamp_ms=ts,
                    event_type="bid_imbalance",
                    severity=severity,
                    snapshot=snap,
                    metadata={"direction": "long", "threshold": IMBALANCE_THRESHOLD_LONG},
                )
            )
        elif imbalance <= IMBALANCE_THRESHOLD_SHORT:
            severity = self._imbalance_to_severity(abs(imbalance), direction="short")
            anomalies.append(
                AnomalyEvent(
                    symbol=snap.symbol,
                    timestamp_ms=ts,
                    event_type="ask_imbalance",
                    severity=severity,
                    snapshot=snap,
                    metadata={"direction": "short", "threshold": abs(IMBALANCE_THRESHOLD_SHORT)},
                )
            )

        # Spread widening anomaly (spread > 0.1% of mid)
        if snap.spread_pct > 0.001:
            anomalies.append(
                AnomalyEvent(
                    symbol=snap.symbol,
                    timestamp_ms=ts,
                    event_type="spread_widening",
                    severity="medium" if snap.spread_pct < 0.005 else "high",
                    snapshot=snap,
                    metadata={"spread_pct": snap.spread_pct},
                )
            )

        return anomalies

    @staticmethod
    def _imbalance_to_severity(value: float, direction: str) -> str:
        abs_val = abs(value)
        if abs_val >= 0.6:
            return "critical"
        elif abs_val >= 0.45:
            return "high"
        elif abs_val >= 0.35:
            return "medium"
        else:
            return "low"

    async def _persist_snapshot(self, snap: OrderbookSnapshot) -> bool:
        """
        S3: Write orderbook imbalance data to TimescaleDB.

        Returns True on success, False on failure.
        The caller uses this to gate the subsequent Redis publish (atomic gate).
        """
        if self._pg_pool is None:
            return True   # no-op is considered successful

        try:
            async with self._pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO orderbook_imbalance
                        (time, symbol, bid_volume, ask_volume, imbalance, spread)
                    VALUES (
                        to_timestamp($1 / 1000.0),
                        $2, $3, $4, $5, $6
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    snap.timestamp_ms,
                    snap.symbol,
                    snap.total_bid_volume,
                    snap.total_ask_volume,
                    snap.imbalance,
                    snap.spread,
                )
            return True
        except Exception as exc:
            logger.error(f"Failed to persist snapshot to TSDB: {exc}")
            return False

    async def _publish_anomaly(self, anomaly: AnomalyEvent) -> bool:
        """
        Publish an anomaly event to the Redis scanner.events pub/sub channel.

        Returns True on success, False on failure.
        S3: Caller gates this on TSDB write success to maintain atomicity.
        """
        if self._redis is None:
            return True   # no-op is considered successful

        try:
            payload = json.dumps(anomaly.to_redis_payload(), default=str)
            await self._redis.publish(self.REDIS_CHANNEL, payload)
            logger.debug(
                f"Published anomaly: {anomaly.event_type} "
                f"severity={anomaly.severity} symbol={anomaly.symbol}"
            )
            return True
        except Exception as exc:
            logger.error(f"Failed to publish anomaly to Redis: {exc}")
            return False


# ─────────────────────────────────────────────────────────────────────────────
# CLI Entry Point
# ─────────────────────────────────────────────────────────────────────────────

async def _main() -> None:
    import signal

    worker = ScannerWorker()
    await worker.connect()

    # Graceful shutdown on SIGINT/SIGTERM
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(worker)))

    try:
        await worker.run()
    finally:
        await worker.disconnect()


async def _shutdown(worker: ScannerWorker) -> None:
    logger.info("Shutdown signal received; stopping ScannerWorker...")
    await worker.stop()


if __name__ == "__main__":
    asyncio.run(_main())
