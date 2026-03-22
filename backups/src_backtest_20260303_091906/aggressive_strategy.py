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
    ):
        self.risk_pct = risk_pct
        self.max_leverage = max_leverage

    def analyze(self, candles: List[dict]) -> dict:
        """Quick analysis."""
        if len(candles) < 50:
            return None

        closes = [c["close"] for c in candles]
        current = closes[-1]

        # EMAs
        try:
            ema20 = indicators.calculate_ema(closes, 20)
            ema50 = indicators.calculate_ema(closes, 50)
            rsi = indicators.calculate_rsi(closes, 14)
        except:
            return None

        # Trend
        if ema20 > ema50:
            trend = "UP"
        elif ema20 < ema50:
            trend = "DOWN"
        else:
            trend = "SIDEWAYS"

        return {
            "trend": trend,
            "rsi": rsi,
            "current": current,
            "ema20": ema20,
            "ema50": ema50,
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
        if analysis["trend"] == "UP" and analysis["rsi"] < 60:
            direction = TradeDirection.LONG
        elif analysis["trend"] == "DOWN" and analysis["rsi"] > 40:
            direction = TradeDirection.SHORT

        if not direction:
            return

        # Stop at 1.5%
        stop_pct = 0.015

        if direction == TradeDirection.LONG:
            entry = current
            stop = current * (1 - stop_pct)
            target = current * (1 + stop_pct * 3)
        else:
            entry = current
            stop = current * (1 + stop_pct)
            target = current * (1 - stop_pct * 3)

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
