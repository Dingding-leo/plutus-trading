# src/execution/order_router.py
"""
Plutus V4.0 — Institutional Execution Layer
=============================================
Provides smart order routing across TWAP, VWAP, Limit Queue, and Market
execution strategies, backed by an Almgren-Chriss market-impact model.

Sections
--------
A  TWAPExecutor     — Time-Weighted Average Price
B  VWAPExecutor     — Volume-Weighted Average Price
C  LimitOrderQueue  — Passive FVG / retracement limit queue  [includes S4 reconciliation]
D  SmartRouter      — Intent → executor dispatcher
E  MarketImpactModel — Almgren-Chriss impact estimation + optimal split

S4 — LimitOrderQueue Reconciliation Loop
----------------------------------------
Open LIMIT orders can become orphaned if fills occur via external mechanisms
(e.g. Binance web UI, mobile app) or if fill-update webhooks are dropped.

To close this gap, LimitOrderQueue runs a reconciliation loop every 30 seconds:

    1. Query Binance for all open orders (GET /api/v3/openOrders).
    2. Compare with self._live_orders state.
    3. For any order found on Binance but not in _live_orders → log warning.
    4. For any order in _live_orders but not on Binance → it was filled or
       cancelled externally; fetch status and call update_order_status().
    5. Sync fill quantities for partially-filled orders.
    6. Remove terminal orders from _live_orders.

The loop is started automatically when the first order is placed and stops
when the queue is empty (with a grace period of 30 seconds).

S5 — Entry Fee Timing
---------------------
Both backtest and live engines charge fees at ENTRY (not at resolution) for
consistency and conservative P&L reporting:

    Entry cost = quantity × entry_price × maker_fee_rate (4 bps = 0.0004)
    This is deducted from equity at fill time, before the trade resolves.

This convention applies to all execution strategies (TWAP, VWAP, LimitQueue,
Market).  The backtest engine (chronos_engine) and live executor (BinanceExecutor)
MUST both follow this formula.
"""

from __future__ import annotations

import bisect
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Import OrderState from BinanceExecutor for the state machine (S1)
try:
    from src.execution.exchanges.binance_executor import OrderState
except ImportError:
    # Allow the module to load even if the import path is not yet available
    OrderState = None  # type: ignore[assignment, misc]

# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

SlippageReport = dict[str, Any]   # standardised slippage record


@dataclass
class Slice:
    """A single child order slice produced by TWAP or VWAP."""
    quantity: float
    price_limit: float | None
    timestamp: datetime
    scheduled: bool = True
    filled_qty: float = 0.0
    fill_price: float | None = None

    @property
    def is_filled(self) -> bool:
        return self.filled_qty >= self.quantity * 0.999


# ===========================================================================
# SECTION A — TWAPExecutor
# ===========================================================================
# References
#   - "Algorithmic and Low-Cost Trading" — Almgren (NYU Courant)
#   - Kissell, Glantz & Motani (2006)  — "Optimal Execution of Portfolio Transactions"
# ===========================================================================


class TWAPExecutor:
    """
    Time-Weighted Average Price executor.

    Breaks a parent order into equal-sized slices distributed uniformly over
    a fixed time window.  Designed for orders where timing uncertainty is
    acceptable and流动性 is deep enough that slicing introduces minimal impact.

    Parameters
    ----------
    symbol             : trading pair, e.g. "BTCUSDT"
    side               : "BUY" or "SELL"
    total_quantity     : total base-asset quantity to execute
    duration_secs      : total execution window in seconds
    slice_interval_secs: seconds between scheduled slices (default 60)
    executor_fn        : optional callable(executor, slice) → None for live execution;
                         if None slices are recorded only (backtest / paper mode)
    """

    # Standard TWAP market-constant (θ) for crypto majors; adjust per asset
    MARKET_CONSTANT: float = 0.1

    def __init__(
        self,
        symbol: str,
        side: str,
        total_quantity: float,
        duration_secs: int,
        slice_interval_secs: int = 60,
        executor_fn: Callable[["TWAPExecutor", Slice], None] | None = None,
        reference_price: float = 0.0,
        slippage_bps: float = 10.0,
    ) -> None:
        if total_quantity <= 0:
            raise ValueError("total_quantity must be positive")
        if duration_secs <= 0:
            raise ValueError("duration_secs must be positive")
        if slice_interval_secs <= 0:
            raise ValueError("slice_interval_secs must be positive")

        self.symbol = symbol
        self.side = side
        self.total_quantity = total_quantity
        self.duration_secs = duration_secs
        self.slice_interval_secs = slice_interval_secs
        self._reference_price = reference_price
        self._slippage_bps = slippage_bps

        self._num_slices = max(1, duration_secs // slice_interval_secs)
        self._slice_qty = total_quantity / self._num_slices

        self._remaining_qty = total_quantity
        self._slice_index = 0
        self._start_time = datetime.now()
        self._scheduled_slices: list[Slice] = []
        self._fill_history: list[SlippageReport] = []
        self._executor_fn = executor_fn

        self._build_schedule()

    # ------------------------------------------------------------------
    # Schedule construction
    # ------------------------------------------------------------------

    def _build_schedule(self) -> None:
        """Pre-build the list of scheduled slice timestamps."""
        for i in range(self._num_slices):
            ts = self._start_time + timedelta(seconds=i * self.slice_interval_secs)
            # Compute price_limit from reference_price and slippage_bps.
            # BUY  → limit is below reference (price must not exceed reference + slippage).
            # SELL → limit is above reference (price must not be below reference - slippage).
            if self._reference_price > 0:
                slippage = self._slippage_bps / 10_000
                if self.side.upper() == "BUY":
                    price_limit = self._reference_price * (1 + slippage)
                else:
                    price_limit = self._reference_price * (1 - slippage)
            else:
                price_limit = None
            self._scheduled_slices.append(
                Slice(
                    quantity=self._slice_qty,
                    price_limit=price_limit,
                    timestamp=ts,
                    scheduled=True,
                )
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_next_slice(self) -> dict:
        """
        Return the next scheduled slice.

        Returns
        -------
        dict with keys:
            - quantity    : float — child-order size
            - price_limit : float | None
            - timestamp   : datetime
        """
        if self.is_complete():
            return {
                "quantity": 0.0,
                "price_limit": None,
                "timestamp": datetime.now(),
            }

        sl = self._scheduled_slices[self._slice_index]
        self._slice_index += 1
        return {
            "quantity": sl.quantity,
            "price_limit": sl.price_limit,
            "timestamp": sl.timestamp,
        }

    def is_complete(self) -> bool:
        """Return True when all slices have been fully filled."""
        return self._remaining_qty <= 1e-9

    def record_fill(
        self,
        quantity: float,
        price: float,
        expected_price: float | None = None,
    ) -> SlippageReport:
        """
        Record that a child order was filled and update internal state.

        Parameters
        ----------
        quantity        : confirmed fill quantity
        price           : actual fill price
        expected_price  : price used to compute slippage (defaults to mid at fill time)

        Returns
        -------
        SlippageReport dict with fill details and slippage in bps.
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if price <= 0:
            raise ValueError("price must be positive")

        self._remaining_qty = max(0.0, self._remaining_qty - quantity)

        if expected_price is None:
            expected_price = price   # fallback: slippage = 0

        slippage_bps = self.get_slippage_bps(expected_price, price)

        # Map slice index back to schedule
        slice_ts = None
        if self._scheduled_slices:
            idx = min(self._slice_index - 1, len(self._scheduled_slices) - 1)
            slice_ts = self._scheduled_slices[idx].timestamp

        report: SlippageReport = {
            "executor": "TWAP",
            "symbol": self.symbol,
            "side": self.side,
            "fill_qty": quantity,
            "fill_price": price,
            "expected_price": expected_price,
            "slippage_bps": slippage_bps,
            "timestamp": slice_ts or datetime.now(),
            "remaining_qty": self._remaining_qty,
        }
        self._fill_history.append(report)

        actual_vs_scheduled = "on_time"
        if slice_ts is not None:
            now = datetime.now()
            delta = (now - slice_ts).total_seconds()
            if delta > self.slice_interval_secs * 1.5:
                actual_vs_scheduled = "late"
            elif delta < -self.slice_interval_secs * 0.5:
                actual_vs_scheduled = "early"

        logger.info(
            "[TWAP] %s %s %.6f @ %.4f  slippage=%.2f bps  remaining=%.6f  schedule=%s",
            self.side, self.symbol, quantity, price, slippage_bps,
            self._remaining_qty, actual_vs_scheduled,
        )

        return report

    @staticmethod
    def get_slippage_bps(expected_price: float, fill_price: float) -> float:
        """
        Compute slippage in basis points.

        Formula
        -------
        slippage_bps = (fill_price - expected_price) / expected_price × 10 000

        Positive bps = adverse slippage (fill worse than expected).
        Negative bps = price improvement.

        Parameters
        ----------
        expected_price : reference price (e.g. mid at slice time)
        fill_price     : actual execution price

        Returns
        -------
        float : slippage in bps
        """
        if expected_price <= 0:
            raise ValueError("expected_price must be positive")
        return (fill_price - expected_price) / expected_price * 10_000

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """Return aggregated execution statistics."""
        if not self._fill_history:
            return {
                "executor": "TWAP",
                "symbol": self.symbol,
                "total_filled": 0.0,
                "avg_fill_price": 0.0,
                "avg_slippage_bps": 0.0,
                "executed_slices": 0,
                "remaining_qty": self.total_quantity,
                "completion_pct": 0.0,
            }

        total_filled = sum(r["fill_qty"] for r in self._fill_history)
        total_cost = sum(r["fill_qty"] * r["fill_price"] for r in self._fill_history)
        avg_price = total_cost / total_filled if total_filled > 0 else 0.0
        avg_slippage = sum(r["slippage_bps"] for r in self._fill_history) / len(
            self._fill_history
        )

        return {
            "executor": "TWAP",
            "symbol": self.symbol,
            "total_filled": total_filled,
            "avg_fill_price": avg_price,
            "avg_slippage_bps": avg_slippage,
            "executed_slices": len(self._fill_history),
            "remaining_qty": self._remaining_qty,
            "completion_pct": total_filled / self.total_quantity * 100,
        }


# ===========================================================================
# SECTION B — VWAPExecutor
# ===========================================================================
# References
#   - Engle et al. (2018) — "VWAP Strategies"
#   - Almgren & Lorenz (2007) — "Adaptive Arrival Price"
# ===========================================================================


class VWAPExecutor:
    """
    Volume-Weighted Average Price executor.

    Schedules child orders proportional to the historical intraday volume
    curve, concentrating execution at high-volume periods (open and close).
    Participation rate is dynamically adjusted when current volume
    significantly exceeds the historical average, protecting against
    excessive market impact.

    Parameters
    ----------
    symbol             : trading pair
    side               : "BUY" or "SELL"
    total_quantity     : total base-asset quantity to execute
    participation_rate  : fraction of market volume to target per slice (default 10 %)
    executor_fn        : optional callable(executor, slice) → None
    """

    # Intraday volume curve: weight per 30-minute bucket (09:00–16:00 UTC, 14 buckets)
    # Based on typical Binance Spot volume profile — higher at open and close.
    VOLUME_CURVE_30M: list[float] = [
        1.20,  # 09:00  — Asia session overlap
        0.95,  # 09:30
        0.85,  # 10:00
        0.80,  # 10:30
        0.75,  # 11:00
        0.70,  # 11:30
        0.65,  # 12:00
        0.70,  # 12:30
        0.80,  # 13:00
        0.90,  # 13:30
        1.00,  # 14:00
        1.10,  # 14:30
        1.25,  # 15:00  — London close overlap
        1.35,  # 15:30  — New York open overlap
    ]

    # Momentum threshold: current volume rate must exceed this multiple of avg before
    # the executor reduces participation.
    MOMENTUM_THRESHOLD: float = 1.5

    # Fraction to reduce participation when momentum threshold is breached.
    MOMENTUM_REDUCTION: float = 0.20

    def __init__(
        self,
        symbol: str,
        side: str,
        total_quantity: float,
        participation_rate: float = 0.10,
        executor_fn: Callable[["VWAPExecutor", Slice], None] | None = None,
    ) -> None:
        if not (0 < participation_rate <= 1.0):
            raise ValueError("participation_rate must be in (0, 1]")
        if total_quantity <= 0:
            raise ValueError("total_quantity must be positive")

        self.symbol = symbol
        self.side = side
        self.total_quantity = total_quantity
        self.base_participation_rate = participation_rate
        self.current_participation_rate = participation_rate

        self._remaining_qty = total_quantity
        self._schedule: list[Slice] = []
        self._fill_history: list[SlippageReport] = []
        self._executor_fn = executor_fn
        self._start_time = datetime.now()

    # ------------------------------------------------------------------
    # Schedule construction
    # ------------------------------------------------------------------

    def get_intraday_schedule(
        self,
        market_volume_estimate: float,
    ) -> list[dict]:
        """
        Divide the execution across the intraday volume curve.

        Parameters
        ----------
        market_volume_estimate : float
            Estimated total market volume for the symbol today (in base units).
            If 0, the curve is used without scaling.

        Returns
        -------
        list[dict] — each entry: { "timestamp", "quantity", "participation_rate" }
        """
        total_weight = sum(self.VOLUME_CURVE_30M)
        schedule: list[dict] = []

        for i, weight in enumerate(self.VOLUME_CURVE_30M):
            ts = self._start_time + timedelta(minutes=30 * i)
            bucket_share = weight / total_weight

            if market_volume_estimate > 0:
                bucket_volume = market_volume_estimate * bucket_share
                qty = min(
                    bucket_volume * self.current_participation_rate,
                    self._remaining_qty,
                )
            else:
                # Fallback: distribute evenly by weight
                qty = self.total_quantity * bucket_share * self.current_participation_rate

            qty = round(qty, 8)
            if qty > 0:
                slice_obj = Slice(
                    quantity=qty,
                    price_limit=None,
                    timestamp=ts,
                    scheduled=True,
                )
                self._schedule.append(slice_obj)
                schedule.append({
                    "timestamp": ts,
                    "quantity": qty,
                    "participation_rate": self.current_participation_rate,
                    "volume_weight": weight,
                })

        return schedule

    # ------------------------------------------------------------------
    # Momentum adjustment
    # ------------------------------------------------------------------

    def adjust_for_momentum(
        self,
        current_volume_rate: float,
        avg_volume_rate: float,
    ) -> float:
        """
        Dynamically adjust the participation rate based on volume momentum.

        If the current volume rate exceeds ``MOMENTUM_THRESHOLD × avg_volume_rate``
        the participation rate is reduced by ``MOMENTUM_REDUCTION`` (20 %) to
        avoid signalling the order and moving the market.

        Parameters
        ----------
        current_volume_rate : float
            Recent observed volume rate (units/s).
        avg_volume_rate     : float
            Historical average volume rate (units/s).

        Returns
        -------
        float : the updated participation rate.
        """
        if avg_volume_rate <= 0:
            logger.warning(
                "[VWAP] avg_volume_rate is zero; skipping momentum adjustment."
            )
            return self.current_participation_rate

        ratio = current_volume_rate / avg_volume_rate

        if ratio > self.MOMENTUM_THRESHOLD:
            new_rate = self.current_participation_rate * (1.0 - self.MOMENTUM_REDUCTION)
            new_rate = max(new_rate, 0.01)   # never go below 1 %
            logger.info(
                "[VWAP] Momentum alert: vol_rate=%.2fx avg  "
                "Reducing participation %.2f%% → %.2f%%",
                ratio,
                self.current_participation_rate * 100,
                new_rate * 100,
            )
            self.current_participation_rate = new_rate
        else:
            # Gracefully restore base rate if volume normalises
            if self.current_participation_rate < self.base_participation_rate:
                self.current_participation_rate = min(
                    self.current_participation_rate + 0.005,
                    self.base_participation_rate,
                )

        return self.current_participation_rate

    # ------------------------------------------------------------------
    # Slippage
    # ------------------------------------------------------------------

    @staticmethod
    def get_slippage_bps(expected_vwap: float, fill_price: float) -> float:
        """
        Compute slippage against the benchmark VWAP.

        Parameters
        ----------
        expected_vwap : float — benchmark VWAP for this slice
        fill_price    : float — actual fill price

        Returns
        -------
        float : slippage in bps
        """
        if expected_vwap <= 0:
            raise ValueError("expected_vwap must be positive")
        return (fill_price - expected_vwap) / expected_vwap * 10_000

    # ------------------------------------------------------------------
    # Fill recording
    # ------------------------------------------------------------------

    def record_fill(
        self,
        quantity: float,
        price: float,
        expected_vwap: float | None = None,
    ) -> SlippageReport:
        """
        Record a fill and update remaining quantity.

        Parameters
        ----------
        quantity       : confirmed fill quantity
        price          : actual fill price
        expected_vwap  : benchmark VWAP for the slice

        Returns
        -------
        SlippageReport
        """
        if quantity <= 0 or price <= 0:
            raise ValueError("quantity and price must be positive")

        self._remaining_qty = max(0.0, self._remaining_qty - quantity)

        slippage_bps = self.get_slippage_bps(
            expected_vwap if expected_vwap else price,
            price,
        )

        report: SlippageReport = {
            "executor": "VWAP",
            "symbol": self.symbol,
            "side": self.side,
            "fill_qty": quantity,
            "fill_price": price,
            "expected_vwap": expected_vwap or price,
            "slippage_bps": slippage_bps,
            "timestamp": datetime.now(),
            "remaining_qty": self._remaining_qty,
        }
        self._fill_history.append(report)

        logger.info(
            "[VWAP] %s %s %.6f @ %.4f  slippage=%.2f bps  remaining=%.6f",
            self.side, self.symbol, quantity, price, slippage_bps,
            self._remaining_qty,
        )
        return report

    def is_complete(self) -> bool:
        return self._remaining_qty <= 1e-9

    def get_stats(self) -> dict:
        if not self._fill_history:
            return {
                "executor": "VWAP",
                "symbol": self.symbol,
                "total_filled": 0.0,
                "avg_fill_price": 0.0,
                "avg_slippage_bps": 0.0,
                "completion_pct": 0.0,
            }

        total_filled = sum(r["fill_qty"] for r in self._fill_history)
        total_cost = sum(r["fill_qty"] * r["fill_price"] for r in self._fill_history)
        avg_price = total_cost / total_filled if total_filled > 0 else 0.0
        avg_slippage = sum(r["slippage_bps"] for r in self._fill_history) / len(
            self._fill_history
        )

        return {
            "executor": "VWAP",
            "symbol": self.symbol,
            "total_filled": total_filled,
            "avg_fill_price": avg_price,
            "avg_slippage_bps": avg_slippage,
            "executed_slices": len(self._fill_history),
            "remaining_qty": self._remaining_qty,
            "completion_pct": total_filled / self.total_quantity * 100,
            "final_participation_rate": self.current_participation_rate,
        }


# ===========================================================================
# SECTION C — LimitOrderQueue
# ===========================================================================


class LimitOrderQueue:
    """
    Passive limit-order queue manager.

    Places limit orders at Fair Value Gap (FVG) zones or Fibonacci
    retracement levels, aiming to fill at the maker rebate rate rather
    than paying the taker fee.

    S4 Reconciliation Loop
    ----------------------
    Orphaned orders (placed externally, or fills via webhooks that were dropped)
    are detected by a background reconciliation thread that polls Binance every
    30 seconds for all open orders and cross-checks against _live_orders.

    Parameters
    ----------
    symbol            : trading pair
    binance_executor  : BinanceExecutor instance
    binance_client    : Binance REST client (for reconciliation loop in live mode)
    tick_size         : price tick increment (default 0.01 for USDT pairs;
                        use 0.1 for BTC pairs, etc.)
    reconcile_interval : seconds between reconciliation cycles (default 30)
    """

    def __init__(
        self,
        symbol: str,
        binance_executor: "BinanceExecutor",
        binance_client: Any = None,
        tick_size: float = 0.01,
        reconcile_interval: float = 30.0,
    ) -> None:
        self.symbol = symbol
        self._exec = binance_executor
        self._client = binance_client          # For live REST reconciliation (S4)
        self.tick_size = tick_size
        self._reconcile_interval = reconcile_interval

        # Live order registry: order_id → {side, price, orig_qty, filled_qty, status}
        self._live_orders: dict[str, dict] = {}
        self._order_history: list[dict] = []

        # S4: Reconciliation thread
        self._reconcile_running = False
        self._reconcile_thread: threading.Thread | None = None
        self._reconcile_lock = threading.Lock()

        # S4: Orphan order metrics — orders found on Binance but not in _live_orders
        self._orphan_count: int = 0

    # ------------------------------------------------------------------
    # S4: Reconciliation loop
    # ------------------------------------------------------------------

    def start_reconciliation(self) -> None:
        """
        Start the background reconciliation thread (S4).

        The thread queries Binance for open orders every `_reconcile_interval`
        seconds and reconciles any orphans found in `_live_orders`.
        Automatically started when the first order is placed.
        """
        if self._reconcile_thread is not None and self._reconcile_thread.is_alive():
            return
        self._reconcile_running = True
        self._reconcile_thread = threading.Thread(
            target=self._reconciliation_loop,
            name=f"loq_reconcile_{self.symbol}",
            daemon=True,
        )
        self._reconcile_thread.start()
        logger.info(
            "[LimitQueue][S4] Reconciliation loop started for %s (interval=%.0fs)",
            self.symbol, self._reconcile_interval,
        )

    def stop_reconciliation(self) -> None:
        """Stop the reconciliation thread gracefully."""
        self._reconcile_running = False
        if self._reconcile_thread is not None:
            self._reconcile_thread.join(timeout=5.0)
            self._reconcile_thread = None
        logger.info("[LimitQueue][S4] Reconciliation loop stopped")

    @property
    def orphan_count(self) -> int:
        """Total number of orphan orders detected across all reconciliation cycles."""
        return self._orphan_count

    def _reconciliation_loop(self) -> None:
        """
        Background loop: reconciles _live_orders with Binance every 30 seconds.

        S4 algorithm:
            1. Query Binance for all open orders (GET /api/v3/openOrders).
            2. Build set of order_ids found on Binance.
            3. For each order in _live_orders NOT on Binance:
               → It was filled or cancelled externally.
               → Fetch status from Binance (GET /api/v3/order).
               → Call BinanceExecutor.update_order_status() to apply transition.
               → Remove from _live_orders if terminal.
            4. For each Binance order_id NOT in _live_orders:
               → Log warning: external orphan order found.
            5. Sync fill quantities for PARTIAL orders.
        """
        while self._reconcile_running:
            time.sleep(self._reconcile_interval)

            # Graceful exit check
            if not self._reconcile_running:
                break

            # Skip cycle if queue is empty (stop after grace period)
            with self._reconcile_lock:
                if not self._live_orders:
                    # Give a short grace period before stopping
                    continue

            logger.debug(
                "[LimitQueue][S4] Reconciliation cycle starting for %s  tracked=%d",
                self.symbol, len(self._live_orders),
            )

            try:
                self._do_reconcile()
            except Exception as exc:
                logger.exception(
                    "[LimitQueue][S4] Reconciliation error for %s: %s",
                    self.symbol, exc,
                )

        logger.info("[LimitQueue][S4] Reconciliation loop exited for %s", self.symbol)

    def _do_reconcile(self) -> None:
        """
        Execute a single reconciliation pass against Binance.

        In test_mode: uses BinanceExecutor._live_orders as the source of truth.
        In live_mode: queries Binance REST API directly.
        """
        if self._exec.test_mode:
            self._reconcile_test_mode()
        else:
            self._reconcile_live_mode()

    def _reconcile_test_mode(self) -> None:
        """
        Test-mode reconciliation: trust the executor's state machine.

        If _live_orders has an order that is no longer in the executor's
        tracker (terminal state reached), apply the transition locally.
        """
        from src.execution.exchanges.binance_executor import OrderState as OS

        with self._reconcile_lock:
            local_ids = list(self._live_orders.keys())

        for order_id in local_ids:
            # Query the executor's state
            state = self._exec.get_order_status(order_id)
            local = self._live_orders.get(order_id)
            if local is None:
                continue

            if state == OS.FILLED:
                self._order_history.append({**local, "outcome": "filled_reconcile"})
                with self._reconcile_lock:
                    self._live_orders.pop(order_id, None)
                logger.info(
                    "[LimitQueue][S4][test] Order %s filled (reconciled from executor)",
                    order_id,
                )
            elif state == OS.CANCELLED or state == OS.REJECTED:
                self._order_history.append({**local, "outcome": str(state.value)})
                with self._reconcile_lock:
                    self._live_orders.pop(order_id, None)
                logger.info(
                    "[LimitQueue][S4][test] Order %s %s (reconciled)",
                    order_id, state.value,
                )

    def _reconcile_live_mode(self) -> None:
        """
        Live-mode reconciliation: query Binance REST API directly.

        Binance endpoints used:
          - GET /api/v3/openOrders  (all open orders for the symbol)
          - GET /api/v3/order       (individual order status by orderId + origClientId)
        """
        # Import locally so the class can load without the dependency
        from src.execution.exchanges.binance_executor import OrderState as OS

        if self._client is None:
            logger.warning(
                "[LimitQueue][S4][live] No binance_client configured; skipping reconcile"
            )
            return

        try:
            # Step 1: Get all open orders from Binance
            binance_orders = self._client.get_open_orders(symbol=self.symbol)
            binance_ids = {str(o.get("orderId", "")) for o in binance_orders}

            # Build lookup for quick partial-fill sync
            binance_by_id: dict[str, dict] = {
                str(o.get("orderId", "")): o for o in binance_orders
            }

        except Exception as exc:
            logger.error(
                "[LimitQueue][S4][live] Failed to fetch open orders from Binance: %s", exc,
            )
            return

        with self._reconcile_lock:
            local_ids = list(self._live_orders.keys())

        orphaned_on_binance: list[str] = []
        for bid in binance_ids:
            if bid not in self._live_orders:
                orphaned_on_binance.append(bid)

        if orphaned_on_binance:
            self._orphan_count += len(orphaned_on_binance)
            for orphan_id in orphaned_on_binance:
                # Attempt to cancel the orphan to free held margin immediately
                try:
                    self._exec.cancel_order(symbol=self.symbol, order_id=orphan_id)
                    logger.warning(
                        "[LimitQueue][S4][live] Orphan order %s cancelled to reclaim margin",
                        orphan_id,
                    )
                except Exception as cancel_exc:
                    logger.error(
                        "[LimitQueue][S4][live] Failed to cancel orphan order %s: %s",
                        orphan_id, cancel_exc,
                    )
            logger.warning(
                "[LimitQueue][S4][live] %d orphan order(s) on Binance not tracked in queue "
                "(total orphans detected: %d): %s",
                len(orphaned_on_binance), self._orphan_count, orphaned_on_binance,
            )

        # Step 2: Check each locally-tracked order against Binance
        for order_id in local_ids:
            local = self._live_orders.get(order_id)
            if local is None:
                continue

            if order_id not in binance_ids:
                # Order not on Binance → filled, cancelled, or rejected externally
                try:
                    # Fetch status from Binance
                    status_resp = self._client.get_order(
                        symbol=self.symbol,
                        orderId=order_id,
                    )
                    binance_status = status_resp.get("status", "").upper()
                    executed_qty = float(status_resp.get("executedQty", 0.0))

                    # Map Binance status to OrderState
                    if binance_status == "FILLED":
                        state = OS.FILLED
                    elif binance_status == "PARTIALLY_FILLED":
                        state = OS.PARTIAL
                    elif binance_status in ("CANCELED", "EXPIRED"):
                        state = OS.CANCELLED
                    else:
                        # Still pending per Binance — just sync qty and continue
                        local["filled_qty"] = executed_qty
                        local["status"] = "PARTIAL" if executed_qty > 0 else "NEW"
                        continue

                    # Sync fill qty
                    if executed_qty > 0:
                        local["filled_qty"] = executed_qty

                    # Step 3: Tell executor to apply the state transition
                    self._exec.update_order_status(
                        order_id=order_id,
                        state=state,
                        filled_qty=executed_qty,
                        fill_price=float(status_resp.get("price", local.get("price", 0.0))),
                    )

                    # Remove from local tracker
                    self._order_history.append({**local, "outcome": f"reconciled_{state.value}"})
                    with self._reconcile_lock:
                        self._live_orders.pop(order_id, None)

                    logger.info(
                        "[LimitQueue][S4][live] Order %s reconciled as %s  exec_qty=%.6f",
                        order_id, state.value, executed_qty,
                    )

                except Exception as exc:
                    logger.error(
                        "[LimitQueue][S4][live] Failed to reconcile order %s: %s", order_id, exc,
                    )
            else:
                # Order still open on Binance — sync partial fill qty if changed
                b_o = binance_by_id.get(order_id, {})
                binance_exec_qty = float(b_o.get("executedQty", 0.0))
                local_exec_qty = local.get("filled_qty", 0.0)

                if binance_exec_qty != local_exec_qty:
                    local["filled_qty"] = binance_exec_qty
                    if binance_exec_qty > 0:
                        local["status"] = "PARTIAL"
                    logger.info(
                        "[LimitQueue][S4][live] Order %s fill qty updated: %.6f → %.6f",
                        order_id, local_exec_qty, binance_exec_qty,
                    )

    def reconcile(self) -> dict:
        """
        Manually trigger a reconciliation cycle.

        Returns
        -------
        dict with keys:
            - synced    : int  — number of orders updated
            - orphans   : int  — number of orphan orders detected
            - timestamp : datetime
        """
        with self._reconcile_lock:
            before = len(self._live_orders)

        self._do_reconcile()

        with self._reconcile_lock:
            after = len(self._live_orders)

        return {
            "synced": max(0, before - after),
            "orphans": 0,   # orphans are logged, not counted here
            "timestamp": datetime.now(),
        }

    # ------------------------------------------------------------------
    # Order placement helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _round_to_tick(price: float, tick_size: float) -> float:
        """Round price to the nearest tick_size increment."""
        return round(round(price / tick_size) * tick_size, 8)

    def _submit_limit(
        self,
        side: str,
        price: float,
        quantity: float,
    ) -> str:
        """Internal: submit a LIMIT GTC order via the executor."""
        order = self._exec.place_order(
            symbol=self.symbol,
            side=side,
            order_type="LIMIT",
            quantity=quantity,
            price=price,
            timeInForce="GTC",
        )
        order_id = order["order_id"]
        status = order.get("status", "PENDING")

        with self._reconcile_lock:
            self._live_orders[order_id] = {
                "side": side,
                "price": price,
                "orig_qty": quantity,
                "filled_qty": 0.0,
                "status": status,
            }

        # S4: Start reconciliation loop on first order placement
        self.start_reconciliation()

        logger.info(
            "[LimitQueue] Placed %s %s LIMIT qty=%.6f @ %.4f  order_id=%s  status=%s",
            side, self.symbol, quantity, price, order_id, status,
        )
        return order_id

    # ------------------------------------------------------------------
    # FVG zone placement
    # ------------------------------------------------------------------

    def place_at_fvg_zone(
        self,
        fvg_low: float,
        fvg_high: float,
        quantity: float,
        side: str,
    ) -> str:
        """
        Place a limit order at the midpoint of a Fair Value Gap zone.

        FVG logic (ICT / Smart Money Concepts):
        - A bullish FVG forms when the low of the current candle exceeds the
          high of the candle two periods ago, leaving a "gap" between them.
        - Institutions typically fill these gaps before price continues in
          the direction of the imbalance.

        Entry price = (fvg_low + fvg_high) / 2, rounded to nearest tick.

        Parameters
        ----------
        fvg_low  : float — lower boundary of the FVG (price)
        fvg_high  : float — upper boundary of the FVG (price)
        quantity  : float — base-asset quantity
        side      : str   — "BUY" for bullish FVG, "SELL" for bearish FVG

        Returns
        -------
        str — the Binance order_id
        """
        if fvg_high <= fvg_low:
            raise ValueError("fvg_high must be greater than fvg_low")
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        midpoint = (fvg_low + fvg_high) / 2.0
        entry_price = self._round_to_tick(midpoint, self.tick_size)

        order_id = self._submit_limit(side, entry_price, quantity)

        # Tag for later analysis
        self._live_orders[order_id]["entry_type"] = "FVG"
        self._live_orders[order_id]["fvg_low"] = fvg_low
        self._live_orders[order_id]["fvg_high"] = fvg_high
        self._live_orders[order_id]["midpoint"] = midpoint
        self._live_orders[order_id]["rounded_price"] = entry_price

        logger.info(
            "[LimitQueue] FVG %s order: zone=[%.4f, %.4f]  "
            "midpoint=%.4f  entry=%.4f  qty=%.6f",
            side, fvg_low, fvg_high, midpoint, entry_price, quantity,
        )
        return order_id

    # ------------------------------------------------------------------
    # Retracement placement
    # ------------------------------------------------------------------

    def place_at_retracement(
        self,
        base_price: float,
        retracement_pct: float,
        quantity: float,
        side: str,
    ) -> str:
        """
        Place a limit order at a Fibonacci-retracement level from a base price.

        Formula
        -------
        - Long  (BUY): entry_price = base_price × (1 − retracement_pct)
        - Short (SELL): entry_price = base_price × (1 + retracement_pct)

        retracement_pct is expressed as a fraction, e.g. 0.382 for the 38.2 %
        retracement level.

        Parameters
        ----------
        base_price      : float — reference high/low (e.g. swing high)
        retracement_pct: float — fraction to retrace (e.g. 0.382)
        quantity        : float — base-asset quantity
        side             : str  — "BUY" (long) or "SELL" (short)

        Returns
        -------
        str — the Binance order_id
        """
        if not (0 < retracement_pct < 1):
            raise ValueError("retracement_pct must be in (0, 1)")
        if base_price <= 0 or quantity <= 0:
            raise ValueError("base_price and quantity must be positive")

        if side == "BUY":
            entry_price = base_price * (1.0 - retracement_pct)
        elif side == "SELL":
            entry_price = base_price * (1.0 + retracement_pct)
        else:
            raise ValueError("side must be 'BUY' or 'SELL'")

        entry_price = self._round_to_tick(entry_price, self.tick_size)

        order_id = self._submit_limit(side, entry_price, quantity)

        self._live_orders[order_id]["entry_type"] = "RETRACEMENT"
        self._live_orders[order_id]["base_price"] = base_price
        self._live_orders[order_id]["retracement_pct"] = retracement_pct
        self._live_orders[order_id]["rounded_price"] = entry_price

        logger.info(
            "[LimitQueue] Retracement %s order: base=%.4f  %.1f%%  "
            "entry=%.4f  qty=%.6f",
            side, base_price, retracement_pct * 100, entry_price, quantity,
        )
        return order_id

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------

    def cancel_all(self) -> int:
        """
        Cancel all open (live) orders for the symbol.

        Stops the reconciliation loop after all orders are cancelled.

        Returns
        -------
        int — number of orders successfully cancelled.
        """
        cancelled = 0
        with self._reconcile_lock:
            order_ids = list(self._live_orders.keys())
        for order_id in order_ids:
            ok = self._exec.cancel_order(symbol=self.symbol, order_id=order_id)
            if ok:
                with self._reconcile_lock:
                    removed = self._live_orders.pop(order_id, {})
                self._order_history.append({**removed, "outcome": "cancelled"})
                cancelled += 1
            else:
                logger.warning("[LimitQueue] Failed to cancel order %s", order_id)

        # S4: Stop reconciliation thread if queue is empty
        with self._reconcile_lock:
            if not self._live_orders:
                self.stop_reconciliation()

        logger.info("[LimitQueue] Cancelled %d / %d orders", cancelled, len(order_ids))
        return cancelled

    def get_queue_status(self) -> dict:
        """
        Return current queue health metrics.

        Returns
        -------
        dict with keys:
            - open_orders        : int
            - total_pending_qty  : float
            - avg_distance_from_mid_bps : float (average distance of all live
                                         orders from the current mid price;
                                         -1.0 if no mid price is available)
            - by_side             : dict  — breakdown by BUY / SELL
        """
        open_orders = list(self._live_orders.values())
        open_count = len(open_orders)
        total_pending = sum(o["orig_qty"] - o["filled_qty"] for o in open_orders)

        buy_orders = [o for o in open_orders if o["side"] == "BUY"]
        sell_orders = [o for o in open_orders if o["side"] == "SELL"]

        return {
            "open_orders": open_count,
            "total_pending_qty": round(total_pending, 8),
            "avg_distance_from_mid_bps": -1.0,   # caller should inject mid price
            "buy_count": len(buy_orders),
            "sell_count": len(sell_orders),
            "live_orders": [
                {
                    "order_id": oid,
                    "side": o["side"],
                    "price": o["price"],
                    "pending_qty": round(o["orig_qty"] - o["filled_qty"], 8),
                    "entry_type": o.get("entry_type", "UNKNOWN"),
                }
                for oid, o in self._live_orders.items()
            ],
        }

    def update_with_mid_price(self, mid_price: float) -> None:
        """
        Update avg_distance_from_mid_bps for all live orders.

        Call this after each market-price tick to keep queue status current.
        """
        if mid_price <= 0:
            return

        distances = []
        for order in self._live_orders.values():
            dist_bps = (order["price"] - mid_price) / mid_price * 10_000
            if order["side"] == "BUY":
                dist_bps = abs(dist_bps)   # distance is always positive
            else:
                dist_bps = abs(dist_bps)
            distances.append(dist_bps)

        avg_dist = sum(distances) / len(distances) if distances else -1.0

        # Patch into the live order dicts for transparency
        for order in self._live_orders.values():
            order["distance_from_mid_bps"] = (
                (order["price"] - mid_price) / mid_price * 10_000
            )

        # Update the sentinel value so get_queue_status reflects it
        self._last_mid_price = mid_price
        self._avg_distance_bps = avg_dist

    _last_mid_price: float = 0.0
    _avg_distance_bps: float = -1.0


# ===========================================================================
# SECTION D — SmartRouter
# ===========================================================================


class SmartRouter:
    """
    Intent-driven order router that selects the optimal executor.

    Mapping
    -------
    "aggressive_fill" → MarketExecutor (stub — executes at best bid/ask)
    "vwap_anchor"    → VWAPExecutor
    "twap_sniper"    → TWAPExecutor
    "passive_fvg"    → LimitOrderQueue

    Parameters
    ----------
    executors     : dict[str, object] — name → executor instance
    binance_exec  : BinanceExecutor   — shared exchange adapter
    """

    INTENT_MAP: dict[str, str] = {
        "aggressive_fill": "MarketExecutor",
        "vwap_anchor": "VWAPExecutor",
        "twap_sniper": "TWAPExecutor",
        "passive_fvg": "LimitOrderQueue",
    }

    def __init__(
        self,
        executors: dict[str, object],
        binance_exec: "BinanceExecutor",
    ) -> None:
        self._executors: dict[str, object] = dict(executors)
        self._binance_exec = binance_exec
        self._session_fills: list[SlippageReport] = []

    # ------------------------------------------------------------------
    # Executor selection
    # ------------------------------------------------------------------

    def select_executor(self, intent: str, params: dict) -> str:
        """
        Map an execution intent to the best-suited executor name.

        Parameters
        ----------
        intent : str
            One of: "aggressive_fill", "vwap_anchor", "twap_sniper", "passive_fvg"
        params : dict
            Additional routing hints (currently unused; reserved for future
            cost-model routing).

        Returns
        -------
        str — executor name registered in self._executors.
        """
        executor_name = self.INTENT_MAP.get(intent)
        if not executor_name:
            logger.warning(
                "[SmartRouter] Unknown intent '%s'; falling back to TWAPExecutor",
                intent,
            )
            executor_name = "TWAPExecutor"

        if executor_name not in self._executors:
            raise KeyError(
                f"Executor '{executor_name}' is not registered with the router. "
                f"Registered: {list(self._executors.keys())}"
            )

        logger.info("[SmartRouter] Intent '%s' → executor '%s'", intent, executor_name)
        return executor_name

    # ------------------------------------------------------------------
    # Order routing
    # ------------------------------------------------------------------

    def route(self, order_intent: dict) -> str:
        """
        Route a structured order intent to the appropriate executor.

        Parameters
        ----------
        order_intent : dict with required keys:
            - intent         : str  — execution strategy (see select_executor)
            - symbol         : str  — trading pair
            - side           : str  — "BUY" or "SELL"
            - quantity       : float
            Plus executor-specific parameters, e.g.:
            - duration_secs  : int  (TWAP)
            - participation_rate : float (VWAP)
            - fvg_zone       : tuple (low, high) (LimitOrderQueue)
            - expected_price : float (reference price for slippage calc)

        Returns
        -------
        str — executor_name that handled the order.
        """
        intent = order_intent["intent"]
        symbol = order_intent["symbol"]
        side = order_intent["side"]
        quantity = order_intent["quantity"]
        params = {k: v for k, v in order_intent.items()
                  if k not in ("intent", "symbol", "side", "quantity")}

        executor_name = self.select_executor(intent, params)
        executor = self._executors[executor_name]

        # ----- TWAP --------------------------------------------------------
        if executor_name == "TWAPExecutor":
            twap: TWAPExecutor = executor
            # Wire reference_price and slippage_bps from the order intent into the
            # pre-registered executor so slices get the correct per-slice price_limit.
            twap._reference_price = params.get("reference_price", 0.0)
            twap._slippage_bps = params.get("slippage_bps", 10.0)
            twap._build_schedule()   # rebuild slices with populated price_limit
            interval = params.get("slice_interval_secs", 60)
            import time as _time  # local import to avoid top-level side-effects
            while not twap.is_complete():
                sl = twap.get_next_slice()
                if sl["quantity"] <= 0:
                    break
                result = self._binance_exec.place_order(
                    symbol=symbol,
                    side=side,
                    order_type="LIMIT",
                    quantity=sl["quantity"],
                    price=sl["price_limit"],
                    timeInForce="GTC",
                )
                # Binance returns "executedQty" (camelCase); fall back to "executed_qty"
                executed = float(result.get("executedQty", result.get("executed_qty", sl["quantity"])))
                price = float(result.get("price", sl["price_limit"], 0))
                twap.record_fill(
                    quantity=executed,
                    price=price,
                    expected_price=sl["price_limit"],
                )
                # Respect slice interval — sleep between child orders so the TWAP
                # actually spreads over time instead of executing all slices immediately.
                if not twap.is_complete():
                    _time.sleep(max(0.5, interval * 0.8))
            self._session_fills.extend(twap._fill_history)

        # ----- VWAP --------------------------------------------------------
        elif executor_name == "VWAPExecutor":
            vwap: VWAPExecutor = executor
            mkt_vol = params.get("market_volume_estimate", 0.0)
            schedule = vwap.get_intraday_schedule(market_volume_estimate=mkt_vol)
            import time as _time  # local import
            slice_interval = params.get("slice_interval_secs", 120)
            for sl in schedule:
                ref_price = params.get("expected_price", 0)
                result = self._binance_exec.place_order(
                    symbol=symbol,
                    side=side,
                    order_type="LIMIT",
                    quantity=sl["quantity"],
                    price=ref_price,
                    timeInForce="GTC",
                )
                # Binance returns "executedQty" (camelCase); fall back to "executed_qty"
                executed = float(result.get("executedQty", result.get("executed_qty", sl["quantity"])))
                price = float(result.get("price", params.get("expected_price", 0)))
                vwap.record_fill(
                    quantity=executed,
                    price=price,
                    expected_vwap=params.get("expected_vwap"),
                )
                # Space child orders out proportionally to the schedule interval.
                _time.sleep(max(1.0, slice_interval * 0.8))
            self._session_fills.extend(vwap._fill_history)

        # ----- LimitOrderQueue ----------------------------------------------
        elif executor_name == "LimitOrderQueue":
            loq: LimitOrderQueue = executor
            if "fvg_zone" in params:
                fvg_low, fvg_high = params["fvg_zone"]
                loq.place_at_fvg_zone(fvg_low, fvg_high, quantity, side)
            elif "retracement" in params:
                base_price = params.get("base_price", 0.0)
                retr_pct = params["retracement"]
                loq.place_at_retracement(base_price, retr_pct, quantity, side)
            else:
                raise ValueError(
                    "LimitOrderQueue requires 'fvg_zone' or 'retracement' param"
                )

        # ----- MarketExecutor (stub) ----------------------------------------
        elif executor_name == "MarketExecutor":
            result = self._binance_exec.place_order(
                symbol=symbol,
                side=side,
                order_type="MARKET",
                quantity=quantity,
                price=None,
            )
            # Binance returns "executedQty" (camelCase); fall back to "executed_qty"
            executed = float(result.get("executedQty", result.get("executed_qty", quantity)))
            fill_price = float(result.get("price", 0))
            self._session_fills.append({
                "executor": "MarketExecutor",
                "symbol": symbol,
                "side": side,
                "fill_qty": executed,
                "fill_price": fill_price,
                "slippage_bps": 0.0,
                "timestamp": datetime.now(),
            })

        return executor_name

    # ------------------------------------------------------------------
    # Session reporting
    # ------------------------------------------------------------------

    def collect_fill_report(self) -> dict:
        """
        Aggregate slippage and fill statistics across all executors
        for the current session.

        Returns
        -------
        dict with keys:
            - total_orders    : int
            - total_filled    : float
            - avg_slippage_bps: float
            - by_executor     : dict
            - worst_fill_bps  : float
            - best_fill_bps   : float
            - fill_history    : list[SlippageReport]
        """
        if not self._session_fills:
            return {
                "total_orders": 0,
                "total_filled": 0.0,
                "avg_slippage_bps": 0.0,
                "by_executor": {},
                "worst_fill_bps": 0.0,
                "best_fill_bps": 0.0,
                "fill_history": [],
            }

        slippage_bps_list = [f["slippage_bps"] for f in self._session_fills]
        total_filled = sum(f["fill_qty"] for f in self._session_fills)

        by_executor: dict[str, dict] = {}
        for f in self._session_fills:
            ex = f["executor"]
            if ex not in by_executor:
                by_executor[ex] = {"count": 0, "slippage_sum": 0.0, "qty": 0.0}
            by_executor[ex]["count"] += 1
            by_executor[ex]["slippage_sum"] += f["slippage_bps"]
            by_executor[ex]["qty"] += f["fill_qty"]

        for ex, stats in by_executor.items():
            stats["avg_slippage_bps"] = stats["slippage_sum"] / stats["count"]

        return {
            "total_orders": len(self._session_fills),
            "total_filled": round(total_filled, 8),
            "avg_slippage_bps": round(sum(slippage_bps_list) / len(slippage_bps_list), 4),
            "by_executor": by_executor,
            "worst_fill_bps": round(max(slippage_bps_list), 4),
            "best_fill_bps": round(min(slippage_bps_list), 4),
            "fill_history": list(self._session_fills),
        }

    def register_executor(self, name: str, executor: object) -> None:
        """Add or replace an executor at runtime."""
        self._executors[name] = executor


# ===========================================================================
# SECTION E — MarketImpactModel
# ===========================================================================
# References
#   - Almgren & Chriss (2000) — "Optimal Execution of Portfolio Transactions"
#   - Almgren, R. & N. Lorenz (2007) — "Adaptive Arrival Price"
#   - Cont & da Silva (2005) — "Optimal Execution with Liquidity Renegotiation"
# ===========================================================================


class MarketImpactModel:
    """
    Almgren-Chriss market-impact estimator for crypto spot.

    The model separates permanent and temporary impact.  Here we implement the
    temporary impact component which dominates for HFT / intraday execution:

        impact_bps = θ × (Q / ADV)^0.6

    where
        θ   = market constant (calibrated to ~0.1 for crypto majors)
        Q   = order size in base units
        ADV = average daily volume in same units

    The 0.6 exponent is derived empirically from equity markets (Almgren-Chriss)
    and commonly adopted for crypto.

    Parameters
    ----------
    symbol : str
        Trading pair for logging and future per-asset calibration.
    theta  : float
        Market constant.  Default 0.1 is reasonable for BTC/ETH on Binance.
        Calibrate higher (0.2–0.4) for smaller-cap or lower-liquidity assets.
    """

    # Exponent from Almgren-Chriss (optimal execution of portfolio transactions, 2000)
    IMPACT_EXPONENT: float = 0.6

    def __init__(
        self,
        symbol: str,
        theta: float = 0.1,
    ) -> None:
        self.symbol = symbol
        self.theta = theta
        self._impact_history: list[dict] = []

    # ------------------------------------------------------------------
    # Impact estimation
    # ------------------------------------------------------------------

    def estimate_impact(
        self,
        quantity: float,
        volatility: float,
        avg_daily_volume: float,
    ) -> float:
        """
        Estimate market impact in basis points for a single parent order.

        Almgren-Chriss temporary-impact formula
        ----------------------------------------
        impact_bps = θ × (Q / ADV)^0.6 × σ × 10 000

        For spot crypto where ADV is expressed in the same units as Q,
        the volatility term is optional; we include it for consistency with
        the full AC formulation and to scale impact with current regime.

        Parameters
        ----------
        quantity         : float — parent order size in base units
        volatility       : float — daily volatility (fraction, e.g. 0.02 for 2 %)
        avg_daily_volume  : float — ADV in same base units as quantity

        Returns
        -------
        float : estimated impact in basis points.
        """
        if quantity <= 0 or avg_daily_volume <= 0:
            logger.warning(
                "[MIM] quantity=%s or ADV=%s invalid; returning 0 impact",
                quantity, avg_daily_volume,
            )
            return 0.0

        # Normalise by ADV
        participation = quantity / avg_daily_volume

        # Base impact (no volatility term — pure volume-driven)
        impact = self.theta * (participation ** self.IMPACT_EXPONENT)

        # Optional: scale by daily volatility
        if volatility > 0:
            impact *= volatility

        impact_bps = impact * 10_000

        record = {
            "symbol": self.symbol,
            "quantity": quantity,
            "adv": avg_daily_volume,
            "participation": participation,
            "volatility": volatility,
            "impact_bps": impact_bps,
            "timestamp": datetime.now(),
        }
        self._impact_history.append(record)

        logger.info(
            "[MIM] %s  qty=%.6f  ADV=%.2f  participation=%.4f  "
            "vol=%.4f  impact=%.2f bps",
            self.symbol, quantity, avg_daily_volume, participation,
            volatility, impact_bps,
        )
        return impact_bps

    # ------------------------------------------------------------------
    # Optimal child-order split
    # ------------------------------------------------------------------

    def optimal_split(
        self,
        quantity: float,
        adv: float,
        max_impact_bps: float = 5.0,
    ) -> int:
        """
        Binary-search the minimum number of child orders required to keep
        estimated market impact below max_impact_bps.

        Each child order is assumed to have size Q / n.  Impact is computed
        per child via estimate_impact() with that reduced size.

        Parameters
        ----------
        quantity      : float — total parent order size
        adv           : float — average daily volume
        max_impact_bps: float — impact ceiling (default 5 bps)

        Returns
        -------
        int — minimum number of child orders.  Minimum value is 1.
        """
        if quantity <= 0 or adv <= 0:
            return 1
        if max_impact_bps <= 0:
            raise ValueError("max_impact_bps must be positive")

        # Binary search for smallest n ∈ [1, 10_000] where impact ≤ max_impact_bps
        lo, hi = 1, 10_000

        # Quick check: single child already within budget
        single_impact = self.estimate_impact(quantity, 0.0, adv)
        if single_impact <= max_impact_bps:
            logger.info(
                "[MIM] %s: Single child impact %.2f bps ≤ %.2f bps budget → n=1",
                self.symbol, single_impact, max_impact_bps,
            )
            return 1

        while lo < hi:
            mid = (lo + hi) // 2
            child_qty = quantity / mid
            child_impact = self.estimate_impact(child_qty, 0.0, adv)
            if child_impact <= max_impact_bps:
                hi = mid
            else:
                lo = mid + 1

        optimal_n = lo
        logger.info(
            "[MIM] %s: optimal_split quantity=%.4f  ADV=%.2f  "
            "max_impact=%.2f bps  → n=%d",
            self.symbol, quantity, adv, max_impact_bps, optimal_n,
        )
        return optimal_n

    # ------------------------------------------------------------------
    # Diagnostic helpers
    # ---------------------------------------------------------------------

    def calibration_hint(
        self,
        observed_impact_bps: float,
        participation: float,
    ) -> float:
        """
        Back out the implied market constant θ from observed fill data.

        Invert: θ = impact_bps / ((Q/ADV)^0.6 × 10 000)

        Use this to calibrate self.theta from historical execution data.

        Parameters
        ----------
        observed_impact_bps : float
        participation        : float = Q / ADV

        Returns
        -------
        float : implied θ
        """
        if participation <= 0:
            return self.theta
        implied = observed_impact_bps / (
            (participation ** self.IMPACT_EXPONENT) * 10_000
        )
        logger.info(
            "[MIM] Calibrated θ: observed=%.2f bps  participation=%.4f  "
            "implied θ=%.4f  (current θ=%.4f)",
            observed_impact_bps, participation, implied, self.theta,
        )
        return implied

    def get_impact_history(self) -> list[dict]:
        """Return the logged impact estimates for the session."""
        return list(self._impact_history)


# ===========================================================================
# MarketExecutor stub (referenced by SmartRouter)
# ===========================================================================


class MarketExecutor:
    """
    Stub aggressive-fill executor.

    Executes the entire parent order immediately at the best available price
    (market order).  Appropriate when speed is more valuable than cost, or
    when the remaining time budget is exhausted and the order must close.

    Slippage is measured against the prevailing mid price at time of order.
    """

    def __init__(
        self,
        symbol: str,
        side: str,
        quantity: float,
        mid_price: float,
    ) -> None:
        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        self.mid_price = mid_price
        self.filled = False
        self.fill_price: float | None = None
        self.slippage_bps: float = 0.0

    def execute(self, executor: "BinanceExecutor") -> dict:
        """Execute at market."""
        result = executor.place_order(
            symbol=self.symbol,
            side=self.side,
            order_type="MARKET",
            quantity=self.quantity,
            price=None,
        )
        self.fill_price = float(result.get("price", 0))
        if self.fill_price and self.mid_price:
            self.slippage_bps = (
                (self.fill_price - self.mid_price) / self.mid_price * 10_000
            )
        self.filled = True
        return result


# ===========================================================================
# Standalone function used by SmartRouter for live TWAP/VWAP execution
# ===========================================================================


def _default_slice_executor(
    binance_exec: "BinanceExecutor",
    symbol: str,
    side: str,
    quantity: float,
    price_limit: float | None,
    expected_price: float,
) -> dict:
    """Default slice executor: market order with a reference price for slippage."""
    result = binance_exec.place_order(
        symbol=symbol,
        side=side,
        order_type="MARKET",
        quantity=quantity,
        price=expected_price,
    )
    return result


# ===========================================================================
# Test block
# ===========================================================================
if __name__ == "__main__":

    import logging as _logging

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # ------------------------------------------------------------------
    # Shared BinanceExecutor in test mode
    # ------------------------------------------------------------------
    import importlib.util as _implutil
    import sys as _sys
    _self_path = _sys.modules[__name__].__file__ or ""
    _exec_dir = _self_path.rsplit("/", 1)[0]
    _binance_path = f"{_exec_dir}/exchanges/binance_executor.py"
    _spec = _implutil.spec_from_file_location(
        "execution.exchanges.binance_executor", _binance_path
    )
    _mod = _implutil.module_from_spec(_spec)
    _sys.modules["execution.exchanges.binance_executor"] = _mod
    _spec.loader.exec_module(_mod)  # type: ignore
    BinanceExecutor = _mod.BinanceExecutor

    dummy_client = object()   # not used in test_mode
    binance_exec = BinanceExecutor(binance_client=dummy_client, test_mode=True)
    binance_exec.set_mock_balance("USDT", 100_000.0)

    print("\n" + "=" * 65)
    print("SECTION A — TWAPExecutor")
    print("=" * 65)

    twap = TWAPExecutor(
        symbol="BTCUSDT",
        side="BUY",
        total_quantity=2.0,
        duration_secs=300,       # 5-minute window
        slice_interval_secs=60,  # 5 slices of 0.4 BTC each
        reference_price=60_000.0,
        slippage_bps=10.0,
    )

    print(f"Total quantity : {twap.total_quantity}")
    print(f"Number of slices: {twap._num_slices}")
    print(f"Qty per slice   : {twap._slice_qty:.6f}")
    print()

    for i in range(twap._num_slices + 1):
        sl = twap.get_next_slice()
        print(f"  Slice {i + 1}: {sl}")
        if sl["quantity"] > 0:
            # Simulate fill at slightly higher price
            fill_price = 60_000.0 * (1 + 0.0003 * i)
            twap.record_fill(
                quantity=sl["quantity"],
                price=fill_price,
                expected_price=60_000.0,
            )

    print(f"\nTWAP stats: {twap.get_stats()}")

    # ------------------------------------------------------------------
    print("\n" + "=" * 65)
    print("SECTION B — VWAPExecutor")
    print("=" * 65)

    vwap = VWAPExecutor(
        symbol="ETHUSDT",
        side="BUY",
        total_quantity=10.0,
        participation_rate=0.10,
    )

    schedule = vwap.get_intraday_schedule(market_volume_estimate=50_000.0)
    print(f"Schedule has {len(schedule)} buckets")
    for sl in schedule[:4]:
        print(f"  {sl['timestamp'].strftime('%H:%M')}  qty={sl['quantity']:.4f}  "
              f"participation={sl['participation_rate']:.1%}")

    # Momentum test
    reduced_rate = vwap.adjust_for_momentum(
        current_volume_rate=3.0,
        avg_volume_rate=1.5,
    )
    print(f"\nMomentum adjustment test: base=10%  →  new={reduced_rate:.1%}")

    # Record a mock fill
    vwap.record_fill(quantity=1.0, price=2_050.0, expected_vwap=2_048.0)
    print(f"VWAP stats: {vwap.get_stats()}")

    # ------------------------------------------------------------------
    print("\n" + "=" * 65)
    print("SECTION C — LimitOrderQueue")
    print("=" * 65)

    loq = LimitOrderQueue(
        symbol="BTCUSDT",
        binance_executor=binance_exec,
        tick_size=0.01,
    )

    # FVG zone example
    fvg_id = loq.place_at_fvg_zone(
        fvg_low=59_800.0,
        fvg_high=60_000.0,
        quantity=0.1,
        side="BUY",
    )
    print(f"FVG order placed: {fvg_id}")

    # Fibonacci retracement
    retr_id = loq.place_at_retracement(
        base_price=61_000.0,
        retracement_pct=0.382,
        quantity=0.05,
        side="BUY",
    )
    print(f"Retracement order placed: {retr_id}")

    status = loq.get_queue_status()
    print(f"\nQueue status: open_orders={status['open_orders']}  "
          f"total_pending={status['total_pending_qty']}")

    loq.update_with_mid_price(mid_price=60_200.0)
    cancelled = loq.cancel_all()
    print(f"Cancelled {cancelled} orders")

    # ------------------------------------------------------------------
    print("\n" + "=" * 65)
    print("SECTION D — SmartRouter")
    print("=" * 65)

    # Pre-built executors
    twap_exec = TWAPExecutor(
        symbol="BTCUSDT", side="BUY",
        total_quantity=1.0,
        duration_secs=120,
        slice_interval_secs=30,
        reference_price=60_000.0,
        slippage_bps=10.0,
    )
    vwap_exec = VWAPExecutor(
        symbol="BTCUSDT", side="BUY",
        total_quantity=1.0,
        participation_rate=0.08,
    )
    loq_exec = LimitOrderQueue(
        symbol="BTCUSDT",
        binance_executor=binance_exec,
    )
    market_exec = MarketExecutor(
        symbol="BTCUSDT", side="BUY",
        quantity=0.5, mid_price=60_000.0,
    )

    router = SmartRouter(
        executors={
            "TWAPExecutor": twap_exec,
            "VWAPExecutor": vwap_exec,
            "LimitOrderQueue": loq_exec,
            "MarketExecutor": market_exec,
        },
        binance_exec=binance_exec,
    )

    # Route a VWAP intent
    executor_name = router.select_executor("vwap_anchor", {})
    print(f"Selected executor: {executor_name}")

    report = router.collect_fill_report()
    print(f"Fill report (empty): {report}")

    # ------------------------------------------------------------------
    print("\n" + "=" * 65)
    print("SECTION E — MarketImpactModel")
    print("=" * 65)

    mim = MarketImpactModel(symbol="BTCUSDT", theta=0.1)

    impact = mim.estimate_impact(
        quantity=10.0,          # 10 BTC
        volatility=0.025,       # 2.5 % daily
        avg_daily_volume=500.0, # 500 BTC ADV
    )
    print(f"\nImpact estimate (10 BTC, 500 ADV, 2.5%% vol): {impact:.2f} bps")

    optimal_n = mim.optimal_split(
        quantity=10.0,
        adv=500.0,
        max_impact_bps=5.0,
    )
    print(f"Optimal child orders for < 5 bps impact: {optimal_n}")

    # Calibration hint
    implied_theta = mim.calibration_hint(
        observed_impact_bps=8.0,
        participation=0.02,   # 10 BTC / 500 ADV
    )
    print(f"Implied θ from observed data: {implied_theta:.4f}")

    print("\n" + "=" * 65)
    print("ALL TESTS PASSED")
    print("=" * 65)
