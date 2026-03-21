"""
Strategy Executor - Implements the full trading workflow strategy.
"""

from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass
import random

from .engine import BacktestEngine, TradeDirection
from ..analysis import indicators, volume_profile, market_context
from ..execution import position_sizer, trade_plan
from ..data.coin_tiers import is_major, normalize_symbol
from .. import config


# Trading pairs to backtest — NO HYPHEN format (Binance standard)
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT",
    "ADAUSDT", "AVAXUSDT", "DOTUSDT", "MATICUSDT", "LINKUSDT",
    "ATOMUSDT", "UNIUSDT", "LTCUSDT", "ETCUSDT", "XLMUSDT",
]

# Timeframes for multi-timeframe analysis
DEFAULT_TIMEFRAMES = ["15m", "1h", "4h"]


@dataclass
class StrategyConfig:
    """Configuration for the trading strategy."""
    # Position sizing
    base_risk_pct: float = 0.01  # 1% risk per trade
    pos_mult: float = 1.0  # Position multiplier
    max_leverage: float = 50.0
    training_mode: bool = True

    # Entry criteria
    min_rr: float = 1.5
    min_resonance: int = 2  # Minimum timeframes with aligned levels
    min_volume_profile_confidence: float = 0.6

    # Exit criteria
    use_stop_loss: bool = True
    stop_atr_multiplier: float = 2.0
    take_profit_rr: float = 2.0  # TP at 2R

    # Filters
    allow_alt_coins: bool = True
    require_btc_alignment: bool = True  # Only trade in same direction as BTC


class StrategyState:
    """Tracks strategy state for each symbol."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.analysis = None
        self.levels = None
        self.signal = None
        self.last_check_time = None
        self.position_open = False


class WorkflowStrategy:
    """
    Implements the full trading workflow strategy from TRADING_WORKFLOW.md.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        self.states: Dict[str, StrategyState] = {}
        self.btc_signal = None
        self.market_context = {
            "risk_level": "MODERATE",
            "macro_state": "risk_on",
            "btc_strength": "neutral",
        }

    def analyze_symbol(
        self,
        symbol: str,
        data: Dict[str, List[dict]],
        timestamp: int
    ) -> Optional[dict]:
        """
        Perform complete analysis on a symbol.
        """
        # Get relevant timeframes
        candles_1h = data.get("1h", [])
        candles_4h = data.get("4h", [])
        candles_15m = data.get("15m", [])

        # Need at least 200 candles for meaningful analysis
        if len(candles_1h) < 100:
            return None

        # Find candles up to current timestamp
        def get_candles_at(candles: List[dict], ts: int) -> List[dict]:
            return [c for c in candles if c["timestamp"] <= ts]

        c1h = get_candles_at(candles_1h, timestamp)
        c4h = get_candles_at(candles_4h, timestamp)
        c15m = get_candles_at(candles_15m, timestamp)

        if len(c1h) < 50:
            return None

        # Technical analysis
        closes = [c["close"] for c in c1h]
        highs = [c["high"] for c in c1h]
        lows = [c["low"] for c in c1h]
        volumes = [c["volume"] for c in c1h]

        # Calculate indicators
        try:
            ema50 = indicators.calculate_ema(closes, 50)
            ema200 = indicators.calculate_ema(closes, 200) if len(closes) >= 200 else None
            rsi = indicators.calculate_rsi(closes, 14)
        except Exception:
            return None

        # Support/Resistance
        sr = indicators.find_support_resistance(closes, highs, lows)

        # Momentum
        momentum = indicators.calculate_momentum(closes)

        # Trend
        trend = indicators.detect_trend(ema50, ema200) if ema200 else "SIDEWAYS"

        # Signal
        signal_data = indicators.get_signal(ema50, ema200, rsi)

        # Volume profile (for entry/exit levels)
        levels = volume_profile.get_key_levels(c1h)

        # Multi-timeframe resonance
        resonance = None
        if c15m and c4h:
            levels_15m = volume_profile.get_key_levels(c15m)
            levels_4h = volume_profile.get_key_levels(c4h)
            levels_1h = levels

            resonance = volume_profile.check_multi_timeframe_resonance(
                levels_15m, levels_1h, levels_4h
            )

        return {
            "symbol": symbol,
            "timestamp": timestamp,
            "current_price": closes[-1],
            "ema50": ema50,
            "ema200": ema200,
            "rsi": rsi,
            "trend": trend,
            "signal": signal_data["signal"],
            "support": sr["low"],
            "resistance": sr["high"],
            "position_in_range": sr["position_in_range"],
            "momentum": momentum,
            "levels": levels,
            "resonance": resonance,
        }

    def update_market_context(self, data: Dict[str, List[dict]], timestamp: int):
        """Update overall market context based on BTC.

        Note: data is already the BTC data (tf -> candles), not symbol -> tf
        """
        # data is already the BTC data dict like {"1h": [...], "4h": [...]}
        if data:
            btc_analysis = self.analyze_symbol("BTCUSDT", data, timestamp)
            if btc_analysis:
                self.btc_signal = btc_analysis["signal"]
                self.market_context["btc_strength"] = market_context.assess_btc_strength(btc_analysis)

                if btc_analysis["trend"] == "DOWNTREND":
                    self.market_context["macro_state"] = "risk_off"
                else:
                    self.market_context["macro_state"] = "risk_on"

    def check_entry(
        self,
        symbol: str,
        analysis: dict,
        equity: float
    ) -> Optional[dict]:
        """
        Check if entry criteria are met.
        """
        # Skip if already have position
        engine = getattr(self, 'engine', None)
        if engine is not None and symbol in engine.open_trades:
            return None

        # Check if alt coin and BTC is weakening
        if not is_major(symbol) and self.config.require_btc_alignment:
            if self.market_context["macro_state"] == "risk_off":
                if self.market_context["btc_strength"] in ["weakness", "neutral"]:
                    return None  # No alt longs in risk-off

        # Check resonance
        resonance = analysis.get("resonance")
        if not resonance or resonance["resonance_strength"] == "NONE":
            return None

        # Determine direction
        direction = None
        if analysis["trend"] == "UPTREND" and analysis["current_price"] < analysis["resistance"]:
            direction = TradeDirection.LONG
        elif analysis["trend"] == "DOWNTREND" and analysis["current_price"] > analysis["support"]:
            direction = TradeDirection.SHORT
        else:
            return None

        # Calculate entry, stop, target
        levels = analysis.get("levels", {})
        current = analysis["current_price"]

        if direction == TradeDirection.LONG:
            # Entry at LVN or current price
            entry = current
            # Stop below support or recent low
            stop = min(analysis["support"] * 0.99, current * 0.98)
            # Target at HVN or resistance
            target = analysis["resistance"] * 1.02
        else:
            # Short
            entry = current
            stop = max(analysis["resistance"] * 1.01, current * 1.02)
            target = analysis["support"] * 0.98

        # Calculate stop distance
        stop_distance = abs(entry - stop) / entry

        # Check minimum stop distance
        if stop_distance < 0.005:  # Too small
            return None

        # Calculate position size
        coin_type = "major" if is_major(symbol) else "small"
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

        # Check RR
        reward = abs(target - entry)
        risk = abs(entry - stop)
        rr = reward / risk

        if rr < self.config.min_rr:
            return None

        # Cap leverage
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
        """
        Execute strategy for a symbol at given timestamp.
        """
        self.engine = engine

        # Normalize symbol to standard format
        symbol = normalize_symbol(symbol)

        # Update market context from BTC
        if symbol == "BTCUSDT":
            self.update_market_context(data, ts_int)

        # Analyze symbol
        analysis = self.analyze_symbol(symbol, data, ts_int)
        if not analysis:
            return

        # Update state
        if symbol not in self.states:
            self.states[symbol] = StrategyState(symbol)

        state = self.states[symbol]
        state.analysis = analysis
        state.last_check_time = timestamp

        # Check for open position
        if symbol in engine.open_trades:
            trade = engine.open_trades[symbol]

            # Check stop/take
            engine.check_stop_take(symbol, analysis["current_price"], timestamp)

            # Check for manual exit signals
            if analysis["signal"] == "SELL" and trade.direction == TradeDirection.LONG:
                engine.close_trade(symbol, analysis["current_price"], timestamp, "SIGNAL_SELL")
            elif analysis["signal"] == "BUY" and trade.direction == TradeDirection.SHORT:
                engine.close_trade(symbol, analysis["current_price"], timestamp, "SIGNAL_BUY")

        else:
            # Check for entry
            entry_setup = self.check_entry(symbol, analysis, engine.equity)
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
    market: str = "futures",
    use_llm: bool = False,
    llm_provider: str = "minimax",
) -> dict:
    """
    Run complete backtest.

    Args:
        symbols: List of trading symbols
        timeframes: List of timeframes to use
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        initial_equity: Starting equity
        config: Strategy configuration
        market: 'futures' (default) or 'spot'
    """
    from .engine import MultiCoinBacktester, format_results

    if symbols is None:
        symbols = DEFAULT_SYMBOLS
    if timeframes is None:
        timeframes = DEFAULT_TIMEFRAMES

    # Default to 12 months
    if start_date is None:
        from datetime import timedelta
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=365)
        start_date = start_dt.strftime("%Y-%m-%d")
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    # Create engine and strategy
    engine = BacktestEngine(
        initial_equity=initial_equity,
    )

    # Plutus V2: Hybrid strategy when --use-llm, pure-rule otherwise
    if use_llm:
        from .hybrid_strategy import HybridWorkflowStrategy
        strategy = HybridWorkflowStrategy(
            config=config or StrategyConfig(),
            use_llm=True,
            llm_provider=llm_provider,
            llm_cache_seconds=3600,
        )
    else:
        strategy = WorkflowStrategy(config or StrategyConfig())

    # Create backtester with market preference
    backtester = MultiCoinBacktester(engine)

    # Run
    result = backtester.run(
        symbols=symbols,
        timeframes=timeframes,
        strategy_fn=strategy.execute,
        start_date=start_date,
        end_date=end_date,
        market=market,
    )

    # Format results
    output = format_results(result)

    return {
        "result": result,
        "output": output,
    }
