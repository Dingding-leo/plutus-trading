"""
Aggressive Strategy - Optimized for high returns.
"""

from datetime import datetime
from typing import Dict, List
from .engine import BacktestEngine, TradeDirection
from ..analysis import indicators
from ..execution import position_sizer


class AggressiveStrategy:
    """
    Aggressive strategy optimized for high returns.
    """

    def __init__(
        self,
        risk_pct: float = 0.05,
        max_leverage: float = 100,
        ema_fast: int = 20,
        ema_slow: int = 50,
        rsi_period: int = 14,
        rsi_long_max: float = 60,
        rsi_short_min: float = 40,
        stop_pct: float = 0.015,
        tp_multiple: float = 3.0,
    ):
        self.risk_pct = risk_pct
        self.max_leverage = max_leverage
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.rsi_long_max = rsi_long_max
        self.rsi_short_min = rsi_short_min
        self.stop_pct = stop_pct
        self.tp_multiple = tp_multiple

    def analyze(self, candles: List[dict]) -> dict:
        """Quick analysis."""
        if len(candles) < 50:
            return None

        closes = [c["close"] for c in candles]
        current = closes[-1]

        # EMAs
        try:
            ema_fast = indicators.calculate_ema(closes, self.ema_fast)
            ema_slow = indicators.calculate_ema(closes, self.ema_slow)
            rsi = indicators.calculate_rsi(closes, self.rsi_period)
        except Exception:
            return None

        # Trend
        if ema_fast > ema_slow:
            trend = "UP"
        elif ema_fast < ema_slow:
            trend = "DOWN"
        else:
            trend = "SIDEWAYS"

        return {
            "trend": trend,
            "rsi": rsi,
            "current": current,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
        }

    def execute(
        self,
        engine: BacktestEngine,
        symbol: str,
        data: Dict[str, List[dict]],
        timestamp: datetime,
        ts_int: int
    ):
        """Execute strategy."""
        candles = data.get("1h", [])
        if len(candles) < 50:
            return

        analysis = self.analyze(candles)
        if not analysis:
            return

        current = analysis["current"]

        # Check position
        if symbol in engine.open_trades:
            trade = engine.open_trades[symbol]
            engine.check_stop_take(symbol, current, timestamp)

            # Exit on reversal
            if analysis["trend"] == "DOWN" and trade.direction == TradeDirection.LONG:
                engine.close_trade(symbol, current, timestamp, "REVERSAL")
            elif analysis["trend"] == "UP" and trade.direction == TradeDirection.SHORT:
                engine.close_trade(symbol, current, timestamp, "REVERSAL")
            return

        # Entry signals
        direction = None
        if analysis["trend"] == "UP" and analysis["rsi"] < self.rsi_long_max:
            direction = TradeDirection.LONG
        elif analysis["trend"] == "DOWN" and analysis["rsi"] > self.rsi_short_min:
            direction = TradeDirection.SHORT

        if not direction:
            return

        stop_pct = self.stop_pct
        tp_multiple = self.tp_multiple

        if direction == TradeDirection.LONG:
            entry = current
            stop = current * (1 - stop_pct)
            target = current * (1 + stop_pct * tp_multiple)
        else:
            entry = current
            stop = current * (1 + stop_pct)
            target = current * (1 - stop_pct * tp_multiple)

        # Position sizing
        coin_type = "major" if symbol in ["BTC-USDT", "ETH-USDT"] else "small"
        position = position_sizer.calculate_position_size(
            equity=engine.equity,
            risk_pct=self.risk_pct,
            stop_distance=stop_pct,
            pos_mult=1.0,
            coin_type=coin_type,
            training_mode=False,
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


def run_aggressive_backtest(
    symbols=None,
    start_date='2025-09-01',
    end_date='2026-03-02',
    initial_equity=10000,
    **kwargs
):
    """Run aggressive backtest."""
    from .simple_fetch import fetch_binance_history
    from .engine import format_results

    if symbols is None:
        symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']

    # Fetch data
    data = {}
    for sym in symbols:
        print(f'Fetching {sym}...')
        data[sym] = fetch_binance_history(sym, '1h', start_date, end_date, 5000)

    engine = BacktestEngine(initial_equity)
    strategy = AggressiveStrategy(**kwargs)

    # Find min length
    min_len = min(len(data[s]) for s in symbols)

    # Run backtest
    for i in range(50, min_len):
        ts = data[symbols[0]][i]['timestamp']
        current_time = data[symbols[0]][i]['datetime']

        for sym in symbols:
            strategy.execute(engine, sym.replace('USDT', '-USDT'), {'1h': data[sym][:i+1]}, current_time, ts)

    result = engine.get_results()
    return format_results(result)
