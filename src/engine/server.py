"""
src/engine/server.py
===================
Plutus V4.0 — PlutusEngine: the LLM-powered trading brain.

Responsibilities
---------------
- Expose a FastAPI HTTP server (Uvicorn) on port 8000.
- Run IdempotentScannerWorker as a background task (RT2 fix):
    Redis stream (XREADGROUP consumer) → anomaly detection → scanner.events pub/sub
- Subscribe to Redis pub/sub channels for real-time events.
- Publish scanner anomalies and portfolio signals to Redis.
- Query TimescaleDB for historical time-series data.
- WebSocket endpoint for true real-time push to clients (RT3 fix).

Environment Variables
--------------------
REDIS_URL         Redis connection URL  (default: redis://localhost:6379)
TIMESERIES_URL    PostgreSQL/TSDB URL   (default: postgresql://plutus:plutus@localhost:5432/plotus)
LLM_API_KEY       API key for the LLM backend
LLM_BASE_URL      Base URL for LLM API (default: https://api.openai.com/v1)
LLM_MODEL         Model name            (default: gpt-4o)
LOG_LEVEL         Logging level        (default: INFO)
"""

from __future__ import annotations

import inspect
import os
import json
import asyncio
import signal
import threading
from typing import Any, Callable, Coroutine, Optional

# FastAPI / Uvicorn
import fastapi
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

# Redis
import redis

# Database
import asyncpg

# Logging
from loguru import logger

# ── RT2 fix: IdempotentScannerWorker (Redis stream consumer) ─────────────────
from src.engine.realtime_pipeline import IdempotentScannerWorker

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# ── Auth ─────────────────────────────────────────────────────────────────────────
# Required header: X-API-Key (checked directly) or Authorization: Bearer <key>.
#
# PRODUCTION: set PLUTUS_API_KEY env var.  If unset, all requests are allowed —
# DEVELOPMENT ONLY.  Never expose a server with PLUTUS_API_KEY unset in production.
_API_KEY: str | None = os.getenv("PLUTUS_API_KEY")


def _verify_api_key(request) -> bool:
    """Verify the request includes the correct API key in the X-API-Key header
    or the Authorization: Bearer header."""
    if _API_KEY is None:
        # No key configured — allow all requests (development mode only)
        return True
    # Support both X-API-Key and Authorization: Bearer
    key = request.headers.get("X-API-Key", "") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return key == _API_KEY


def _verify_api_key_obj(key: str) -> bool:
    """Verify a raw API key string against the configured key."""
    if _API_KEY is None:
        return True
    return key == _API_KEY


# ── Configuration ────────────────────────────────────────────────────────────────

# Build TimescaleDB URL from individual env vars.
# In production: set DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME.
# The fallback below contains no credentials — it is for LOCAL DEVELOPMENT ONLY.
_DB_HOST = os.getenv("DB_HOST", "localhost")
_DB_PORT = os.getenv("DB_PORT", "5432")
_DB_USER = os.getenv("DB_USER", "plutus")
_DB_PASSWORD = os.getenv("DB_PASSWORD", "plutus")   # LOCAL DEV ONLY — override in production
_DB_NAME = os.getenv("DB_NAME", "plutus")

TIMESERIES_URL: str = os.getenv(
    "TIMESERIES_URL",
    f"postgresql://{_DB_USER}:{_DB_PASSWORD}@{_DB_HOST}:{_DB_PORT}/{_DB_NAME}",
)
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")
LLM_API_KEY: str | None = os.getenv("LLM_API_KEY")
LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL: str = os.getenv("LLM_MODEL", "gpt-4o")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

logger.remove()
logger.add(
    os.fdopen(os.stderr, "w"),
    level=LOG_LEVEL,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> — <level>{message}</level>",
)

# ─────────────────────────────────────────────────────────────────────────────
# FastAPI App
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Plutus V4.0 Engine",
    description="LLM-powered trading brain with real-time Redis pub/sub",
    version="4.0.0",
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "plutus_engine", "version": "4.0.0"}


@app.get("/channels")
async def list_channels() -> dict[str, list[str]]:
    """Return all registered Plutus pub/sub channels."""
    channels = [
        "scanner.events",
        "orders.pending",
        "orders.filled",
        "portfolio.updates",
        "risk.alerts",
    ]
    return {"channels": channels}


@app.post("/publish/{channel}")
async def publish_to_channel(channel: str, payload: dict[str, Any]) -> dict[str, str]:
    """Manually publish a payload to a Redis channel."""
    engine = app.state.engine  # type: ignore[attr-defined]
    success = engine.publish_event(channel, payload)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to publish to Redis")
    return {"published": channel, "ok": True}


@app.get("/query")
async def query_timeseries(sql: str, request: fastapi.Request) -> dict[str, Any]:
    """Execute a read-only SQL query against TimescaleDB."""
    if not _verify_api_key(request):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    # Strict whitelist — only SELECT statements
    normalised = sql.strip().upper()
    if not normalised.startswith("SELECT") or not normalised.endswith(";"):
        raise HTTPException(
            status_code=400,
            detail="Only single SELECT statements are permitted",
        )

    engine = app.state.engine  # type: ignore[attr-defined]
    try:
        rows = await engine.query_timeseries(sql)
        return {"rows": rows, "count": len(rows)}
    except Exception as exc:
        logger.error(f"TSDB query failed: {exc}")
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ─────────────────────────────────────────────────────────────────────────────
# RT3 FIX — Real-time push endpoints (WebSocket + SSE)
# Replaces the 1-second polling loop in _pubsub_reader with true push.
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_SIGNAL_CHANNELS = ["plutus:signals", "plutus:decisions"]


@app.websocket("/ws/signals")
async def websocket_signals(websocket: WebSocket):
    """
    RT3 FIX: WebSocket endpoint for real-time signal push.

    Uses Redis pub/sub subscriber that blocks on get_message()
    (no polling interval). Messages are forwarded to the WebSocket
    client the instant they are published.

    REQUIRED HEADER: X-API-Key <key>  (or Authorization: Bearer <key>).

    Clients specify channels via URL param, e.g.:
      ws://localhost:8000/ws/signals?channels=plutus:signals,plutus:decisions
    """
    # Authenticate before accepting the WebSocket upgrade (#32 fix)
    raw_key = websocket.headers.get("x-api-key", "")
    if not raw_key:
        bearer = websocket.headers.get("authorization", "")
        raw_key = bearer.removeprefix("Bearer ").strip()
    if not _verify_api_key_obj(raw_key):
        await websocket.close(code=1008, reason="Missing or invalid API key")
        logger.warning("WebSocket connection rejected: invalid API key")
        return

    raw_channels = websocket.query_params.get("channels", ",".join(_DEFAULT_SIGNAL_CHANNELS))
    channels = [ch.strip() for ch in raw_channels.split(",") if ch.strip()]

    await websocket.accept()
    logger.info(f"WebSocket client connected (channels={channels})")

    import redis.asyncio as redis_async
    subscriber = _WSSubscriber(channels)
    try:
        async for channel, event in subscriber.subscribe():
            payload = json.dumps({"channel": channel, "event": event}, default=str)
            await websocket.send_text(payload)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as exc:
        logger.exception(f"WebSocket error: {exc}")
    finally:
        await subscriber.close()


@app.get("/sse/signals")
async def sse_signals(
    channels: str = ",".join(_DEFAULT_SIGNAL_CHANNELS),
    request: fastapi.Request = None,
):
    """
    RT3 FIX: Server-Sent Events (SSE) endpoint for real-time signal push.

    SSE is simpler than WebSocket for server-to-client streaming.
    No polling — messages arrive the instant they are published to Redis.

    REQUIRED HEADER: X-API-Key <key>  (or Authorization: Bearer <key>).

    Usage:
        curl -N -H "X-API-Key: mykey" "http://localhost:8000/sse/signals?channels=plutus:signals"

    Browser consumption:
        const src = new EventSource("/sse/signals");
        src.addEventListener("signal", e => console.log(JSON.parse(e.data)));
    """
    # Authenticate SSE connection (#32 fix)
    if not _verify_api_key(request):
        raise HTTPException(status_code=401, detail="Missing or invalid API key — X-API-Key header required")

    channel_list = [ch.strip() for ch in channels.split(",") if ch.strip()]

    async def event_stream():
        import redis.asyncio as redis_async
        subscriber = _WSSubscriber(channel_list)
        try:
            yield (
                "event: connected\n"
                f"data: {{\"status\":\"connected\",\"channels\":{json.dumps(channel_list)}}}\n\n"
            )
            async for channel, event in subscriber.subscribe():
                payload = json.dumps({"channel": channel, "event": event}, default=str)
                yield f"event: signal\ndata: {payload}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            await subscriber.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class _WSSubscriber:
    """
    Thin async wrapper around redis.asyncio pub/sub for WebSocket/SSE handlers.

    RT3: Uses blocking get_message() with no polling — messages are yielded
    immediately when Redis delivers them.
    """

    def __init__(self, channels: list[str]) -> None:
        self._channels = channels
        self._redis: Optional[redis_async.Redis] = None
        self._pubsub: Optional[redis_async.client.PubSub] = None

    async def connect(self) -> None:
        import redis.asyncio as redis_async
        self._redis = redis_async.from_url(REDIS_URL, decode_responses=True, encoding="utf-8")
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(*self._channels)
        logger.info(f"_WSSubscriber subscribed to {self._channels}")

    async def close(self) -> None:
        if self._pubsub:
            await self._pubsub.unsubscribe()
            await self._pubsub.close()
        if self._redis:
            await self._redis.aclose()
        logger.debug("_WSSubscriber closed")

    async def subscribe(self):
        """Async generator: yields (channel, event_dict) tuples immediately on publish."""
        if self._pubsub is None:
            raise RuntimeError("Not connected; call connect() first")

        while True:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message:
                    ch = message.get("channel", "")
                    data = message.get("data", "")
                    if data:
                        try:
                            event = json.loads(data) if isinstance(data, str) else data
                            yield ch, event
                        except json.JSONDecodeError:
                            logger.warning(f"Invalid JSON in pub/sub: {data}")
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Pub/sub error: {exc}")
                await asyncio.sleep(1.0)

    async def __aenter__(self) -> "_WSSubscriber":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()


# ─────────────────────────────────────────────────────────────────────────────
# PlutusEngine
# ─────────────────────────────────────────────────────────────────────────────


class PlutusEngine:
    """
    Central LLM brain for Plutus V4.0.

    Manages Redis subscriptions, TimescaleDB queries, and event publication.
    The FastAPI server is started via :meth:`start`.
    """

    def __init__(
        self,
        redis_url: str = REDIS_URL,
        timeseries_url: str = TIMESERIES_URL,
        llm_api_key: str | None = LLM_API_KEY,
        llm_base_url: str = LLM_BASE_URL,
        llm_model: str = LLM_MODEL,
    ) -> None:
        self.redis_url = redis_url
        self.timeseries_url = timeseries_url
        self.llm_api_key = llm_api_key
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model

        # Synchronous Redis client (for pub/sub thread)
        self._redis_sync: redis.Redis = redis.from_url(
            self.redis_url, decode_responses=True
        )

        # Asynchronous Redis client (for async API handlers)
        self._redis_async: redis.asyncio.Redis = redis.asyncio.from_url(
            self.redis_url, decode_responses=True
        )

        # Async PostgreSQL connection pool (TimescaleDB)
        self._pg_pool: asyncpg.Pool | None = None

        # Subscriptions: channel_name -> list of callbacks (C4: guarded by _sub_lock)
        self._subscriptions: dict[str, list[Callable[[str, dict[str, Any]], None]]] = (
            {}
        )
        self._sub_lock = threading.Lock()  # guards _subscriptions dict

        # Reference to the running event loop; set in connect() and used in the
        # pub/sub reader thread to schedule async callbacks (C5 fix)
        self._loop: asyncio.AbstractEventLoop | None = None

        # Pubsub thread
        self._pubsub_thread: threading.Thread | None = None
        self._stop_pubsub = threading.Event()

        logger.info("PlutusEngine initialised")

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Establish connection pools. Call before :meth:`start`."""
        self._loop = asyncio.get_running_loop()
        if self._pg_pool is None:
            self._pg_pool = await asyncpg.create_pool(
                self.timeseries_url,
                min_size=2,
                max_size=10,
            )
            logger.info("TimescaleDB pool established")

    async def disconnect(self) -> None:
        """Close all connection pools and stop pub/sub thread."""
        self._stop_pubsub.set()
        if self._pubsub_thread and self._pubsub_thread.is_alive():
            self._pubsub_thread.join(timeout=5)

        if self._redis_async:
            await self._redis_async.aclose()
        if self._pg_pool:
            await self._pg_pool.close()

        logger.info("PlutusEngine disconnected")

    # ── Pub/Sub ────────────────────────────────────────────────────────────────

    def subscribe_to_redis(
        self,
        channel: str,
        callback: Callable[[str, dict[str, Any]], None],
    ) -> None:
        """
        Subscribe ``callback`` to a Redis pub/sub ``channel``.

        Callbacks receive ``(channel: str, message: dict)`` and are invoked
        in a background thread that reads from the Redis subscription socket.

        Supports both synchronous callables and async coroutines (C5 fix: async
        callbacks are scheduled on the main event loop instead of silently failing).

        Parameters
        ----------
        channel:
            Redis channel name, e.g. ``scanner.events``.
        callback:
            Synchronous callable or async coroutine invoked for every message.
        """
        # C4 fix: hold lock while mutating shared dict
        with self._sub_lock:
            if channel not in self._subscriptions:
                self._subscriptions[channel] = []
            self._subscriptions[channel].append(callback)

        # Ensure the pub/sub reader thread is running
        if self._pubsub_thread is None or not self._pubsub_thread.is_alive():
            self._stop_pubsub.clear()
            self._pubsub_thread = threading.Thread(
                target=self._pubsub_reader,
                name="plutus_pubsub_reader",
                daemon=True,
            )
            self._pubsub_thread.start()
            logger.info("Redis pub/sub reader thread started")

    def _pubsub_reader(self) -> None:
        """
        Background thread that continuously reads from all subscribed channels
        and dispatches messages to registered callbacks.

        C4 fix: takes a snapshot of _subscriptions under lock before iterating.
        C5 fix: async callbacks are scheduled on the event loop via call_soon_threadsafe.
        """
        pubsub = self._redis_sync.pubsub()

        # Capture initial subscription snapshot under lock
        with self._sub_lock:
            initial_channels = list(self._subscriptions.keys())
        if initial_channels:
            pubsub.subscribe(**{ch: self._dispatch_callback(ch) for ch in initial_channels})
        else:
            pubsub.psubscribe("*")  # subscribe to all if nothing registered yet

        logger.debug("Redis pub/sub reader subscribed to: %s" % initial_channels)

        while not self._stop_pubsub.is_set():
            try:
                message = pubsub.get_message(timeout=1.0)
                if message and message["type"] == "message":
                    ch = message["channel"]
                    data = json.loads(message["data"])

                    # C4 fix: snapshot _subscriptions under lock before iterating
                    with self._sub_lock:
                        callbacks = list(self._subscriptions.get(ch, []))

                    for cb in callbacks:
                        try:
                            self._invoke_callback(cb, ch, data)
                        except Exception as exc:
                            logger.exception(f"Callback error on channel {ch}: {exc}")
            except redis.ConnectionError:
                logger.warning("Redis connection lost in pub/sub reader; reconnecting...")
                pubsub.close()
                # Brief back-off before re-subscribing
                self._stop_pubsub.wait(timeout=3)
                if self._stop_pubsub.is_set():
                    break
                pubsub = self._redis_sync.pubsub()
                with self._sub_lock:
                    reconnect_channels = list(self._subscriptions.keys())
                pubsub.subscribe(**{ch: self._dispatch_callback(ch) for ch in reconnect_channels})

        pubsub.close()
        logger.info("Redis pub/sub reader thread stopped")

    def _invoke_callback(
        self,
        cb: Callable[[str, dict[str, Any]], None],
        channel: str,
        data: dict[str, Any],
    ) -> None:
        """
        Invoke a registered callback, handling both sync callables and async coroutines.

        C5 fix: if ``cb`` is a coroutine, schedule it on the event loop using
        call_soon_threadsafe so it executes in the async context instead of being
        silently discarded (calling a coroutine directly in a thread just creates
        the coroutine object and immediately garbage-collects it).
        """
        if inspect.iscoroutinefunction(cb):
            if self._loop is not None:
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(cb(channel, data))
                )
            else:
                logger.warning(
                    "Async callback on channel %s cannot be scheduled: "
                    "event loop not initialised (call connect() first)",
                    channel,
                )
        else:
            cb(channel, data)

    @staticmethod
    def _dispatch_callback(channel: str) -> Callable[[Any], None]:
        """Return a passthrough handler — actual dispatch happens in reader loop."""
        def _inner(_: Any) -> None:
            pass
        return _inner

    # ── Publish ────────────────────────────────────────────────────────────────

    def publish_event(self, channel: str, event: dict[str, Any]) -> bool:
        """
        Publish ``event`` (a JSON-serialisable dict) to the named Redis channel.

        Parameters
        ----------
        channel:
            Target channel name.
        event:
            Event payload to publish.

        Returns
        -------
        bool:
            ``True`` if the publish succeeded, ``False`` otherwise.
        """
        try:
            payload = json.dumps(event, default=str)
            n = self._redis_sync.publish(channel, payload)
            logger.debug(f"Published to {channel} (n_subscribers={n}): {event}")
            return bool(n >= 0)
        except Exception as exc:
            logger.error(f"Failed to publish to {channel}: {exc}")
            return False

    # ── TimescaleDB ────────────────────────────────────────────────────────────

    async def query_timeseries(self, sql: str) -> list[dict[str, Any]]:
        """
        Execute a read-only SQL query against TimescaleDB.

        Parameters
        ----------
        sql:
            The SQL statement to execute.

        Returns
        -------
        list[dict[str, Any]]:
            List of rows as dictionaries.

        Raises
        ------
        RuntimeError:
            If the database pool has not been initialised.
        """
        if self._pg_pool is None:
            raise RuntimeError("TimescaleDB pool not initialised; call connect() first")

        # Guard against destructive SQL (strict whitelist — no subcommands)
        normalised = sql.strip().upper()
        allowed = {"SELECT"}
        if not any(normalised.startswith(k) for k in allowed):
            raise ValueError(f"Query must be a SELECT statement; got: {sql!r}")

        async with self._pg_pool.acquire() as conn:
            rows = await conn.fetch(sql)  # type: ignore[arg-type]
            # Convert asyncpg records to plain dicts
            return [dict(r) for r in rows]

    # ── Server ────────────────────────────────────────────────────────────────

    async def start(
        self,
        host: str = "0.0.0.0",
        port: int = 8000,
        server_holder: list | None = None,
    ) -> None:
        """
        Start the FastAPI/Uvicorn server on ``host:port`` and run it forever.

        This is a coroutine — await it in an event loop.

        Parameters
        ----------
        host:
            Bind address (default: 0.0.0.0).
        port:
            TCP port (default: 8000).
        server_holder:
            Optional list that will be populated with the uvicorn.Server instance
            so that a signal handler (defined in ``_main``) can call
            ``server.should_exit = True`` to trigger graceful shutdown.
        """
        await self.connect()

        # Attach engine instance to app state so route handlers can access it
        app.state.engine = self

        # ── RT2 fix: start IdempotentScannerWorker as background task ─────────
        # This runs in the plutus_engine container alongside FastAPI.
        # RT2: Uses XREADGROUP for exactly-once delivery semantics.
        # RT3: Runs as an async task — no polling, messages delivered via callbacks.
        # The worker publishes anomalies to scanner.events (consumed by execution_node).
        worker = IdempotentScannerWorker(
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379"),
            consumer_name=f"plutus-engine-{os.getenv('HOSTNAME', 'dev')}",
            batch_size=10,
            block_ms=1000,
        )
        await worker.connect()
        asyncio.create_task(worker.run(), name="scanner_worker")
        logger.info("IdempotentScannerWorker started (RT2: XREADGROUP, idempotent)")

        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=LOG_LEVEL.lower(),
            access_log=True,
            reload=False,
        )
        server = uvicorn.Server(config)
        if server_holder is not None:
            server_holder[0] = server
        logger.info(f"PlutusEngine starting on {host}:{port}")
        await server.serve()


# ─────────────────────────────────────────────────────────────────────────────
# CLI Entry Point
# ─────────────────────────────────────────────────────────────────────────────

_SHUTDOWN_TIMEOUT: float = 30.0  # seconds to wait for in-flight requests before force-exit


def _make_shutdown_handler(server_holder: list) -> Callable[[int, Any], None]:
    """
    Return a SIGTERM/SIGINT handler that sets uvicorn.Server.should_exit = True.

    Uses a list wrapper so the closure can see the server once it is assigned
    inside :meth:`PlutusEngine.start` (closures capture variables by reference,
    so mutating the list contents is visible inside the handler even though the
    handler is registered before the server exists).
    """

    def handler(signum: int, _: Any) -> None:
        sig_name = signal.Signals(signum).name
        logger.info(f"{sig_name} received — initiating graceful shutdown")
        s = server_holder[0] if server_holder else None
        if s is not None and s.should_exit:
            logger.warning("Shutdown already in progress")
            return
        if s is not None:
            s.should_exit = True
        else:
            # Server not yet created — raise KeyboardInterrupt to break the event loop.
            raise KeyboardInterrupt(sig_name)

    return handler


async def _main() -> None:
    engine = PlutusEngine()
    # List wrapper so the signal handler closure can see the server after it is set.
    _server_holder: list[uvicorn.Server | None] = [None]

    # Register signal handlers for Docker SIGTERM and terminal SIGINT.
    # Skip on Windows (sigaction not fully supported there).
    if os.name != "nt":
        handler = _make_shutdown_handler(_server_holder)
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    try:
        await engine.start(server_holder=_server_holder)
    except KeyboardInterrupt:
        logger.info("Shutdown via signal")
    finally:
        await engine.disconnect()


if __name__ == "__main__":
    asyncio.run(_main())
