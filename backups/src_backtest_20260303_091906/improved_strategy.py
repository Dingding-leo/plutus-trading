"""
Strategy Executor - Improved version with proper entry logic.
"""

from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass

from .engine import BacktestEngine, TradeDirection
from ..analysis import indicators, volume_profile
from ..execution import position_sizer
from .. import config


DEFAULT_SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "DOGE-USDT", "XRP-USDT",
    "ADA-USDT", "AVAX-USDT", "DOT-USDT", "MATIC-USDT", "LINK-USDT",
    "ATOM-USDT", "UNI-USDT", "LTC-USDT", "ETC-USDT", "XLM-USDT",
]

DEFAULT_TIMEFRAMES = ["15m", "1h", "4h"]


@dataclass
class StrategyConfig:
    base_risk_pct: float = 0.01
    pos_mult: float = 1.0
    max_leverage: float = 50.0
    training_mode: bool = True
    min_rr: float = 1.5
    min_resonance: int = 2
    allow_alt_coins: bool = True
    require_btc_alignment: bool = True


class StrategyState:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.analysis = None
        self.last_price = None
        self.last_trend = None
        self.last_exit_time = None  # Cooldown after trade
        self.cooldown_hours = 24  # Wait 24 hours before re-entry
        self.position_open = False


class ImprovedStrategy:
    """
    Improved strategy with proper entry logic:
    1. Trend detection using EMA + price momentum
    2. Entry only at key levels (LVN/HVN)
    3. Confirmation of breakout/breakdown
    4. Proper stop loss at technical levels
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        self.states: Dict[str, StrategyState] = {}
        self.btc_trend = "SIDEWAYS"
        self.market_context = {
            "risk_level": "MODERATE",
            "macro_state": "risk_on",
            "btc_strength": "neutral",
        }

    def get_trend_with_momentum(self, candles: List[dict]) -> str:
        """Get trend with momentum confirmation."""
        if len(candles) < 200:
            return "SIDEWAYS"

        closes = [c["close"] for c in candles]
        ema50 = indicators.calculate_ema(closes, 50)
        ema200 = indicators.calculate_ema(closes, 200) if len(closes) >= 200 else None

        if not ema200:
            return "SIDEWAYS"

        current_price = closes[-1]

        # EMA gap for trend strength
        ema_gap_pct = abs(ema50 - ema200) / ema200 * 100

        # Strong uptrend: EMA50 > EMA200 AND price above EMA50
        if ema50 > ema200 and current_price > ema50:
            if ema_gap_pct >= 0.5:  # At least 0.5% separation
                return "UPTREND"
            else:
                return "UPTREND_WEAK"

        # Strong downtrend: EMA50 < EMA200 AND price below EMA50
        elif ema50 < ema200 and current_price < ema50:
            if ema_gap_pct >= 0.5:
                return "DOWNTREND"
            else:
                return "DOWNTREND_WEAK"

        # Weak: EMA aligned but price not confirmed
        elif ema50 > ema200:
            return "UPTREND_WEAK"

        elif ema50 < ema200:
            return "DOWNTREND_WEAK"

        else:
            return "SIDEWAYS"

    def get_dynamic_levels(self, candles: List[dict]) -> dict:
        """Get dynamic support/resistance based on recent price action."""
        if len(candles) < 20:
            return {"support": None, "resistance": None}

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]

        # Recent lows (last 20 candles)
        recent_low = min(lows[-20:])
        recent_high = max(highs[-20:])

        # Fibonacci retracement levels for dynamic S/R
        range_size = recent_high - recent_low
        support = recent_low + range_size * 0.382  # 38.2% retracement
        resistance = recent_high - range_size * 0.382

        return {
            "support": support,
            "resistance": resistance,
            "recent_low": recent_low,
            "recent_high": recent_high,
        }

    def analyze_symbol(
        self,
        symbol: str,
        data: Dict[str, List[dict]],
        timestamp: int
    ) -> Optional[dict]:
        """Perform complete analysis on a symbol."""
        candles_1h = data.get("1h", [])
        candles_4h = data.get("4h", [])
        candles_15m = data.get("15m", [])

        if len(candles_1h) < 100:
            return None

        def get_candles_at(candles: List[dict], ts: int) -> List[dict]:
            return [c for c in candles if c["timestamp"] <= ts]

        c1h = get_candles_at(candles_1h, timestamp)
        c4h = get_candles_at(candles_4h, timestamp)
        c15m = get_candles_at(candles_15m, timestamp)

        if len(c1h) < 50:
            return None

        closes = [c["close"] for c in c1h]
        highs = [c["high"] for c in c1h]
        lows = [c["low"] for c in c1h]

        current_price = closes[-1]

        # Get trend with momentum
        trend = self.get_trend_with_momentum(c1h)

        # Technical indicators
        ema50 = indicators.calculate_ema(closes, 50)
        ema200 = indicators.calculate_ema(closes, 200) if len(closes) >= 200 else None
        rsi = indicators.calculate_rsi(closes, 14)

        # Support/Resistance - use dynamic levels
        sr = self.get_dynamic_levels(c1h)
        if sr["support"] is None:
            sr = indicators.find_support_resistance(closes, highs, lows)

        # Momentum
        momentum = indicators.calculate_momentum(closes)

        # Volume profile for key levels
        levels = volume_profile.get_key_levels(c1h)

        # Check for breakout/breakdown in last 5 candles
        breakout = None
        if len(closes) >= 5:
            recent_high = max(closes[-5:])
            recent_low = min(closes[-5:])
            if current_price > recent_high * 1.01:
                breakout = "UP"
            elif current_price < recent_low * 0.99:
                breakout = "DOWN"

        return {
            "symbol": symbol,
            "timestamp": timestamp,
            "current_price": current_price,
            "ema50": ema50,
            "ema200": ema200,
            "rsi": rsi,
            "trend": trend,
            "support": sr.get("support", sr.get("low")),
            "resistance": sr.get("resistance", sr.get("high")),
            "recent_low": sr.get("recent_low"),
            "recent_high": sr.get("recent_high"),
            "position_in_range": sr.get("position_in_range", 50),
            "momentum": momentum,
            "levels": levels,
            "breakout": breakout,
            "candles": c1h,
        }

    def check_entry(
        self,
        symbol: str,
        analysis: dict,
        equity: float,
        timestamp: datetime = None
    ) -> Optional[dict]:
        """Check if entry criteria are met - strong trends with confirmation."""
        if hasattr(self, 'engine') and symbol in self.engine.open_trades:
            return None

        # Check cooldown
        state = self.states.get(symbol)
        if state and state.last_exit_time and timestamp:
            hours_since_exit = (timestamp - state.last_exit_time).total_seconds() / 3600
            if hours_since_exit < state.cooldown_hours:
                return None

        # Check BTC alignment for alts
        if symbol != "BTC-USDT" and self.config.require_btc_alignment:
            if self.market_context["macro_state"] == "risk_off":
                if self.market_context["btc_strength"] in ["weakness", "neutral"]:
                    return None

        trend = analysis["trend"]
        current = analysis["current_price"]
        support = analysis["support"]
        resistance = analysis["resistance"]
        breakout = analysis.get("breakout")

        # Allow entries in strong trends
        if trend not in ["UPTREND", "DOWNTREND"]:
            return None

        is_near_support = current < support * 1.10  # 10% tolerance
        is_near_resistance = current > resistance * 0.90  # 10% tolerance

        direction = None

        # LONG: Uptrend + near support
        if trend == "UPTREND" and is_near_support:
            direction = TradeDirection.LONG

        # SHORT: Downtrend + near resistance
        elif trend == "DOWNTREND" and is_near_resistance:
            direction = TradeDirection.SHORT

        if not direction:
            return None

        # SHORT: Reversal from breakout
        elif trend in ["UPTREND_WEAK", "SIDEWAYS"] and is_near_resistance:
            if breakout == "DOWN" or current < analysis["ema50"]:
                direction = TradeDirection.SHORT

        if not direction:
            return None

        # Calculate entry, stop, target
        if direction == TradeDirection.LONG:
            entry = current
            stop = support * 0.99
            target = resistance
        else:
            entry = current
            stop = resistance * 1.01
            target = support

        stop_distance = abs(entry - stop) / entry

        if stop_distance < 0.008:
            return None

        coin_type = "major" if symbol in ["BTC-USDT", "ETH-USDT"] else "small"
        position = position_sizer.calculate_position_size(
            equity=equity,
            risk_pct=self.config.base_risk_pct,
            stop_distance=stop_distance,
            pos_mult=self.config.pos_mult,
            coin_type=coin_type,
            training_mode=self.config.training_mode,
        )

        if not position["valid"]:
            return None

        reward = abs(target - entry)
        risk = abs(entry - stop)
        rr = reward / risk

        if rr < self.config.min_rr:
            return None

        leverage = min(
            position["recommended_leverage"],
            self.config.max_leverage
        )

        return {
            "direction": direction,
            "entry": entry,
            "stop": stop,
            "target": target,
            "size": position["max_position"] / entry,
            "leverage": leverage,
            "rr": rr,
            "stop_distance": stop_distance,
        }

    def execute(
        self,
        engine: BacktestEngine,
        symbol: str,
        data: Dict[str, List[dict]],
        timestamp: datetime,
        ts_int: int
    ):
        """Execute strategy for a symbol at given timestamp."""
        self.engine = engine

        # Update BTC context
        if symbol == "BTC-USDT":
            btc_analysis = self.analyze_symbol("BTC-USDT", data, ts_int)
            if btc_analysis:
                self.btc_trend = btc_analysis["trend"]
                self.market_context["btc_strength"] = "strength" if "UPTREND" in btc_analysis["trend"] else "weakness"

                if btc_analysis["trend"] == "DOWNTREND":
                    self.market_context["macro_state"] = "risk_off"
                else:
                    self.market_context["macro_state"] = "risk_on"

        # Analyze symbol
        analysis = self.analyze_symbol(symbol, data, ts_int)
        if not analysis:
            return

        # Update state
        if symbol not in self.states:
            self.states[symbol] = StrategyState(symbol)

        state = self.states[symbol]
        state.analysis = analysis
        state.last_price = analysis["current_price"]

        # Check for open position
        if symbol in engine.open_trades:
            trade = engine.open_trades[symbol]

            engine.check_stop_take(symbol, analysis["current_price"], timestamp)

            # Check if trade was closed
            if symbol not in engine.open_trades:
                state.last_exit_time = timestamp

            # Signal-based exits
            if analysis["current_price"] > analysis["ema50"] * 1.02 and trade.direction == TradeDirection.SHORT:
                engine.close_trade(symbol, analysis["current_price"], timestamp, "TREND_REVERSAL")
            elif analysis["current_price"] < analysis["ema50"] * 0.98 and trade.direction == TradeDirection.LONG:
                engine.close_trade(symbol, analysis["current_price"], timestamp, "TREND_REVERSAL")

            # Check again if closed
            if symbol not in engine.open_trades:
                state.last_exit_time = timestamp

        else:
            entry_setup = self.check_entry(symbol, analysis, engine.equity, timestamp)
            if entry_setup:
                engine.open_trade(
                    symbol=symbol,
                    direction=entry_setup["direction"],
                    entry_price=entry_setup["entry"],
                    size=entry_setup["size"],
                    leverage=entry_setup["leverage"],
                    stop_loss=entry_setup["stop"],
                    take_profit=entry_setup["target"],
                    timestamp=timestamp
                )


def run_backtest(
    symbols: List[str] = None,
    timeframes: List[str] = None,
    start_date: str = None,
    end_date: str = None,
    initial_equity: float = 10000,
    config: StrategyConfig = None,
) -> dict:
    """Run complete backtest."""
    from .engine import MultiCoinBacktester, format_results

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
    strategy = ImprovedStrategy(config or StrategyConfig())
    backtester = MultiCoinBacktester(engine)

    result = backtester.run(
        symbols=symbols,
        timeframes=timeframes,
        strategy_fn=strategy.execute,
        start_date=start_date,
        end_date=end_date,
    )

    output = format_results(result)

    return {
        "result": result,
        "output": output,
    }
