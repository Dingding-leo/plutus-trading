"""
Full Workflow Strategy - Following TRADING_WORKFLOW.md exactly.
"""

from datetime import datetime
from typing import Dict, List, Optional
from enum import Enum
from .engine import BacktestEngine, TradeDirection
from ..analysis import indicators, volume_profile
from ..execution import position_sizer
from .. import config


class Phase(Enum):
    """Three phases of market movement."""
    NO_MOVEMENT = "未动"
    SHOCK = "冲击"
    CONFIRMATION = "确认"


class WorkflowStrategy:
    """
    Full implementation of TRADING_WORKFLOW.md strategy.
    """

    def __init__(
        self,
        risk_pct: float = 0.01,
        max_leverage: float = 30,
        pos_mult: float = 1.0,
    ):
        self.risk_pct = risk_pct
        self.max_leverage = max_leverage
        self.pos_mult = pos_mult

        # Track last signals for each symbol
        self.last_signal: Dict[str, str] = {}
        self.btc_trend = "SIDEWAYS"

    def analyze(
        self,
        candles_1h: List[dict],
        candles_4h: List[dict] = None,
    ) -> Optional[dict]:
        """Full technical analysis."""
        if len(candles_1h) < 50:
            return None

        closes = [c["close"] for c in candles_1h]
        highs = [c["high"] for c in candles_1h]
        lows = [c["low"] for c in candles_1h]

        current = closes[-1]

        # EMAs
        try:
            ema50 = indicators.calculate_ema(closes, 50)
            ema200 = indicators.calculate_ema(closes, 200)
        except:
            return None

        # Trend
        trend = indicators.detect_trend(ema50, ema200)

        # RSI
        try:
            rsi = indicators.calculate_rsi(closes, 14)
        except:
            rsi = 50

        # Support/Resistance
        sr = indicators.find_support_resistance(closes, highs, lows)

        # Momentum
        momentum = indicators.calculate_momentum(closes)

        # Signal
        signal_data = indicators.get_signal(ema50, ema200, rsi)

        return {
            "trend": trend,
            "signal": signal_data["signal"],
            "rsi": rsi,
            "current": current,
            "ema50": ema50,
            "ema200": ema200,
            "support": sr["low"],
            "resistance": sr["high"],
            "momentum": momentum,
            "position_in_range": sr["position_in_range"],
        }

    def check_entry(
        self,
        analysis: dict,
        equity: float,
        symbol: str,
    ) -> Optional[dict]:
        """Check if entry criteria met."""
        if not analysis:
            return None

        current = analysis["current"]
        trend = analysis["trend"]
        signal = analysis["signal"]

        # Determine direction based on trend
        if trend == "UPTREND":
            direction = TradeDirection.LONG
            entry = current
            stop = analysis["support"] * 0.99  # Below support
            target = analysis["resistance"]  # To resistance
        elif trend == "DOWNTREND":
            direction = TradeDirection.SHORT
            entry = current
            stop = analysis["resistance"] * 1.01  # Above resistance
            target = analysis["support"]  # To support
        else:
            return None

        # Calculate stop distance
        stop_distance = abs(entry - stop) / entry

        if stop_distance < 0.005:  # Too tight
            return None

        # Position sizing
        coin_type = "major" if symbol in ["BTC-USDT", "ETH-USDT"] else "small"
        position = position_sizer.calculate_position_size(
            equity=equity,
            risk_pct=self.risk_pct,
            stop_distance=stop_distance,
            pos_mult=self.pos_mult,
            coin_type=coin_type,
            training_mode=True,
        )

        if not position["valid"]:
            return None

        # RR check (minimum 1.5R)
        reward = abs(target - entry)
        risk = abs(entry - stop)
        rr = reward / risk

        if rr < 1.5:
            return None

        # Leverage
        leverage = min(position["recommended_leverage"], self.max_leverage)

        return {
            "direction": direction,
            "entry": entry,
            "stop": stop,
            "target": target,
            "size": position["max_position"] / entry,
            "leverage": leverage,
            "rr": rr,
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
        candles_4h = data.get("4h", [])

        if len(candles_1h) < 50:
            return

        # Analyze
        analysis = self.analyze(candles_1h, candles_4h if candles_4h else None)

        if not analysis:
            return

        # Track BTC trend
        if symbol == "BTC-USDT":
            self.btc_trend = analysis["trend"]

        # Check for open position
        if symbol in engine.open_trades:
            trade = engine.open_trades[symbol]

            # Check stop/take
            engine.check_stop_take(symbol, analysis["current"], timestamp)

            # Exit on trend reversal
            if analysis["trend"] == "DOWNTREND" and trade.direction == TradeDirection.LONG:
                engine.close_trade(symbol, analysis["current"], timestamp, "TREND_REVERSAL")
            elif analysis["trend"] == "UPTREND" and trade.direction == TradeDirection.SHORT:
                engine.close_trade(symbol, analysis["current"], timestamp, "TREND_REVERSAL")

            return

        # Check entry
        setup = self.check_entry(analysis, engine.equity, symbol)

        if setup:
            engine.open_trade(
                symbol=symbol,
                direction=setup["direction"],
                entry_price=setup["entry"],
                size=setup["size"],
                leverage=setup["leverage"],
                stop_loss=setup["stop"],
                take_profit=setup["target"],
                timestamp=timestamp
            )


def run_workflow_backtest(
    symbols: List[str] = None,
    start_date: str = None,
    end_date: str = None,
    initial_equity: float = 10000,
    **kwargs
) -> dict:
    """Run TRADING_WORKFLOW.md strategy backtest."""
    from .time_based import run_proper_backtest
    from datetime import timedelta

    if symbols is None:
        symbols = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]

    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    strategy = WorkflowStrategy(**kwargs)

    result = run_proper_backtest(
        symbols=symbols,
        strategy_fn=strategy.execute,
        start_date=start_date,
        end_date=end_date,
        initial_equity=initial_equity,
    )

    return result
