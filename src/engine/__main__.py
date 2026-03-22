"""
src/engine/__main__.py
======================
Plutus V4.0 — Engine Package Entry Point

RT2 fix: Starts IdempotentScannerWorker as a background task alongside FastAPI.

Usage:
    python -m src.engine           # starts PlutusEngine (FastAPI + ScannerWorker)
    python -m src.engine.scanner   # starts BinanceConnector only (legacy)

The docker-compose plutus_engine service uses: ENTRYPOINT ["python", "-m", "src.engine"]
"""

from __future__ import annotations
import sys

# Re-export the PlutusEngine so `python -m src.engine` just starts the engine
from src.engine.server import PlutusEngine


if __name__ == "__main__":
    import asyncio

    async def _main():
        engine = PlutusEngine()
        try:
            await engine.start()
        except KeyboardInterrupt:
            pass
        finally:
            await engine.disconnect()

    asyncio.run(_main())
