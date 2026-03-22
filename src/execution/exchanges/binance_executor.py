# src/execution/exchanges/binance_executor.py
"""
BinanceExecutor — thin adapter that routes orders to Binance Spot API.

In test_mode (default) all methods return deterministic mock fills so that
backtesting and unit tests run without network access.  When test_mode=False
the real Binance API is called via the configured binance_client injected at
construction time.

State Machine (S1 — Order State Machine)
------------------------------------------
Every order transitions through a defined lifecycle:

    PENDING → PARTIAL → FILLED
    PENDING → FILLED          (market orders / instant fill)
    PENDING → CANCELLED
    PENDING → REJECTED

Live orders (LIMIT GTC) are tracked in `_live_orders` and updated via
`update_order_status()` (webhook or polling).  MARKET orders fill immediately
and are not tracked in `_live_orders`.

Position Authority (S2 — Unified Position State)
------------------------------------------------
RiskGuard is the authoritative source for position state.  After every fill
BinanceExecutor.record_fill() calls the registered risk_guard callback so both
systems stay in sync.  The executor no longer maintains independent position
tracking (`_mock_positions` has been removed).
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

# ── WriteRateLimiter imported lazily to avoid circular imports ─────────────────
# WriteRateLimiter lives in src.data.binance_client and is a pure-Python class
# with no executor dependencies, so lazy import here is safe.

logger = logging.getLogger(__name__)


# ─── Order State Machine (S1) ─────────────────────────────────────────────────

class OrderState(Enum):
    """
    Canonical order lifecycle states.

    Transitions:
        PENDING  → PARTIAL  (limit order partially filled)
        PENDING  → FILLED  (market order / instant full fill)
        PENDING  → CANCELLED
        PENDING  → REJECTED
        PARTIAL → FILLED   (remaining quantity filled)
        PARTIAL → CANCELLED
    """
    PENDING    = "PENDING"
    PARTIAL   = "PARTIAL"
    FILLED    = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED  = "REJECTED"


@dataclass
class TrackedOrder:
    """
    In-flight order tracked by the executor from placement through final state.
    Used by the order state machine (S1) to drive PENDING → PARTIAL → FILLED
    transitions and by LimitOrderQueue.reconcile() (S4) to sync with Binance.
    """
    order_id: str
    symbol: str
    side: str
    order_type: str          # "LIMIT", "MARKET", etc.
    price: float             # limit price; 0 for MARKET
    orig_qty: float
    filled_qty: float = 0.0
    state: OrderState = OrderState.PENDING
    created_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    last_update_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    @property
    def remaining_qty(self) -> float:
        return max(0.0, self.orig_qty - self.filled_qty)

    @property
    def is_terminal(self) -> bool:
        return self.state in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED)


@dataclass
class MockFill:
    """Simulated fill used in test_mode."""
    order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: float
    price: float
    status: str = "FILLED"
    commission: float = 0.0
    transact_time: int = field(default_factory=lambda: int(time.time() * 1000))


@dataclass
class MockOrder:
    """Simulated open order used in test_mode."""
    order_id: str
    symbol: str
    side: str
    order_type: str
    price: float
    orig_qty: float
    status: str = "NEW"


# ─── Risk Guard Callback Type ─────────────────────────────────────────────────

PositionUpdate = Callable[[str, str, float, float], None]
"""
Callback signature for position updates pushed to RiskGuard.

Args:
    symbol     — trading pair, e.g. "BTCUSDT"
    side       — "BUY" or "SELL"
    qty        — base-asset quantity filled (positive for buys, positive for sells)
    price      — fill price
"""


class BinanceExecutor:
    """
    Unified executor for Binance Spot orders.

    Parameters
    ----------
    binance_client : BinanceClient
        Authenticated Binance client (from src.data.binance_client).
    test_mode : bool
        If True (default) all methods return mock data without calling the API.
    risk_guard : RiskGuard, optional
        If provided, the executor calls risk_guard.update_position() after every
        fill so RiskGuard remains the authoritative position source (S2).
    """

    def __init__(
        self,
        binance_client: Any,
        test_mode: bool = True,
        risk_guard: Any = None,
        **kwargs: Any,
    ) -> None:
        self._client = binance_client
        self.test_mode = test_mode
        self._risk_guard = risk_guard  # S2: RiskGuard is authoritative

        # S1: In-flight orders tracked through the state machine
        self._live_orders: dict[str, TrackedOrder] = {}

        self._mock_fills: list[MockFill] = []
        self._mock_orders: dict[str, MockOrder] = {}
        self._mock_balances: dict[str, float] = {}    # asset -> free balance

        # Background thread for mock LIMIT order fill simulation (S1)
        self._mock_fill_thread_running = False
        self._mock_fill_thread: threading.Thread | None = None

        # Volatility-adjusted slippage model for mock MARKET fills
        # Recent prices are used to compute normalised ATR for slippage scaling
        self._recent_prices: deque[float] = deque(maxlen=100)
        self._base_slippage_bps: float = kwargs.get("base_slippage_bps", 2.0)
        self._max_slippage_bps: float = kwargs.get("max_slippage_bps", 15.0)
        self._slippage_vol_multiplier: float = kwargs.get("slippage_vol_multiplier", 10.0)

    # -------------------------------------------------------------------------
    # Slippage helpers
    # -------------------------------------------------------------------------

    def record_price_for_slippage(self, price: float) -> None:
        """
        Record a fill price for use in the next volatility-adjusted slippage
        calculation.  Call this after every real or mock fill.
        """
        if price > 0:
            self._recent_prices.append(price)

    def _effective_slippage_bps(self, current_price: float) -> float:
        """
        Compute volatility-adjusted slippage in basis points.

        The model uses normalised ATR (20-bar lookback, scaled to a fraction of
        price) as a proxy for market volatility, then scales it by
        _slippage_vol_multiplier to get an additional bps component:

            extra_bps = normalised_20_bar_atr * slippage_vol_multiplier * 10_000

        The final slippage is clamped between _base_slippage_bps and
        _max_slippage_bps, and is expressed as a *multiplicative* factor:

            buy:  1 + slippage_bps / 10_000
            sell: 1 - slippage_bps / 10_000

        Parameters
        ----------
        current_price : float
            Current asset price (used as the normalisation base when
            insufficient price history is available).

        Returns
        -------
        float
            Slippage in basis points (e.g. 3.5 means 3.5 bps).
        """
        prices = list(self._recent_prices)
        if len(prices) < 5:
            # Not enough data — fall back to base slippage
            return self._base_slippage_bps

        # Compute 20-bar simple ATR (absolute single-bar changes)
        lookback = min(20, len(prices) - 1)
        recent = prices[-lookback - 1:]
        true_ranges = [abs(recent[i] - recent[i - 1]) for i in range(1, len(recent))]
        atr = sum(true_ranges) / len(true_ranges)

        # Normalise ATR as a fraction of price
        norm_atr = atr / current_price if current_price > 0 else 0.0
        extra_bps = norm_atr * self._slippage_vol_multiplier * 10_000
        estimated_bps = self._base_slippage_bps + extra_bps
        return max(self._base_slippage_bps, min(self._max_slippage_bps, estimated_bps))

    # -------------------------------------------------------------------------
    # Core order operations
    # -------------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: float | None,
        **kwargs: Any,
    ) -> dict:
        """
        Place an order (market or limit) on Binance.

        In live mode LIMIT orders enter PENDING state and are tracked until
        filled, cancelled, or rejected.  MARKET orders fill immediately
        (PENDING → FILLED in a single call).

        Parameters
        ----------
        symbol     : trading pair, e.g. "BTCUSDT"
        side       : "BUY" or "SELL"
        order_type : "LIMIT", "MARKET", "STOP_LOSS_LIMIT", etc.
        quantity   : base-asset quantity
        price      : limit price (None for market orders)
        **kwargs   : passed through to BinanceClient (e.g. timeInForce)

        Returns
        -------
        dict with keys: order_id, symbol, side, quantity, price, status,
                        filled_qty, avg_fill_price, commission, transact_time
        """
        if self.test_mode:
            return self._mock_place_order(symbol, side, order_type, quantity, price, **kwargs)

        return self._live_place_order(symbol, side, order_type, quantity, price, **kwargs)

    def _mock_place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: float | None,
        **kwargs: Any,
    ) -> dict:
        """
        S1 mock order placement.

        MARKET orders: PENDING → FILLED immediately (simulate instant fill).
        LIMIT orders:   PENDING, tracked in _live_orders, filled asynchronously
                        by _mock_fill_thread (PENDING → PARTIAL → FILLED).
        """
        order_id = f"TEST_{uuid.uuid4().hex[:12].upper()}"
        now_ms = int(time.time() * 1000)

        if order_type == "MARKET":
            # ── MARKET: immediate full fill ──────────────────────────────────────
            fill_price = price if price else 0.0
            if fill_price <= 0 and self._recent_prices:
                fill_price = self._recent_prices[-1]
            # Volatility-adjusted slippage (configurable via __init__ kwargs)
            slippage_bps = self._effective_slippage_bps(fill_price)
            slippage_factor = 1.0 + slippage_bps / 10_000 if side == "BUY" else 1.0 - slippage_bps / 10_000
            fill_price = round(fill_price * slippage_factor, 8)

            commission = quantity * fill_price * 0.0004   # 4 bps taker fee
            fill = MockFill(
                order_id=order_id,
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=fill_price,
                commission=commission,
                transact_time=now_ms,
            )
            self._mock_fills.append(fill)

            # Update slippage model with fill price for next calculation
            self.record_price_for_slippage(fill_price)

            # S2: Update mock balances
            self._update_mock_balance(symbol, side, quantity, fill_price)
            # S2: Push position update to RiskGuard (authoritative)
            self._push_position_update(symbol, side, quantity, fill_price)

            logger.info(
                "[TEST] %s %s %s qty=%.6f @ %.6f  (FILLED)",
                side, order_type, symbol, quantity, fill_price,
            )

            return {
                "order_id": order_id,
                "symbol": symbol,
                "side": side,
                "type": order_type,
                "orig_qty": str(quantity),
                "executed_qty": str(quantity),
                "price": str(fill_price),
                "status": OrderState.FILLED.value,
                "commission": str(commission),
                "transact_time": now_ms,
            }

        else:
            # ── LIMIT: enter PENDING state ───────────────────────────────────────
            tracked = TrackedOrder(
                order_id=order_id,
                symbol=symbol,
                side=side,
                order_type=order_type,
                price=price or 0.0,
                orig_qty=quantity,
                state=OrderState.PENDING,
            )
            self._live_orders[order_id] = tracked

            # Start background fill simulation if not already running
            self._ensure_mock_fill_thread()

            logger.info(
                "[TEST] %s %s %s qty=%.6f @ %.6f  (PENDING order_id=%s)",
                side, order_type, symbol, quantity, price, order_id,
            )

            return {
                "order_id": order_id,
                "symbol": symbol,
                "side": side,
                "type": order_type,
                "orig_qty": str(quantity),
                "executed_qty": "0",
                "price": str(price),
                "status": OrderState.PENDING.value,
                "transact_time": now_ms,
            }

    def _live_place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: float | None,
        **kwargs: Any,
    ) -> dict:
        # ── #19: Generate idempotency key BEFORE the retry loop ───────────────
        # Reused on every retry attempt so Binance deduplicates a retried request.
        idempotency_key = f"plutus_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"

        def _call_place_order() -> dict:
            """Make one place_order call, guarded by WriteRateLimiter + idempotency key."""
            # ── #20: Acquire write rate permit BEFORE any write API call ───────────
            # Lazy import avoids circular dependency with binance_client module.
            from src.data.binance_client import WriteRateLimiter
            WriteRateLimiter.wait()
            params: dict[str, Any] = {
                "symbol": symbol,
                "side": side,
                "type": order_type,
                "quantity": quantity,
                # Binance Spot: newClientOrderId is echoed back in responses and
                # is used for native idempotency — same value within a window →
                # same order, no duplicate created.
                "newClientOrderId": idempotency_key,
            }
            if price is not None:
                params["price"] = price
            params.update(kwargs)
            return self._client.place_order(**params)

        # ── #18: Retry loop with exponential backoff (1 s, 2 s, 4 s) ─────────
        # Retriable conditions: timeout, connection error, HTTP 5xx, HTTP 429.
        MAX_RETRIES = 3
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = _call_place_order()
                break
            except Exception as exc:  # noqa: PERF203  # intentional: reraise after loop
                is_retriable = self._is_retriable_error(exc)
                if not is_retriable or attempt == MAX_RETRIES - 1:
                    logger.error("[Executor] place_order failed (non-retriable / max retries): %s", exc)
                    raise
                wait = 2 ** attempt   # 1, 2, 4 seconds
                logger.warning(
                    "[Executor] place_order attempt %d/%d retriable error: %s  → sleeping %.0f s",
                    attempt + 1, MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)
                last_exc = exc

        logger.info("Live order placed: %s", response)

        # S1: Track non-market orders through state machine
        if order_type != "MARKET":
            order_id = str(response.get("orderId", response.get("order_id", "")))
            tracked = TrackedOrder(
                order_id=order_id,
                symbol=symbol,
                side=side,
                order_type=order_type,
                price=price or 0.0,
                orig_qty=quantity,
                state=OrderState.PENDING,
            )
            self._live_orders[order_id] = tracked
        else:
            # MARKET order fills immediately — extract fill data and push to RiskGuard (S2)
            exec_qty = float(response.get("executedQty", 0) or response.get("executed_qty", 0))
            avg_price = float(response.get("price", price) or price or 0)
            if exec_qty > 0 and avg_price > 0:
                self._push_position_update(
                    symbol=symbol,
                    side=side,
                    quantity=exec_qty,
                    price=avg_price,
                )
                # Also record in mock fills for get_position() in test_mode (belt-and-suspenders)
                fill = MockFill(
                    order_id=str(response.get("orderId", "")),
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    quantity=exec_qty,
                    price=avg_price,
                    commission=exec_qty * avg_price * 0.0004,
                    transact_time=int(response.get("transactTime", 0)) or int(time.time() * 1000),
                )
                self._mock_fills.append(fill)

        return response

    # ── #18 helper: classify retriable errors ─────────────────────────────────

    @staticmethod
    def _is_retriable_error(exc: Exception) -> bool:
        """Return True when the exception represents a transient Binance error."""
        import requests
        # requests Timeout / ConnectionError
        if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            return True
        # HTTP status 429 (rate limit) or 5xx (server error)
        if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
            status = exc.response.status_code
            if status == 429 or 500 <= status < 600:
                return True
        # Binance error code payloads (e.g. {"code": -1001, "msg": "..."})
        if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
            try:
                body = exc.response.json()
                # -1001 = Internal error, -1021 = Timestamp bias, -1015 = Too many new orders
                retriable_codes = {-1001, -1021, -1015}
                if body.get("code") in retriable_codes:
                    return True
            except Exception:
                pass
        return False

    # -------------------------------------------------------------------------
    # Order state machine — status queries and updates (S1)
    # -------------------------------------------------------------------------

    def get_order_status(self, order_id: str) -> OrderState:
        """
        Return the current state of a tracked order.

        In test_mode LIMIT orders are driven by _mock_fill_thread.
        In live mode this reflects the last update received via webhook or poll.

        Parameters
        ----------
        order_id : str
            The order identifier returned by place_order().

        Returns
        -------
        OrderState
            Current state; OrderState.REJECTED if the order is not found.
        """
        tracked = self._live_orders.get(order_id)
        if tracked is None:
            return OrderState.REJECTED
        return tracked.state

    def update_order_status(
        self,
        order_id: str,
        state: OrderState,
        filled_qty: float | None = None,
        fill_price: float | None = None,
    ) -> bool:
        """
        Update the state of a tracked order.

        Called by:
        - Webhook / callback from Binance (live mode)
        - Reconciliation loop in LimitOrderQueue (S4)

        Parameters
        ----------
        order_id   : str
        state      : OrderState — new state
        filled_qty : float, optional — total quantity filled (for PARTIAL/FILLED)
        fill_price : float, optional — average fill price

        Returns
        -------
        bool — True if the order was found and updated; False if not tracked.
        """
        tracked = self._live_orders.get(order_id)
        if tracked is None:
            logger.warning("[Executor] update_order_status: order %s not tracked", order_id)
            return False

        if tracked.is_terminal:
            logger.warning(
                "[Executor] update_order_status: order %s already terminal (%s); ignoring update to %s",
                order_id, tracked.state.value, state.value,
            )
            return False

        old_state = tracked.state
        tracked.state = state
        tracked.last_update_ms = int(time.time() * 1000)

        if filled_qty is not None:
            tracked.filled_qty = filled_qty
        if fill_price is not None:
            # Update tracked price to last fill price
            tracked.price = fill_price

        logger.info(
            "[Executor] Order %s state transition: %s → %s  filled_qty=%.6f",
            order_id, old_state.value, state.value, tracked.filled_qty,
        )

        # S2: If terminal (FILLED/CANCELLED), push position update to RiskGuard
        if state in (OrderState.FILLED, OrderState.CANCELLED):
            self._on_order_terminal(tracked)

        return True

    def _on_order_terminal(self, tracked: TrackedOrder) -> None:
        """S2: On terminal state, push position update to RiskGuard.

        Keeps the order in _live_orders with its terminal state so that callers
        (including tests) can still query the final state via get_order_status().
        """
        if tracked.state == OrderState.FILLED and tracked.filled_qty > 0:
            self._push_position_update(
                tracked.symbol,
                tracked.side,
                tracked.filled_qty,
                tracked.price,
            )
        # Keep in _live_orders — callers query state after terminal

    def _push_position_update(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
    ) -> None:
        """
        S2: Push a position delta to RiskGuard.

        BinanceExecutor.record_fill() calls this after every confirmed fill so
        that RiskGuard remains the single authoritative position source.
        RiskGuard.record_trade() adds to exposure; close_position() removes it.
        """
        if self._risk_guard is None:
            return
        try:
            notional = quantity * price
            if side == "BUY":
                self._risk_guard.record_trade(notional=notional, symbol=symbol)
            else:
                # SELL: reduce/close existing position exposure
                self._risk_guard.close_position(notional=notional, symbol=symbol)
            logger.debug(
                "[Executor→RiskGuard] %s %s %s qty=%.6f @ %.6f",
                "record_trade" if side == "BUY" else "close_position",
                side, symbol, quantity, price,
            )
        except Exception as exc:
            logger.error(
                "[Executor] Failed to push position update to RiskGuard: %s", exc,
            )

    # -------------------------------------------------------------------------
    # S1: Mock LIMIT order fill simulation
    # -------------------------------------------------------------------------

    def _ensure_mock_fill_thread(self) -> None:
        """Start the background thread that simulates LIMIT order fills in test mode."""
        if self._mock_fill_thread is not None and self._mock_fill_thread.is_alive():
            return
        self._mock_fill_thread_running = True
        self._mock_fill_thread = threading.Thread(
            target=self._mock_fill_loop,
            name="mock_fill_thread",
            daemon=True,
        )
        self._mock_fill_thread.start()
        logger.debug("[TEST] Mock fill simulation thread started")

    def _mock_fill_loop(self) -> None:
        """
        Background loop that transitions LIMIT orders from PENDING → PARTIAL → FILLED.

        Every 2 seconds, for each PENDING/PARTIAL tracked order:
        1. Simulate a partial fill of up to 20 % of remaining quantity at the order price.
        2. Update order state: PENDING → PARTIAL, PARTIAL → FILLED.
        3. Call _push_position_update() so RiskGuard stays in sync (S2).
        4. Remove FILLED/CANCELLED orders from _live_orders.
        """
        while self._mock_fill_thread_running:
            time.sleep(2.0)

            # Snapshot keys to avoid dict-changed-during-iteration issues
            order_ids = list(self._live_orders.keys())
            for order_id in order_ids:
                if not self._mock_fill_thread_running:
                    break
                tracked = self._live_orders.get(order_id)
                if tracked is None or tracked.is_terminal:
                    continue

                # Simulate partial fill: up to 20 % of remaining, minimum 1 unit
                remaining = tracked.remaining_qty
                if remaining <= 1e-9:
                    # Fully filled but not yet marked — should not happen
                    self.update_order_status(order_id, OrderState.FILLED, tracked.orig_qty, tracked.price)
                    continue

                fill_qty = min(
                    remaining,
                    max(tracked.orig_qty * 0.20, 1e-8),
                )
                fill_price = tracked.price

                commission = fill_qty * fill_price * 0.0004
                fill = MockFill(
                    order_id=order_id,
                    symbol=tracked.symbol,
                    side=tracked.side,
                    order_type=tracked.order_type,
                    quantity=fill_qty,
                    price=fill_price,
                    commission=commission,
                    transact_time=int(time.time() * 1000),
                )
                self._mock_fills.append(fill)

                # Update balance
                self._update_mock_balance(tracked.symbol, tracked.side, fill_qty, fill_price)

                new_filled = tracked.filled_qty + fill_qty
                if new_filled >= tracked.orig_qty * 0.999:
                    # Full fill
                    self.update_order_status(order_id, OrderState.FILLED, new_filled, fill_price)
                else:
                    # Partial fill
                    tracked.filled_qty = new_filled
                    tracked.state = OrderState.PARTIAL
                    tracked.last_update_ms = int(time.time() * 1000)
                    # S2: Push partial position update
                    self._push_position_update(tracked.symbol, tracked.side, fill_qty, fill_price)
                    logger.info(
                        "[TEST] %s %s partial fill qty=%.6f @ %.6f  (%.1f%% done)",
                        tracked.side, tracked.symbol, fill_qty, fill_price,
                        new_filled / tracked.orig_qty * 100,
                    )

    def _update_mock_balance(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
    ) -> None:
        """Update mock USDT/BTC/ETH balances after a fill.

        Asset extraction by suffix detection:
            "BTCUSDT" → base="BTC", quote="USDT"
            "ETHUSDT" → base="ETH", quote="USDT"
        """
        # Detect quote suffix (USDT pairs are most common)
        for suffix, base in [("USDT", symbol[:-4]), ("BTC", symbol[:-3])]:
            if symbol.endswith(suffix) and len(base) > 0 and base.isalpha():
                base_asset = base
                quote_asset = suffix
                break
        else:
            # Fallback: strip USDT, then BTC (covers BTCUSDT correctly)
            base_asset = symbol.replace("USDT", "").replace("BTC", "")
            quote_asset = "USDT"

        if side == "BUY":
            cost = quantity * price
            self._mock_balances[quote_asset] = (
                self._mock_balances.get(quote_asset, 0.0) - cost
            )
            self._mock_balances[base_asset] = (
                self._mock_balances.get(base_asset, 0.0) + quantity
            )
        else:
            proceeds = quantity * price
            self._mock_balances[quote_asset] = (
                self._mock_balances.get(quote_asset, 0.0) + proceeds
            )
            self._mock_balances[base_asset] = (
                self._mock_balances.get(base_asset, 0.0) - quantity
            )

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """
        Cancel an open order.

        Returns True on success, False otherwise.  In test_mode LIMIT orders
        transition to CANCELLED and are removed from the live order tracker.
        """
        # S1: Check tracked orders first
        tracked = self._live_orders.get(order_id)
        if tracked is not None:
            if tracked.is_terminal:
                logger.warning(
                    "[Executor] cancel_order: order %s already terminal (%s)",
                    order_id, tracked.state.value,
                )
                return False
            if self.test_mode:
                # S1: Apply cancellation in mock mode
                self.update_order_status(order_id, OrderState.CANCELLED, tracked.filled_qty, tracked.price)
                logger.info("[TEST] Cancelled tracked order %s", order_id)
                return True

        # Fall back to legacy mock_orders dict (backward compat)
        if self.test_mode:
            if order_id in self._mock_orders:
                del self._mock_orders[order_id]
                logger.info("[TEST] Cancelled legacy mock order %s", order_id)
                return True
            logger.warning("[TEST] Order %s not found for cancellation", order_id)
            return False

        # ── #18: Retry loop for cancel_order (1 s, 2 s, 4 s) ─────────────────
        MAX_RETRIES = 3
        for attempt in range(MAX_RETRIES):
            try:
                result = self._client.cancel_order(symbol=symbol, orderId=order_id)
                if result.get("status") == "CANCELED":
                    # S1: Mark as cancelled in tracker if present
                    self.update_order_status(order_id, OrderState.CANCELLED)
                    return True
                return False
            except Exception as exc:
                is_retriable = self._is_retriable_error(exc)
                if not is_retriable or attempt == MAX_RETRIES - 1:
                    logger.error("Cancel order failed (non-retriable / max retries): %s", exc)
                    return False
                wait = 2 ** attempt
                logger.warning(
                    "[Executor] cancel_order attempt %d/%d retriable error: %s  → sleeping %.0f s",
                    attempt + 1, MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)

    def get_open_orders(self, symbol: str) -> list[TrackedOrder]:
        """
        Return all non-terminal tracked orders for a symbol.

        Used by LimitOrderQueue.reconcile() (S4) to cross-check with Binance.
        """
        return [
            o for o in self._live_orders.values()
            if o.symbol == symbol and not o.is_terminal
        ]

    # -------------------------------------------------------------------------
    # Account & position queries
    # -------------------------------------------------------------------------

    def get_balance(self, asset: str) -> float:
        """
        Return the free balance for a given asset.

        Parameters
        ----------
        asset : str
            Asset symbol, e.g. "USDT", "BTC", "ETH".

        Returns
        -------
        float : free balance in base units.
        """
        if self.test_mode:
            return self._mock_balances.get(asset, 0.0)

        try:
            balances = self._client.get_account()["balances"]
            for b in balances:
                if b["asset"] == asset:
                    return float(b["free"])
            return 0.0
        except Exception as exc:
            logger.error("get_balance failed for %s: %s", asset, exc)
            return 0.0

    def get_position(self, symbol: str) -> dict:
        """
        Return current position details for a symbol.

        WARNING: In the unified model (S2) RiskGuard is the authoritative
        position source.  This method returns a best-effort snapshot derived
        from mock fills for test_mode only.

        Parameters
        ----------
        symbol : str
            Trading pair, e.g. "BTCUSDT".

        Returns
        -------
        dict with keys:
            - size        : float  — net position size (positive = long)
            - entry_price : float  — average entry price
            - unrealized_pnl : float
        """
        if self.test_mode:
            fills = [f for f in self._mock_fills if f.symbol == symbol]
            if not fills:
                return {"size": 0.0, "entry_price": 0.0, "unrealized_pnl": 0.0}

            # Aggregate from fills
            buys  = [(f.quantity, f.price) for f in fills if f.side == "BUY"]
            sells = [(f.quantity, f.price) for f in fills if f.side == "SELL"]
            total_buy  = sum(q for q, _ in buys)
            total_sell = sum(q for q, _ in sells)
            net_size   = total_buy - total_sell

            avg_entry = 0.0
            if buys:
                avg_entry = sum(q * p for q, p in buys) / total_buy

            # Mark price from last fill for unrealised PnL
            mark = fills[-1].price if fills else 0.0
            unrealised = net_size * (mark - avg_entry) if net_size != 0 else 0.0

            return {
                "size": net_size,
                "entry_price": avg_entry,
                "unrealized_pnl": unrealised,
            }

        # Live mode: query RiskGuard for authoritative position
        if self._risk_guard is not None:
            exposure = self._risk_guard.get_open_exposure()
            notional = exposure.get(symbol, 0.0)
            # Get current mark price from Binance ticker
            try:
                ticker = self._client.get_symbol_ticker(symbol=symbol)
                mark = float(ticker.get("price", 0.0))
            except Exception:
                mark = 0.0
            size = notional / mark if mark > 0 else 0.0
            return {"size": size, "entry_price": 0.0, "unrealized_pnl": 0.0}

        # Live mode, no RiskGuard: query USD&M Futures position endpoint directly
        try:
            fut = self._client.get_futures_position(symbol)
            return {
                "size": fut.get("size", 0.0),
                "entry_price": fut.get("entry_price", 0.0),
                "unrealized_pnl": fut.get("unrealized_pnl", 0.0),
            }
        except Exception as exc:
            logger.error("get_position live futures query failed for %s: %s", symbol, exc)

        return {"size": 0.0, "entry_price": 0.0, "unrealized_pnl": 0.0}

    # -------------------------------------------------------------------------
    # Test helpers
    # -------------------------------------------------------------------------

    def set_mock_balance(self, asset: str, amount: float) -> None:
        """Convenience: pre-seed a mock balance for testing."""
        self._mock_balances[asset] = amount

    def get_mock_fills(self) -> list[MockFill]:
        """Return all mock fills for the session."""
        return list(self._mock_fills)

    def reset_mock_state(self) -> None:
        """
        Clear all mock state between test runs.

        Also stops and restarts the mock fill simulation thread.
        """
        self._mock_fill_thread_running = False
        if self._mock_fill_thread is not None:
            self._mock_fill_thread.join(timeout=2)
            self._mock_fill_thread = None

        self._live_orders.clear()
        self._mock_orders.clear()
        self._mock_fills.clear()
        self._mock_balances.clear()

        logger.debug("[TEST] Mock state reset")


# ─── S2: PositionUpdate helper ────────────────────────────────────────────────
# Type alias documented above; re-export for use in other modules.
__all__ = ["BinanceExecutor", "OrderState", "TrackedOrder"]


# -----------------------------------------------------------------------
# Test block
# -----------------------------------------------------------------------
if __name__ == "__main__":
    import logging as _logging

    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s | %(message)s")

    # Stand-in client (not used in test_mode)
    dummy_client = object()

    exec_ = BinanceExecutor(binance_client=dummy_client, test_mode=True)
    exec_.set_mock_balance("USDT", 50_000.0)
    exec_.set_mock_balance("BTC", 0.0)

    # ── 1. Market buy — immediate FILLED (S1: PENDING → FILLED) ───────────
    result = exec_.place_order(
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity=0.5,
        price=60_000.0,
    )
    print(f"[1] Market buy result: status={result['status']}  order_id={result['order_id']}")
    assert result["status"] == OrderState.FILLED.value, f"Expected FILLED, got {result['status']}"

    # ── 2. Limit sell — PENDING state (S1) ─────────────────────────────────
    result2 = exec_.place_order(
        symbol="BTCUSDT",
        side="SELL",
        order_type="LIMIT",
        quantity=0.1,
        price=65_000.0,
        timeInForce="GTC",
    )
    limit_order_id = result2["order_id"]
    print(f"[2] Limit sell result: status={result2['status']}  order_id={limit_order_id}")
    assert result2["status"] == OrderState.PENDING.value, f"Expected PENDING, got {result2['status']}"

    # ── 3. State machine: wait for PARTIAL / FILLED ────────────────────────
    print("[3] Waiting up to 6s for LIMIT order state transitions...")
    deadline = time.time() + 6.0
    states_seen = set()
    while time.time() < deadline:
        state = exec_.get_order_status(limit_order_id)
        states_seen.add(state.value)
        if state == OrderState.FILLED:
            print(f"    → FILLED after {6.0 - (deadline - time.time()):.1f}s")
            break
        if state == OrderState.PARTIAL:
            print(f"    → PARTIAL (still open)")
        time.sleep(0.5)  # Poll frequently to catch fast transitions

    print(f"    States observed: {sorted(states_seen)}")
    # At least PARTIAL or FILLED should have been reached (order progressed)
    assert OrderState.PARTIAL.value in states_seen or OrderState.FILLED.value in states_seen, \
        f"Expected PARTIAL or FILLED; got {sorted(states_seen)}"

    # ── 4. Balance queries ──────────────────────────────────────────────────
    btc_bal = exec_.get_balance("BTC")
    usdt_bal = exec_.get_balance("USDT")
    print(f"[4] BTC balance: {btc_bal}  |  USDT balance: {usdt_bal:.2f}")
    assert btc_bal > 0, "Should have BTC after market buy"
    assert usdt_bal < 50_000, "USDT should be reduced after market buy"

    # ── 5. Position query ───────────────────────────────────────────────────
    pos = exec_.get_position("BTCUSDT")
    print(f"[5] Position: {pos}")

    # ── 6. Cancel the pending limit order ───────────────────────────────────
    cancelled = exec_.cancel_order("BTCUSDT", limit_order_id)
    print(f"[6] Cancel order: {cancelled}")
    assert cancelled is True, "Should successfully cancel tracked order"

    # State after cancellation
    final_state = exec_.get_order_status(limit_order_id)
    print(f"    Final state after cancel: {final_state.value}")

    # ── 7. Reset mock state ─────────────────────────────────────────────────
    exec_.reset_mock_state()
    print("[7] reset_mock_state() called without error")

    # ── 8. RiskGuard integration (S2) ───────────────────────────────────────
    print("\n[8] Testing RiskGuard integration (S2)...")

    class _DummyRiskGuard:
        def __init__(self):
            self.trades: list[dict] = []
        def record_trade(self, notional, symbol):
            self.trades.append({"notional": notional, "symbol": symbol})
            print(f"    [RiskGuard] record_trade called: notional={notional:.2f}  symbol={symbol}")

    rg = _DummyRiskGuard()
    exec_rg = BinanceExecutor(binance_client=dummy_client, test_mode=True, risk_guard=rg)
    exec_rg.set_mock_balance("USDT", 100_000.0)

    market_result = exec_rg.place_order(
        symbol="ETHUSDT", side="BUY", order_type="MARKET",
        quantity=2.0, price=2_000.0,
    )
    assert len(rg.trades) == 1, f"Expected 1 RiskGuard call, got {len(rg.trades)}"
    assert rg.trades[0]["symbol"] == "ETHUSDT"
    print(f"    RiskGuard trades after fill: {rg.trades}")

    print("\n" + "=" * 60)
    print("ALL STATE-MACHINE + RISK-GUARD TESTS PASSED")
    print("=" * 60)
