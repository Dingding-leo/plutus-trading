"""
Plutus V4.0 — Data Streams Module

Exports real-time and on-demand data clients for the trading system.
"""

from __future__ import annotations

from src.data.streams.binance_websocket import BinanceWebsocketClient, StreamConfig
from src.data.streams.glassnode import GlassnodeClient, GlassnodeMetrics

__all__ = [
    "BinanceWebsocketClient",
    "GlassnodeClient",
    "GlassnodeMetrics",
    "StreamConfig",
]
