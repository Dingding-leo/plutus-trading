"""
Backtesting Engine - Core engine for strategy backtesting.

DEPRECATION NOTICE (P2):
    BacktestEngine (this file) and ChronosBacktester
    (src/backtest/chronos_engine.py) use DIFFERENT liquidation formulas:

    - BacktestEngine._check_liquidation (line ~295):
        liq_price = entry * (1 ± 1/leverage - buffer)
        Buffer is SUBTRACTED, making liquidation easier (conservative).

    - ChronosBacktester uses RiskGuard.check_liquidation_buffer() which
        computes distance_to_liquidation_pct = (entry - liq_price) / entry
        and enforces a MINIMUM DISTANCE (1.5% for major coins, 2.5% for small).

    ChronosBacktester is canonical. BacktestEngine is kept for legacy strategy
    compatibility (production_strategy.py, aggressive_strategy.py) but its
    _check_liquidation formula is NOT the same as the production liquidation logic.
    Do not use BacktestEngine results as ground truth for production risk.
"""

import copy
import math
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum
import json

from .data_client import data_client
from ..analysis import indicators, volume_profile, market_context
from ..execution import position_sizer, decision_engine, trade_plan


class TradeDirection(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass
class Trade:
    """Represents a single trade."""
    entry_time: datetime
    entry_price: float
    direction: TradeDirection
    size: float
    leverage: float
    stop_loss: float = None
    take_profit: float = None
    exit_time: datetime = None
    exit_price: float = None
    pnl: float = 0
    pnl_pct: float = 0
    fees: float = 0
    status: str = "OPEN"  # OPEN, CLOSED

    def close(self, exit_price: float, exit_time: datetime, fees: float = 0):
        """Close the trade."""
        self.exit_price = exit_price
        self.exit_time = exit_time
        self.fees += fees

        if self.direction == TradeDirection.LONG:
            self.pnl = (exit_price - self.entry_price) * self.size
            # FIX #38: Use log returns for proper compounding
            self.pnl_pct = (exit_price / self.entry_price - 1) * 100
        else:
            self.pnl = (self.entry_price - exit_price) * self.size
            # FIX #38: Use log returns for proper compounding
            self.pnl_pct = (self.entry_price / exit_price - 1) * 100

        self.pnl -= self.fees
        self.status = "CLOSED"


@dataclass
class BacktestResult:
    """Results from backtesting."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0
    total_pnl: float = 0
    total_pnl_pct: float = 0
    max_drawdown: float = 0
    max_drawdown_pct: float = 0
    sharpe_ratio: float = 0
    profit_factor: float = 0
    avg_win: float = 0
    avg_loss: float = 0
    avg_holding_period: float = 0
    initial_equity: float = 10000
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[Dict] = field(default_factory=list)

    def calculate_metrics(self):
        """Calculate final metrics."""
        if not self.trades:
            return

        closed_trades = [t for t in self.trades if t.status == "CLOSED"]
        self.total_trades = len(closed_trades)

        if self.total_trades == 0:
            return

        winning = [t for t in closed_trades if t.pnl > 0]
        losing = [t for t in closed_trades if t.pnl <= 0]

        self.winning_trades = len(winning)
        self.losing_trades = len(losing)

        self.total_pnl = sum(t.pnl for t in closed_trades)

        # Total return percentage
        self.total_pnl_pct = (self.total_pnl / self.initial_equity) * 100 if self.initial_equity > 0 else 0

        # Win rate
        self.win_rate = self.winning_trades / self.total_trades * 100 if self.total_trades > 0 else 0

        # Avg win/loss
        self.avg_win = sum(t.pnl for t in winning) / len(winning) if winning else 0
        self.avg_loss = sum(t.pnl for t in losing) / len(losing) if losing else 0

        # Profit factor
        gross_wins = sum(t.pnl for t in winning) if winning else 0
        gross_losses = abs(sum(t.pnl for t in losing)) if losing else 0
        self.profit_factor = gross_wins / gross_losses if gross_losses > 0 else 0

        # FIX #36: Calculate equity curve with unrealized PnL between trades.
        # equity_snapshots: list of (timestamp, equity, unrealized_pnl) recorded
        # during the backtest run loop. Each entry is a mark-to-market snapshot.
        equity = self.initial_equity
        peak = equity
        equity_curve = []

        # Merge closed-trade equity steps with time-indexed MTM snapshots.
        # Build a map of timestamp -> snapshot from equity_snapshots.
        snapshot_map: Dict[datetime, float] = {}
        unrealized_at_end = 0.0
        for snap in getattr(self, "equity_snapshots", []):
            ts = snap.get("time")
            if ts is not None and snap.get("equity") is not None:
                snapshot_map[ts] = snap["equity"]
                unrealized_at_end = snap.get("unrealized_pnl", 0.0)

        for trade in closed_trades:
            equity += trade.pnl
            equity_curve.append({
                "time": trade.exit_time,
                "equity": equity,
                "realized": True,
                "unrealized_pnl": 0.0,
            })

            if equity > peak:
                peak = equity

            drawdown = (peak - equity) / peak * 100
            if drawdown > self.max_drawdown_pct:
                self.max_drawdown_pct = drawdown
                self.max_drawdown = peak - equity

        # FIX #36 (continued): also expose the full MTM equity curve so
        # drawdowns are visible even when positions are open.
        # If snapshots were recorded, include them alongside closed-trade points.
        if snapshot_map:
            # Deduplicate: keep earliest snapshot per timestamp.
            merged: List[Dict] = []
            seen_times: set = set()
            for pt in equity_curve:
                if pt["time"] not in seen_times:
                    merged.append(pt)
                    seen_times.add(pt["time"])
            # Add MTM snapshots that fall between (or after) closed trades.
            for ts in sorted(snapshot_map.keys()):
                if ts not in seen_times:
                    mtm_equity = snapshot_map[ts]
                    mtm_peak = max(mtm_equity, peak)
                    mtm_drawdown = (mtm_peak - mtm_equity) / mtm_peak * 100 if mtm_peak > 0 else 0
                    if mtm_drawdown > self.max_drawdown_pct:
                        self.max_drawdown_pct = mtm_drawdown
                        self.max_drawdown = mtm_peak - mtm_equity
                    merged.append({
                        "time": ts,
                        "equity": mtm_equity,
                        "realized": False,
                        "unrealized_pnl": unrealized_at_end,
                    })
                    if mtm_equity > peak:
                        peak = mtm_equity
            equity_curve = sorted(merged, key=lambda x: x["time"])

        self.equity_curve = equity_curve

        # FIX #37: Compute Sharpe ratio from time-indexed equity returns (not
        # trade-level returns) and annualise properly using sqrt(252).
        # Uses the equity_snapshots recorded at each backtest timestamp.
        time_returns: List[float] = []
        if snapshot_map:
            # Build a sorted list of snapshot values (MTM equity at each ts).
            sorted_snaps = sorted(snapshot_map.items(), key=lambda x: x[0])
            prev_equity_mtm = self.initial_equity
            for ts, eq in sorted_snaps:
                if prev_equity_mtm > 0:
                    # FIX #38: Use log returns for proper compounding
                    log_ret = math.log(eq / prev_equity_mtm) if eq > 0 and prev_equity_mtm > 0 else 0.0
                    time_returns.append(log_ret)
                prev_equity_mtm = eq
        elif len(closed_trades) > 1:
            # Fallback: time-indexed using exit timestamps of closed trades.
            sorted_trades = sorted(closed_trades, key=lambda t: t.exit_time or datetime.min)
            prev_equity_mtm = self.initial_equity
            for t in sorted_trades:
                if t.exit_time and prev_equity_mtm > 0:
                    eq = prev_equity_mtm + t.pnl
                    log_ret = math.log(eq / prev_equity_mtm) if eq > 0 and prev_equity_mtm > 0 else 0.0
                    time_returns.append(log_ret)
                    prev_equity_mtm = eq

        if len(time_returns) > 1:
            mean_ret = sum(time_returns) / len(time_returns)
            variance = sum((r - mean_ret) ** 2 for r in time_returns) / len(time_returns)
            std_ret = variance ** 0.5
            # Annualise: assume daily data (252 trading days); sqrt(252) annualisation.
            self.sharpe_ratio = (mean_ret / std_ret) * math.sqrt(252) if std_ret > 0 else 0.0
        else:
            self.sharpe_ratio = 0.0

        # Avg holding period
        holding_times = []
        for t in closed_trades:
            if t.exit_time and t.entry_time:
                holding = (t.exit_time - t.entry_time).total_seconds() / 3600
                holding_times.append(holding)
        self.avg_holding_period = sum(holding_times) / len(holding_times) if holding_times else 0


class BacktestEngine:
    """Main backtesting engine."""

    def __init__(
        self,
        initial_equity: float = 10000,
        maker_fee: float = 0.0002,  # 0.02% (OKX maker)
        taker_fee: float = 0.0005,   # 0.05% (OKX taker)
        slippage_pct: float = 0.0005,  # 0.05%
    ):
        self.initial_equity = initial_equity
        self.equity = initial_equity
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.slippage_pct = slippage_pct
        self.trades: List[Trade] = []
        self.open_trades: Dict[str, Trade] = {}  # symbol -> trade

    def reset(self):
        """Reset engine state."""
        self.equity = self.initial_equity
        self.trades = []
        self.open_trades = {}
        self.equity_snapshots = []

    def get_unrealized_pnl(self, current_prices: Dict[str, float] = None) -> float:
        """
        FIX #36: Calculate unrealized PnL from all open positions.
        current_prices: dict of symbol -> current price.
        If not provided, uses entry price (no MTM gain/loss assumed).
        """
        unrealized = 0.0
        for symbol, trade in self.open_trades.items():
            price = (current_prices or {}).get(symbol, trade.entry_price)
            if trade.direction == TradeDirection.LONG:
                pos_pnl = (price - trade.entry_price) * trade.size - trade.fees
            else:
                pos_pnl = (trade.entry_price - price) * trade.size - trade.fees
            unrealized += pos_pnl
        return unrealized

    def record_equity_snapshot(self, timestamp: datetime, current_prices: Dict[str, float] = None):
        """
        FIX #36: Record a mark-to-market equity snapshot including unrealized PnL.
        Call this at each backtest timestep so the equity curve shows open
        position drawdown (not just realised gaps).
        """
        unrealized = self.get_unrealized_pnl(current_prices)
        mtm_equity = self.equity + unrealized
        self.equity_snapshots.append({
            "time": timestamp,
            "equity": mtm_equity,
            "realized_equity": self.equity,
            "unrealized_pnl": unrealized,
        })

    def apply_slippage(self, price: float, direction: TradeDirection) -> float:
        """Apply slippage to price."""
        if direction == TradeDirection.LONG:
            return price * (1 + self.slippage_pct)
        else:
            return price * (1 - self.slippage_pct)

    def calculate_entry_fees(self, size: float, price: float) -> float:
        """Calculate entry fees using maker fee (backtest assumes limit orders)."""
        return size * price * self.maker_fee

    def calculate_exit_fees(self, size: float, price: float) -> float:
        """Calculate exit fees."""
        return size * price * self.maker_fee

    def open_trade(
        self,
        symbol: str,
        direction: TradeDirection,
        entry_price: float,
        size: float,
        leverage: float = 1,
        stop_loss: float = None,
        take_profit: float = None,
        timestamp: datetime = None
    ) -> Trade:
        """Open a new trade."""
        if timestamp is None:
            timestamp = datetime.now()

        # Apply slippage to entry
        actual_entry = self.apply_slippage(entry_price, direction)

        # Calculate fees
        fees = self.calculate_entry_fees(size, actual_entry)

        # Create trade
        trade = Trade(
            entry_time=timestamp,
            entry_price=actual_entry,
            direction=direction,
            size=size,
            leverage=leverage,
            stop_loss=stop_loss,
            take_profit=take_profit,
            fees=fees,
        )

        self.trades.append(trade)
        self.open_trades[symbol] = trade

        return trade

    def close_trade(
        self,
        symbol: str,
        exit_price: float,
        timestamp: datetime = None,
        reason: str = "SIGNAL"
    ) -> Optional[Trade]:
        """Close an open trade."""
        if symbol not in self.open_trades:
            return None

        if timestamp is None:
            timestamp = datetime.now()

        trade = self.open_trades[symbol]

        # Apply slippage to exit
        actual_exit = self.apply_slippage(exit_price, trade.direction)

        # Calculate exit fees
        exit_fees = self.calculate_exit_fees(trade.size, actual_exit)

        # Close trade
        trade.close(actual_exit, timestamp, exit_fees)

        # Update equity
        self.equity += trade.pnl

        # Remove from open trades
        del self.open_trades[symbol]

        return trade

    def check_stop_take(
        self,
        symbol: str,
        current_price: float,
        timestamp: datetime = None
    ) -> Optional[str]:
        """Check if stop loss, take profit, or liquidation is hit."""
        if symbol not in self.open_trades:
            return None

        trade = self.open_trades[symbol]
        reason = None

        # Check liquidation first (CRITICAL)
        liq_triggered = self._check_liquidation(trade, current_price)
        if liq_triggered:
            self.close_trade(symbol, current_price, timestamp, "LIQUIDATION")
            return "LIQUIDATION"

        if trade.direction == TradeDirection.LONG:
            if trade.stop_loss and current_price <= trade.stop_loss:
                self.close_trade(symbol, trade.stop_loss, timestamp, "STOP_LOSS")
                reason = "STOP_LOSS"
            elif trade.take_profit and current_price >= trade.take_profit:
                self.close_trade(symbol, trade.take_profit, timestamp, "TAKE_PROFIT")
                reason = "TAKE_PROFIT"
        else:  # SHORT
            if trade.stop_loss and current_price >= trade.stop_loss:
                self.close_trade(symbol, trade.stop_loss, timestamp, "STOP_LOSS")
                reason = "STOP_LOSS"
            elif trade.take_profit and current_price <= trade.take_profit:
                self.close_trade(symbol, trade.take_profit, timestamp, "TAKE_PROFIT")
                reason = "TAKE_PROFIT"

        return reason

    def _check_liquidation(self, trade, current_price: float) -> bool:
        """
        Check if position is liquidated.
        Liquidation occurs when price moves against position beyond leverage threshold.
        Buffer is SUBTRACTED to make liquidation easier to trigger (conservative).
        """
        buffer = 0.001  # 0.1% buffer - SUBTRACTED to make liq easier

        if trade.direction == TradeDirection.LONG:
            # Long: liquidation when price drops below entry - (1/leverage)
            liq_price = trade.entry_price * (1 - 1.0 / trade.leverage - buffer)
            return current_price <= liq_price
        else:
            # Short: liquidation when price rises above entry + (1/leverage)
            # Buffer is SUBTRACTED so liquidation triggers slightly earlier (conservative)
            liq_price = trade.entry_price * (1 + 1.0 / trade.leverage - buffer)
            return current_price >= liq_price

    def get_results(self, final_prices: dict = None) -> BacktestResult:
        """Get backtest results.

        Args:
            final_prices: Optional dict of symbol -> final price for closing open trades
        """
        # Close any open trades at final price
        for symbol, trade in list(self.open_trades.items()):
            # Use final price if provided, otherwise use entry price (was the bug)
            close_price = final_prices.get(symbol, trade.entry_price) if final_prices else trade.entry_price
            self.close_trade(symbol, close_price, datetime.now(), "END_OF_BACKTEST")

        result = BacktestResult(trades=self.trades, initial_equity=self.initial_equity)
        # FIX #36: Transfer equity snapshots so calculate_metrics can use them
        # for the mark-to-market equity curve and time-indexed Sharpe ratio.
        result.equity_snapshots = self.equity_snapshots
        result.calculate_metrics()
        return result


class MultiCoinBacktester:
    """Backtester that scans multiple coins."""

    def __init__(self, engine: BacktestEngine = None):
        self.engine = engine or BacktestEngine()
        self.data_cache: Dict[str, Dict[str, List[dict]]] = {}  # symbol -> timeframe -> candles

    def fetch_data(
        self,
        symbols: List[str],
        timeframes: List[str],
        start_date: str,
        end_date: str = None,
        market: str = "futures",
    ):
        """Fetch historical data for all symbols."""
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        print(f"Fetching data for {len(symbols)} symbols, {len(timeframes)} timeframes...")
        print(f"Market: {market.upper()}")

        for symbol in symbols:
            self.data_cache[symbol] = {}
            for tf in timeframes:
                print(f"  {symbol} {tf}...", end=" ")
                try:
                    candles = data_client.fetch_history(
                        symbol,
                        tf,
                        start_date,
                        end_date,
                        max_candles=5000,
                        market=market,
                    )
                    self.data_cache[symbol][tf] = candles
                    print(f"{len(candles)} candles")
                except Exception as e:
                    print(f"Error: {e}")
                    self.data_cache[symbol][tf] = []

    def run(
        self,
        symbols: List[str],
        timeframes: List[str],
        strategy_fn: Callable,
        start_date: str,
        end_date: str = None,
        market: str = "futures",
    ):
        """Run backtest with strategy."""
        # Fetch data
        self.fetch_data(symbols, timeframes, start_date, end_date, market=market)

        # Get all unique timestamps across all data
        all_timestamps = set()
        for symbol in symbols:
            for tf in timeframes:
                for c in self.data_cache.get(symbol, {}).get(tf, []):
                    all_timestamps.add(c["timestamp"])

        sorted_timestamps = sorted(all_timestamps)

        print(f"\nRunning backtest from {start_date} to {end_date}...")
        print(f"Total time points: {len(sorted_timestamps)}")

        # FIX #36: Record equity snapshot at each timestamp so the equity curve
        # reflects mark-to-market (including open-position unrealized PnL).
        current_prices: Dict[str, float] = {}

        for i, ts in enumerate(sorted_timestamps):
            current_time = datetime.fromtimestamp(ts / 1000)

            if i % 1000 == 0:
                print(f"  Progress: {i}/{len(sorted_timestamps)}")

            # Update current prices for all symbols using the latest candle close.
            # This is needed for mark-to-market unrealized PnL in snapshots.
            for symbol in symbols:
                candles = self.data_cache.get(symbol, {}).get("1h", [])
                past = [c for c in candles if c["timestamp"] <= ts]
                if past:
                    current_prices[symbol] = past[-1]["close"]

            # Record MTM equity snapshot BEFORE processing this timestamp's signals.
            # Unrealized PnL is computed using the prices from the previous step,
            # which is the correct "end-of-previous-bar" mark-to-market.
            self.engine.record_equity_snapshot(current_time, current_prices.copy())

            # Check stops/TPs for all open positions before evaluating new entries.
            for symbol in list(self.engine.open_trades.keys()):
                price = current_prices.get(symbol)
                if price is not None:
                    self.engine.check_stop_take(symbol, price, current_time)

            # Run strategy for each symbol
            for symbol in symbols:
                strategy_fn(
                    self.engine,
                    symbol,
                    self.data_cache.get(symbol, {}),
                    current_time,
                    ts
                )

        # Get results
        return self.engine.get_results()


def format_results(result: BacktestResult) -> str:
    """Format backtest results."""
    output = "=" * 60
    output += "\nBACKTEST RESULTS\n"
    output += "=" * 60 + "\n\n"

    output += f"Total Trades: {result.total_trades}\n"
    output += f"Winning Trades: {result.winning_trades}\n"
    output += f"Losing Trades: {result.losing_trades}\n"
    output += f"Win Rate: {result.win_rate:.1f}%\n\n"

    output += f"Total PnL: ${result.total_pnl:,.2f}\n"
    output += f"Total Return: {result.total_pnl_pct:.2f}%\n\n"

    output += f"Max Drawdown: ${result.max_drawdown:,.2f} ({result.max_drawdown_pct:.2f}%)\n"
    output += f"Sharpe Ratio: {result.sharpe_ratio:.2f}\n"
    output += f"Profit Factor: {result.profit_factor:.2f}\n\n"

    output += f"Avg Win: ${result.avg_win:,.2f}\n"
    output += f"Avg Loss: ${result.avg_loss:,.2f}\n"
    output += f"Avg Holding Period: {result.avg_holding_period:.1f} hours\n"

    return output
