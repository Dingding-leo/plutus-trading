"""
src/engine/scanner_cli.py
==========================
RT1: Entry point for the scanner Docker service.

Runs BinanceConnector (WebSocket → Redis stream XADD producer).
Listens on port 8002 for health checks.

Usage (Docker):
    docker run plutus_scanner

Environment Variables:
    REDIS_URL           Redis URL (default: redis://localhost:6379)
    SCANNER_SYMBOLS    Comma-separated symbols (default: btcusdt,ethusdt,solusdt)
    SCANNER_CHANNELS   Comma-separated channels (default: depth20@100ms,trade)
    LOG_LEVEL           Logging level (default: INFO)
"""

from __future__ import annotations

import os
import asyncio
import signal
import sys
from pathlib import Path

import loguru

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.engine.realtime_pipeline import BinanceConnector

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
loguru.logger.remove()
loguru.logger.add(
    sys.stderr,
    level=LOG_LEVEL,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> — <level>{message}</level>",
)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
SYMBOLS = [
    s.strip()
    for s in os.getenv("SCANNER_SYMBOLS", "btcusdt,ethusdt,solusdt").split(",")
    if s.strip()
]
CHANNELS = [
    c.strip()
    for c in os.getenv("SCANNER_CHANNELS", "depth20@100ms,trade").split(",")
    if c.strip()
]


async def main() -> None:
    connector = BinanceConnector(
        redis_url=REDIS_URL,
        symbols=SYMBOLS,
        channels=CHANNELS,
    )

    loop = asyncio.get_running_loop()

    # Graceful shutdown on SIGINT/SIGTERM
    shutdown_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig,
            lambda: asyncio.create_task(_shutdown(connector, shutdown_event)),
        )

    # Start health server in background
    import uvicorn
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "service": "plutus_scanner",
            "symbols": SYMBOLS,
            "channels": CHANNELS,
        }

    @app.get("/")
    async def root():
        return {
            "service": "plutus_scanner",
            "description": "Binance WebSocket → Redis Stream producer",
            "symbols": SYMBOLS,
            "channels": CHANNELS,
        }

    server_task = asyncio.create_task(
        asyncio.to_thread(
            uvicorn.run,
            app,
            host="0.0.0.0",
            port=8002,
            log_level=LOG_LEVEL.lower(),
        )
    )

    try:
        await connector.connect()
        loguru.logger.info(
            f"Scanner started | symbols={SYMBOLS} | channels={CHANNELS}"
        )
        # The connector runs in its own thread; just wait for shutdown
        await shutdown_event.wait()
    except asyncio.CancelledError:
        loguru.logger.info("Scanner cancelled")
    finally:
        await connector.disconnect()
        server_task.cancel()


async def _shutdown(
    connector: BinanceConnector,
    shutdown_event: asyncio.Event,
) -> None:
    loguru.logger.info("Shutdown signal received; stopping scanner...")
    shutdown_event.set()
    await connector.stop()


if __name__ == "__main__":
    asyncio.run(main())
