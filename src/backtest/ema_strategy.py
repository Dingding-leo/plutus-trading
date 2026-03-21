"""
EMA Crossover Strategy - Simple and effective.
"""

from datetime import datetime
from typing import Dict, List
from .engine import BacktestEngine, TradeDirection
from ..analysis import indicators
from ..execution import position_sizer


class EMACrossoverStrategy:
    """Simple EMA crossover strategy."""

    def __init__(
        self,
        fast_period: int = 20,
        slow_period: int = 50,
        risk_pct: float = 0.02,
        max_leverage: float = 20,
        stop_pct: float = 0.03,
    ):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.risk_pct = risk_pct
        self.max_leverage = max_leverage
        self.stop_pct = stop_pct
        self.positions: Dict[str, dict] = {}

    def execute(
        self,
        engine: BacktestEngine,
        symbol: str,
        data: Dict[str, List[dict]],
        timestamp: datetime,
        ts_int: int
    ):
        """Execute strategy."""
        candles_1h = data.get("1h", [])

        if len(candles_1h) < self.slow_period + 10:
            return

        closes = [c["close"] for c in candles_1h]
        current = closes[-1]

        # Calculate EMAs
        try:
            ema_fast = indicators.calculate_ema(closes, self.fast_period)
            ema_slow = indicators.calculate_ema(closes, self.slow_period)

            # Previous EMA for crossover detection
            ema_fast_prev = indicators.calculate_ema(closes[:-1], self.fast_period)
            ema_slow_prev = indicators.calculate_ema(closes[:-1], self.slow_period)
        except Exception:
            return

        # Determine direction
        direction = None
        signal = None

        # Golden Cross (fast crosses above slow)
        if ema_fast_prev <= ema_slow_prev and ema_fast > ema_slow:
            direction = TradeDirection.LONG
            signal = "GOLDEN_CROSS"
        # Death Cross (fast crosses below slow)
        elif ema_fast_prev >= ema_slow_prev and ema_fast < ema_slow:
            direction = TradeDirection.SHORT
            signal = "DEATH_CROSS"

        if not direction:
            return

        # Check if we have a position
        if symbol in engine.open_trades:
            trade = engine.open_trades[symbol]

            # Check stop/take
            engine.check_stop_take(symbol, current, timestamp)

            # Exit on opposite signal
            if signal == "DEATH_CROSS" and trade.direction == TradeDirection.LONG:
                engine.close_trade(symbol, current, timestamp, "EXIT_DEATH_CROSS")
            elif signal == "GOLDEN_CROSS" and trade.direction == TradeDirection.SHORT:
                engine.close_trade(symbol, current, timestamp, "EXIT_GOLDEN_CROSS")
            return

        # Entry
        if direction == TradeDirection.LONG:
            entry = current
            stop = current * (1 - self.stop_pct)
            target = current * (1 + self.stop_pct * 2)  # 2R target
        else:
            entry = current
            stop = current * (1 + self.stop_pct)
            target = current * (1 - self.stop_pct * 2)

        # Position sizing
        coin_type = "major" if symbol in ["BTC-USDT", "ETH-USDT"] else "small"
        position = position_sizer.calculate_position_size(
            equity=engine.equity,
            risk_pct=self.risk_pct,
            stop_distance=self.stop_pct,
            pos_mult=1.0,
            coin_type=coin_type,
            training_mode=True,
        )

        if not position["valid"]:
            return

        size = position["max_position"] / entry
        leverage = min(position["recommended_leverage"], self.max_leverage)

        engine.open_trade(
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            size=size,
            leverage=leverage,
            stop_loss=stop,
            take_profit=target,
            timestamp=timestamp
        )


def run_ema_backtest(
    symbols: list = None,
    start_date: str = None,
    end_date: str = None,
    initial_equity: float = 10000,
    **kwargs
) -> dict:
    """Run EMA crossover backtest."""
    from .engine import MultiCoinBacktester, format_results

    if symbols is None:
        symbols = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]

    if start_date is None:
        from datetime import timedelta
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=180)
        start_date = start_dt.strftime("%Y-%m-%d")
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    engine = BacktestEngine(initial_equity=initial_equity)
    strategy = EMACrossoverStrategy(**kwargs)
    backtester = MultiCoinBacktester(engine)

    result = backtester.run(
        symbols=symbols,
        timeframes=["1h"],
        strategy_fn=strategy.execute,
        start_date=start_date,
        end_date=end_date,
    )

    return {
        "result": result,
        "output": format_results(result),
    }
