# src/engine/__init__.py
# Plutus V4.0 — Engine Package
#
# Contains the LLM-powered trading brain and its supporting workers.

__version__ = "4.0.0"
__author__ = "Plutus Team"

from src.engine.server import PlutusEngine
from src.engine.scanner_worker import ScannerWorker
from src.engine.realtime_pipeline import (
    BinanceConnector,
    IdempotentScannerWorker,
    SHOCKPhase,
    SHOCKResult,
    RealtimeSignalSubscriber,
)

__all__ = [
    "PlutusEngine",
    "ScannerWorker",
    "BinanceConnector",
    "IdempotentScannerWorker",
    "SHOCKPhase",
    "SHOCKResult",
    "RealtimeSignalSubscriber",
]
