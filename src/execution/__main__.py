"""
src/execution/__main__.py
=========================
Plutus V4.0 — Live Execution Node

Service E in the docker-compose architecture.
Subscribes to the ``scanner.events`` Redis pub/sub channel and routes
every anomaly event through the full execution pipeline:

    scanner.events (Redis pub/sub)
        → HybridWorkflowStrategy.analyze_symbol()
        → DecisionEngine.check_execution_gate()    [Connection B]
        → RiskGuard.check_all()                   [Connection C]
        → SmartRouter.route()                      [Connection D]
        → BinanceExecutor.place_order()            [Connection E]
        → RiskGuard.update_position_from_fill()    [Connection F]

Container: plutus_execution_node
Health endpoint: GET http://localhost:8001/health
Metrics endpoint: GET http://localhost:8001/metrics

Environment Variables
--------------------
REDIS_URL         Redis URL   (default: redis://localhost:6379)
EXECUTION_MODE    "test" | "live"  (default: test)
INITIAL_CAPITAL   Starting equity (default: 10000)
RISK_LEVEL        "LOW" | "MODERATE" | "HIGH"  (default: MODERATE)
LOG_LEVEL         Logging level (default: INFO)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import signal
import sys
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Redis async client
import redis.asyncio as redis_async

# HTTP server
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

# ── Plutus imports ──────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.backtest.hybrid_strategy import HybridWorkflowStrategy
from src.backtest.strategy import WorkflowStrategy
from src.execution.decision_engine import DecisionEngine
from src.execution.position_sizer import get_position_multiplier
from src.execution.risk_limits import RiskGuard, RiskLimitExceeded
from src.execution.order_router import (
    SmartRouter,
    TWAPExecutor,
    VWAPExecutor,
    LimitOrderQueue,
    MarketExecutor,
)
from src.execution.exchanges.binance_executor import BinanceExecutor
from src.data.binance_client import get_current_price, fetch_klines
from src.data.coin_tiers import get_tier, normalize_symbol, TIER_PARAMS

# HTTP client for authenticated futures API calls
import requests


# ── Minimal Binance client adapter ─────────────────────────────────────────
# BinanceExecutor expects a client with get_symbol_ticker() and get_account().
# We use a thin adapter around the binance_client module functions.


class MinimalBinanceClient:
    """
    Minimal Binance client adapter for the execution node.

    Wraps the binance_client module's get_current_price() and fetch_klines()
    into the interface expected by BinanceExecutor.

    For live position tracking, BinanceExecutor delegates to RiskGuard which
    is the authoritative source. This client is used only for mark price.
    """

    def __init__(self) -> None:
        self._market = os.getenv("BINANCE_MARKET", "futures")

    def get_symbol_ticker(self, symbol: str) -> dict:
        """Return {"price": str} for the given symbol."""
        price = get_current_price(symbol=symbol, market=self._market)
        return {"price": str(price) if price is not None else "0"}

    def get_account(self) -> dict:
        """
        Return mock account info.

        In the execution node, positions are tracked by RiskGuard.
        This method exists only to satisfy the BinanceExecutor interface.
        """
        return {"balances": []}

    def get_futures_position(self, symbol: str) -> dict:
        """
        Query USD&-M Futures position data via /fapi/v2/positionRisk.

        Returns a dict compatible with BinanceExecutor.get_position():
            {"size": float, "entry_price": float, "unrealized_pnl": float}
        """
        api_key = os.getenv("BINANCE_API_KEY", "")
        api_secret = os.getenv("BINANCE_API_SECRET", "")
        if not api_key or not api_secret:
            return {"size": 0.0, "entry_price": 0.0, "unrealized_pnl": 0.0}

        from src import config
        base_url = config.BINANCE_FUTURES_URL
        endpoint = "/fapi/v2/positionRisk"
        timestamp = int(_time.time() * 1000)
        params = f"symbol={symbol}&timestamp={timestamp}"
        signature = hmac.new(
            api_secret.encode("utf-8"),
            params.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        url = f"{base_url}{endpoint}?{params}&signature={signature}"
        headers = {"X-MBX-APIKEY": api_key}

        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            positions = resp.json()
            if not isinstance(positions, list):
                return {"size": 0.0, "entry_price": 0.0, "unrealized_pnl": 0.0}
            for pos in positions:
                if pos.get("symbol") == symbol and float(pos.get("positionAmt", 0)) != 0:
                    size = float(pos["positionAmt"])
                    entry = float(pos.get("entryPrice", 0))
                    unrealised = float(pos.get("unRealizedProfit", 0))
                    return {
                        "size": size,
                        "entry_price": entry,
                        "unrealized_pnl": unrealised,
                    }
            return {"size": 0.0, "entry_price": 0.0, "unrealized_pnl": 0.0}
        except Exception as exc:
            logger.warning("get_futures_position(%s) failed: %s", symbol, exc)
            return {"size": 0.0, "entry_price": 0.0, "unrealized_pnl": 0.0}

    def fetch_klines(self, symbol: str, interval: str, limit: int) -> list:
        """Proxy to binance_client.fetch_klines for OHLCV data."""
        return fetch_klines(symbol=symbol, interval=interval, limit=limit)

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
)
logger = logging.getLogger("execution_node")


# ── Constants ────────────────────────────────────────────────────────────────

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
EXECUTION_MODE = os.getenv("EXECUTION_MODE", "test")
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "10000"))
RISK_LEVEL = os.getenv("RISK_LEVEL", "MODERATE")
SCANNER_CHANNEL = "scanner.events"   # matches IdempotentScannerWorker.CHANNEL

# Anomaly severity below this threshold is ignored
MIN_SEVERITY = {"medium", "high", "critical"}


# ── FastAPI App ─────────────────────────────────────────────────────────────

app = FastAPI(title="Plutus Execution Node", version="4.0.0")


# ── LiveExecutionNode ───────────────────────────────────────────────────────

class LiveExecutionNode:
    """
    Subscribes to scanner.events pub/sub and routes anomalies through the
    full execution pipeline (Connections B → C → D → E → F).

    Lifecycle:
        1. connect()     — set up Redis, instantiate all components
        2. run()        — main loop: subscribe, process anomalies
        3. disconnect()  — graceful shutdown
    """

    def __init__(
        self,
        redis_url: str = REDIS_URL,
        mode: str = EXECUTION_MODE,
        initial_capital: float = INITIAL_CAPITAL,
        risk_level: str = RISK_LEVEL,
    ) -> None:
        self.redis_url = redis_url
        self.mode = mode
        self.dry_run = mode == "test"

        # ── RiskGuard (singleton, Connection C / F) ──────────────────────────
        self.risk_guard = RiskGuard(
            equity=initial_capital,
            mode="dry_run" if self.dry_run else "live",
            initial_capital=initial_capital,
        )
        self.risk_guard.start_session()

        # ── BinanceExecutor ─────────────────────────────────────────────────
        # MinimalBinanceClient provides the get_symbol_ticker() interface.
        # Position tracking is authoritative via RiskGuard (not this client).
        self._binance = MinimalBinanceClient()
        self.binance_exec = BinanceExecutor(
            binance_client=self._binance,
            test_mode=self.dry_run,
        )

        # ── SmartRouter (all four executors pre-registered, Connection D/E) ─
        # Symbol is replaced per-route; create one executor per type
        self.smart_router = SmartRouter(
            executors={
                "TWAPExecutor": TWAPExecutor(
                    symbol="BTCUSDT", side="BUY", total_quantity=0.0, duration_secs=600,
                ),
                "VWAPExecutor": VWAPExecutor(
                    symbol="BTCUSDT", side="BUY", total_quantity=0.0, participation_rate=0.10,
                ),
                "LimitOrderQueue": LimitOrderQueue(
                    symbol="BTCUSDT", binance_executor=self.binance_exec,
                ),
                "MarketExecutor": MarketExecutor(
                    symbol="BTCUSDT", side="BUY", quantity=0.0, mid_price=0.0,
                ),
            },
            binance_exec=self.binance_exec,
        )

        # ── DecisionEngine (Connection B) ────────────────────────────────────
        self.decision_engine = DecisionEngine()

        # ── Strategy ─────────────────────────────────────────────────────────
        self.strategy = HybridWorkflowStrategy(use_llm=False)

        # ── Redis ────────────────────────────────────────────────────────────
        self._redis: Optional[redis_async.Redis] = None
        self._pubsub: Optional[redis_async.client.PubSub] = None
        self._running = False

        # ── Position multiplier ──────────────────────────────────────────────
        self.pos_mult = get_position_multiplier(risk_level)
        self.equity = initial_capital
        self.risk_pct = 0.01

        logger.info(
            "LiveExecutionNode created | mode=%s dry_run=%s equity=%.2f risk=%s pos_mult=%.2f",
            mode, self.dry_run, initial_capital, risk_level, self.pos_mult,
        )

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Initialize Redis client and pub/sub subscription."""
        self._redis = redis_async.from_url(
            self.redis_url, decode_responses=True, encoding="utf-8",
        )
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(SCANNER_CHANNEL)
        logger.info("Connected to Redis | subscribed to %s", SCANNER_CHANNEL)

    async def disconnect(self) -> None:
        """Close Redis connection gracefully."""
        if self._pubsub:
            await self._pubsub.unsubscribe(SCANNER_CHANNEL)
            await self._pubsub.close()
        if self._redis:
            await self._redis.aclose()
        logger.info("Disconnected from Redis")

    # ── Main Loop ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Subscribe to scanner.events and process anomalies indefinitely.

        Uses redis.asyncio PubSub which is push-based (not polling).
        RT3 fix: no 1-second polling loop — messages are delivered as they arrive.
        """
        if self._pubsub is None:
            raise RuntimeError("Not connected; call connect() first")

        self._running = True
        logger.info("Execution node run loop starting")

        try:
            async for message in self._pubsub.listen():
                if not self._running:
                    break
                if message["type"] != "message":
                    continue
                await self._on_anomaly(message)
        except asyncio.CancelledError:
            logger.info("Execution node loop cancelled")
        finally:
            logger.info("Execution node run loop stopped")

    async def stop(self) -> None:
        """Signal the run loop to exit on next iteration."""
        self._running = False
        logger.info("Stop requested")

    # ── Per-Message Handler ─────────────────────────────────────────────────

    async def _on_anomaly(self, message: dict) -> None:
        """
        Process one anomaly published to scanner.events.

        Pipeline:
            1. Parse payload
            2. Filter by severity
            3. Fetch OHLCV candles
            4. HybridWorkflowStrategy.analyze_symbol() → technical signal
            5. DecisionEngine.check_execution_gate()   [Connection B]
            6. RiskGuard.check_all()                  [Connection C]
            7. SmartRouter.route()                    [Connection D]
            8. BinanceExecutor + RiskGuard.update_position_from_fill()  [Connection E/F]
        """
        try:
            payload = json.loads(message["data"])
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("[Node] Failed to parse anomaly payload: %s", exc)
            return

        symbol: str = payload.get("symbol", "")
        event_type: str = payload.get("event_type", "")
        severity: str = payload.get("severity", "low")
        metadata: dict = payload.get("metadata", {})

        if severity not in MIN_SEVERITY:
            logger.debug(
                "[Node] Skipping sub-threshold anomaly | symbol=%s severity=%s",
                symbol, severity,
            )
            return

        logger.info(
            "[Node] Anomaly received | symbol=%s type=%s severity=%s",
            symbol, event_type, severity,
        )

        # ── Step 1: Fetch OHLCV candles ─────────────────────────────────────
        try:
            candles_1h = self._binance.fetch_klines(symbol, "1h", 200)
            candles_4h = self._binance.fetch_klines(symbol, "4h", 200)
        except Exception as exc:
            logger.warning("[Node] Failed to fetch candles for %s: %s", symbol, exc)
            return

        if len(candles_1h) < 50:
            logger.warning("[Node] Insufficient candles for %s", symbol)
            return

        # ── Step 2: Technical analysis ──────────────────────────────────────
        now = datetime.now(timezone.utc)
        ts_int = int(now.timestamp())

        data = {"1h": candles_1h, "4h": candles_4h}
        analysis = self.strategy._core.analyze_symbol(symbol, data, ts_int)

        if not analysis:
            logger.debug("[Node] No technical signal for %s", symbol)
            return

        quality = analysis.get("quality", 0)
        if quality < 5:
            logger.debug(
                "[Node] Low quality signal for %s (quality=%d < 5)",
                symbol, quality,
            )
            return

        # ── Step 3: Execution gate ───────────────────────────────────────────
        structure_break = severity in ("high", "critical")
        macro_aligned = True   # TODO: wire LLM macro context here
        invalidation_clear = True  # TODO: wire support level check

        gate_result = self.decision_engine.check_execution_gate(
            structure_break=structure_break,
            macro_aligned=macro_aligned,
            invalidation_clear=invalidation_clear,
            rr=analysis.get("rr", 0),
            min_rr=1.5,
        )

        if not gate_result["pass"]:
            logger.info(
                "[Node] Execution gate FAILED | symbol=%s reason=%s",
                symbol, gate_result.get("failed_check", "unknown"),
            )
            return

        # ── Step 4: Entry setup from strategy ───────────────────────────────
        direction = analysis.get("signal", "").upper()
        if direction not in ("BUY", "SELL"):
            logger.debug("[Node] No directional signal for %s", symbol)
            return

        entry_price = analysis.get("current_price", 0)
        stop_loss = analysis.get("stop_loss", 0)
        take_profit = analysis.get("target", 0)
        stop_distance = abs(entry_price - stop_loss) / entry_price if entry_price > 0 else 0
        rr = (
            abs(take_profit - entry_price) / abs(entry_price - stop_loss)
            if entry_price > 0 and stop_loss > 0 else 0
        )

        if stop_distance < 0.005:
            logger.warning("[Node] Stop distance %.3f%% too tight for %s", stop_distance * 100, symbol)
            return

        # ── Step 5: Position sizing ─────────────────────────────────────────
        from src.execution import position_sizer

        symbol_base = normalize_symbol(symbol)
        tier = get_tier(symbol_base)
        params = TIER_PARAMS.get(tier, TIER_PARAMS["TIER_4"])
        coin_type = "major" if tier in ("TIER_1", "TIER_2") else "small"
        leverage_cap = params["max_leverage"]

        position = position_sizer.calculate_position_size(
            equity=self.equity,
            risk_pct=self.risk_pct,
            stop_distance=stop_distance,
            pos_mult=self.pos_mult,
            coin_type=coin_type,
            training_mode=False,
            risk_level=RISK_LEVEL,
        )

        if not position.get("valid", False):
            logger.warning("[Node] Position sizing invalid for %s: %s", symbol, position.get("error"))
            return

        notional = position.get("max_position", 0)
        recommended_leverage = min(position.get("recommended_leverage", 1), leverage_cap)
        quantity = notional / entry_price if entry_price > 0 else 0

        # ── Step 6: RiskGuard.check_all() ────────────────────────────────────
        distance_to_liquidation = max(stop_distance - 0.005, 0.001)

        try:
            self.risk_guard.check_all(
                proposed_notional=notional,
                proposed_leverage=recommended_leverage,
                risk_environment=f"{RISK_LEVEL.lower()}_risk",
                current_exposure={},
                proposed_exposure={symbol: notional},
                distance_to_liquidation_pct=distance_to_liquidation,
                coin_type=coin_type,
                training_mode=False,
                equity=self.equity,
            )
        except RiskLimitExceeded as e:
            logger.warning(
                "[Node] RiskGuard BLOCKED | symbol=%s limit=%s reason=%s",
                symbol, e.limit_name, e.reason,
            )
            return

        # ── Step 7: SmartRouter.route() → BinanceExecutor ────────────────────
        intent = "passive_fvg" if severity == "critical" else "twap_sniper"

        order_intent = {
            "intent": intent,
            "symbol": symbol,
            "side": direction,
            "quantity": quantity,
            "expected_price": entry_price,
        }

        if intent == "twap_sniper":
            order_intent["duration_secs"] = 300
            order_intent["slice_interval_secs"] = 60

        if self.dry_run:
            logger.info(
                "[DRY-RUN] Would route order | symbol=%s side=%s qty=%.6f "
                "price=%.4f intent=%s leverage=%d notional=%.2f",
                symbol, direction, quantity, entry_price, intent,
                recommended_leverage, notional,
            )
            return

        # Live mode: route the order
        try:
            executor_name = self.smart_router.route(order_intent)
            logger.info(
                "[LIVE] Order routed | symbol=%s executor=%s intent=%s",
                symbol, executor_name, intent,
            )

            # ── Step 8: Update RiskGuard on fill ─────────────────────────────
            fill_report = self.smart_router.collect_fill_report()
            for fill in fill_report.get("fill_history", []):
                self.risk_guard.update_position_from_fill(
                    symbol=symbol,
                    side=fill.get("side", direction),
                    notional=fill.get("fill_qty", 0) * fill.get("fill_price", 0),
                    quantity=fill.get("fill_qty", 0),
                    price=fill.get("fill_price", 0),
                )
            # Update equity based on fill
            if fill_report.get("fill_history"):
                latest = fill_report["fill_history"][-1]
                self.equity = self.risk_guard.equity

        except Exception as exc:
            logger.exception("[Node] Order routing failed for %s: %s", symbol, exc)

    # ── Status ──────────────────────────────────────────────────────────────

    def status_report(self) -> dict:
        """Return current system status for healthcheck endpoint."""
        return {
            "status": "running" if self._running else "stopped",
            "mode": self.mode,
            "equity": self.risk_guard.equity,
            "initial_capital": INITIAL_CAPITAL,
            "risk_level": RISK_LEVEL,
            "pos_mult": self.pos_mult,
            "risk_guard": self.risk_guard.status_report(),
        }


# ── FastAPI Routes ──────────────────────────────────────────────────────────

_node: Optional[LiveExecutionNode] = None


@app.on_event("startup")
async def startup():
    global _node
    _node = LiveExecutionNode()
    await _node.connect()

    # Run the subscriber loop as a background task (does NOT block the HTTP server)
    asyncio.create_task(_node.run(), name="execution_node_run")
    logger.info("Execution node started")


@app.on_event("shutdown")
async def shutdown():
    global _node
    if _node:
        await _node.stop()
        await _node.disconnect()
    logger.info("Execution node stopped")


@app.get("/health")
async def health():
    if _node is None:
        return JSONResponse({"status": "starting"}, status_code=503)
    return _node.status_report()


@app.get("/metrics")
async def metrics():
    if _node is None:
        return JSONResponse({"status": "starting"}, status_code=503)
    return _node.status_report()


# ── CLI Entry Point ─────────────────────────────────────────────────────────

async def _main() -> None:
    global _node

    logger.info("Starting execution node...")
    _node = LiveExecutionNode()

    loop = asyncio.get_running_loop()

    # Graceful shutdown on SIGINT/SIGTERM
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown()))

    await _node.connect()
    try:
        await _node.run()
    finally:
        await _node.disconnect()


async def _shutdown() -> None:
    global _node
    logger.info("Shutdown signal received...")
    if _node:
        await _node.stop()
        await _node.disconnect()
    sys.exit(0)


if __name__ == "__main__":
    uvicorn.run(
        "src.execution.__main__:app",
        host="0.0.0.0",
        port=8001,
        log_level=LOG_LEVEL.lower(),
        reload=False,
    )
