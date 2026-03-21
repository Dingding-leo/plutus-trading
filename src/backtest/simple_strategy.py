"""
Simplified Strategy - More aggressive version to generate trades.
"""

from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass

from .engine import BacktestEngine, TradeDirection
from ..analysis import indicators, volume_profile, market_context
from ..execution import position_sizer, trade_plan
from .. import config


DEFAULT_SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "DOGE-USDT", "XRP-USDT",
    "ADA-USDT", "AVAX-USDT", "DOT-USDT", "MATIC-USDT", "LINK-USDT",
]

DEFAULT_TIMEFRAMES = ["15m", "1h", "4h"]


@dataclass
class SimpleStrategyConfig:
    """Simplified strategy config."""
    base_risk_pct: float = 0.02
    pos_mult: float = 1.0
    max_leverage: float = 20.0
    min_rr: float = 1.0
    stop_pct: float = 0.02  # 2% stop


class SimpleStrategy:
    """
    Simplified strategy - trades based on trend and volume profile.
    """

    def __init__(self, config: SimpleStrategyConfig = None):
        self.config = config or SimpleStrategyConfig()
        self.btc_trend = "SIDEWAYS"

    def analyze(self, candles: List[dict]) -> Optional[dict]:
        """Analyze candles and return signals."""
        if len(candles) < 50:
            return None

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]

        current = closes[-1]

        # Trend (EMA crossover)
        try:
            ema20 = indicators.calculate_ema(closes, 20)
            ema50 = indicators.calculate_ema(closes, 50)
        except Exception:
            ema20 = current
            ema50 = current

        # RSI
        try:
            rsi = indicators.calculate_rsi(closes, 14)
        except Exception:
            rsi = 50

        # Support/Resistance
        sr = indicators.find_support_resistance(closes, highs, lows)

        # Determine trend
        if ema20 > ema50 * 1.01:
            trend = "UPTREND"
        elif ema20 < ema50 * 0.99:
            trend = "DOWNTREND"
        else:
            trend = "SIDEWAYS"

        # Signals
        signal = "NEUTRAL"
        direction = None

        if trend == "UPTREND" and rsi < 65:
            signal = "BUY"
            direction = TradeDirection.LONG
        elif trend == "DOWNTREND" and rsi > 35:
            signal = "SELL"
            direction = TradeDirection.SHORT

        return {
            "trend": trend,
            "rsi": rsi,
            "signal": signal,
            "direction": direction,
            "current": current,
            "ema20": ema20,
            "ema50": ema50,
            "support": sr["low"],
            "resistance": sr["high"],
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
        # Get data
        candles_1h = data.get("1h", [])
        candles_15m = data.get("15m", [])

        if len(candles_1h) < 50:
            return

        # Use 1h for trend
        analysis = self.analyze(candles_1h)
        if not analysis:
            return

        # Update BTC trend for context
        if symbol == "BTC-USDT":
            self.btc_trend = analysis["trend"]

        # Check for open position
        if symbol in engine.open_trades:
            trade = engine.open_trades[symbol]

            # Check stop/take
            engine.check_stop_take(symbol, analysis["current"], timestamp)

            # Exit on trend reversal
            if analysis["trend"] == "UPTREND" and trade.direction == TradeDirection.SHORT:
                engine.close_trade(symbol, analysis["current"], timestamp, "TREND_REVERSAL")
            elif analysis["trend"] == "DOWNTREND" and trade.direction == TradeDirection.LONG:
                engine.close_trade(symbol, analysis["current"], timestamp, "TREND_REVERSAL")
            return

        # Entry logic
        if not analysis["direction"]:
            return

        current = analysis["current"]

        if analysis["direction"] == TradeDirection.LONG:
            entry = current
            stop = current * (1 - self.config.stop_pct)
            target = current * (1 + self.config.stop_pct * self.config.min_rr)
        else:
            entry = current
            stop = current * (1 + self.config.stop_pct)
            target = current * (1 - self.config.stop_pct * self.config.min_rr)

        # Position sizing
        coin_type = "major" if symbol in ["BTC-USDT", "ETH-USDT"] else "small"
        position = position_sizer.calculate_position_size(
            equity=engine.equity,
            risk_pct=self.config.base_risk_pct,
            stop_distance=self.config.stop_pct,
            pos_mult=self.config.pos_mult,
            coin_type=coin_type,
            training_mode=True,
        )

        if not position["valid"]:
            return

        # Check RR
        reward = abs(target - entry)
        risk = abs(entry - stop)
        rr = reward / risk

        if rr < self.config.min_rr:
            return

        # Open trade
        size = position["max_position"] / entry
        leverage = min(position["recommended_leverage"], self.config.max_leverage)

        engine.open_trade(
            symbol=symbol,
            direction=analysis["direction"],
            entry_price=entry,
            size=size,
            leverage=leverage,
            stop_loss=stop,
            take_profit=target,
            timestamp=timestamp
        )


def run_simple_backtest(
    symbols: List[str] = None,
    timeframes: List[str] = None,
    start_date: str = None,
    end_date: str = None,
    initial_equity: float = 10000,
    config: SimpleStrategyConfig = None,
) -> dict:
    """Run simple backtest."""
    from .engine import MultiCoinBacktester, format_results
    from .data_client import data_client

    if symbols is None:
        symbols = DEFAULT_SYMBOLS
    if timeframes is None:
        timeframes = DEFAULT_TIMEFRAMES

    if start_date is None:
        from datetime import timedelta
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=365)
        start_date = start_dt.strftime("%Y-%m-%d")
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    engine = BacktestEngine(initial_equity=initial_equity)
    strategy = SimpleStrategy(config or SimpleStrategyConfig())
    backtester = MultiCoinBacktester(engine)

    result = backtester.run(
        symbols=symbols,
        timeframes=timeframes,
        strategy_fn=strategy.execute,
        start_date=start_date,
        end_date=end_date,
    )

    return {
        "result": result,
        "output": format_results(result),
    }
