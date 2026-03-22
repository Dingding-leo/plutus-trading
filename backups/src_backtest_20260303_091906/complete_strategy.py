"""
Complete Workflow Strategy - Following TRADING_WORKFLOW.md exactly.
"""

from datetime import datetime
from typing import Dict, List, Optional
from .engine import BacktestEngine, TradeDirection
from ..analysis import indicators
from ..execution import position_sizer


class CompleteWorkflowStrategy:
    """
    Complete implementation of TRADING_WORKFLOW.md.
    Uses strict rules from the workflow.
    """

    def __init__(
        self,
        risk_pct: float = 0.02,
        max_leverage: float = 50,
    ):
        self.risk_pct = risk_pct
        self.max_leverage = max_leverage

        # Track BTC trend for macro context
        self.btc_trend = "SIDEWAYS"
        self.btc_signal = "NEUTRAL"

    def analyze(self, candles: List[dict]) -> Optional[dict]:
        """Full technical analysis per workflow."""
        if len(candles) < 50:
            return None

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        current = closes[-1]

        # Calculate EMAs (Step 3)
        try:
            ema50 = indicators.calculate_ema(closes, 50)
            ema200 = indicators.calculate_ema(closes, 200)
        except:
            return None

        # Trend detection
        trend = indicators.detect_trend(ema50, ema200)

        # RSI
        try:
            rsi = indicators.calculate_rsi(closes, 14)
        except:
            rsi = 50

        # Support/Resistance
        sr = indicators.find_support_resistance(closes, highs, lows)

        # Momentum (24h, 7d)
        momentum = indicators.calculate_momentum(closes)

        # Signal
        signal_data = indicators.get_signal(ema50, ema200, rsi)

        return {
            "current": current,
            "ema50": ema50,
            "ema200": ema200,
            "trend": trend,
            "rsi": rsi,
            "signal": signal_data["signal"],
            "support": sr["low"],
            "resistance": sr["high"],
            "position_in_range": sr["position_in_range"],
            "momentum": momentum,
        }

    def determine_risk_level(self, analysis: dict) -> str:
        """Classify risk level per workflow."""
        # HIGH RISK triggers
        triggers = 0

        # Check momentum
        momentum = analysis.get("momentum", {})
        change_24h = momentum.get("change_24h", 0)

        if abs(change_24h) > 5:  # Large move
            triggers += 1

        # Check RSI extremes
        rsi = analysis.get("rsi", 50)
        if rsi < 30 or rsi > 70:
            triggers += 1

        # Check position in range
        pos = analysis.get("position_in_range", 50)
        if pos > 90 or pos < 10:  # Near extremes
            triggers += 1

        if triggers >= 2:
            return "HIGH"
        elif triggers == 1:
            return "MODERATE"
        else:
            return "LOW"

    def get_position_mult(self, risk_level: str) -> float:
        """Get position multiplier per risk level."""
        multipliers = {
            "LOW": 1.0,
            "MODERATE": 0.85,
            "HIGH": 0.4,
        }
        return multipliers.get(risk_level, 0.7)

    def execute(
        self,
        engine: BacktestEngine,
        symbol: str,
        data: Dict[str, List[dict]],
        timestamp: datetime,
        ts_int: int
    ):
        """Execute strategy per workflow."""
        candles = data.get("1h", [])
        if len(candles) < 50:
            return

        analysis = self.analyze(candles)
        if not analysis:
            return

        # Track BTC for macro context
        if symbol == "BTC-USDT":
            self.btc_trend = analysis["trend"]
            self.btc_signal = analysis["signal"]

        current = analysis["current"]
        trend = analysis["trend"]
        signal = analysis["signal"]

        # Check open positions
        if symbol in engine.open_trades:
            trade = engine.open_trades[symbol]

            # Check stop/take (Step 3: calculate indicators)
            engine.check_stop_take(symbol, current, timestamp)

            # Exit on reversal (Step 5: reasoning)
            if trend == "DOWNTREND" and trade.direction == TradeDirection.LONG:
                engine.close_trade(symbol, current, timestamp, "TREND_REVERSAL")
            elif trend == "UPTREND" and trade.direction == TradeDirection.SHORT:
                engine.close_trade(symbol, current, timestamp, "TREND_REVERSAL")
            return

        # Determine direction per workflow
        direction = None

        # Only trade with trend
        if trend == "UPTREND" and signal in ["BUY", "NEUTRAL"]:
            direction = TradeDirection.LONG
        elif trend == "DOWNTREND" and signal in ["SELL", "NEUTRAL"]:
            direction = TradeDirection.SHORT
        else:
            return

        # Asset selection rules from workflow (Section 10)
        # BTC > ETH > ALT
        if self.btc_trend == "DOWNTREND" and symbol not in ["BTC-USDT", "ETH-USDT"]:
            return  # No alt longs in risk-off

        # Calculate entry/stop/target
        stop_pct = 0.02  # 2% stop
        rr_target = 2.0  # 2R target

        if direction == TradeDirection.LONG:
            entry = current
            stop = analysis["support"] * 0.99
            target = entry * (1 + stop_pct * rr_target)
        else:
            entry = current
            stop = analysis["resistance"] * 1.01
            target = entry * (1 - stop_pct * rr_target)

        # Verify stop distance
        stop_distance = abs(entry - stop) / entry
        if stop_distance < 0.01:  # Too tight
            return

        # Position sizing (Step 9)
        risk_level = self.determine_risk_level(analysis)
        pos_mult = self.get_position_mult(risk_level)

        coin_type = "major" if symbol in ["BTC-USDT", "ETH-USDT"] else "small"
        position = position_sizer.calculate_position_size(
            equity=engine.equity,
            risk_pct=self.risk_pct,
            stop_distance=stop_distance,
            pos_mult=pos_mult,
            coin_type=coin_type,
            training_mode=False,
        )

        if not position["valid"]:
            return

        # RR check
        reward = abs(target - entry)
        risk = abs(entry - stop)
        rr = reward / risk

        if rr < 1.5:
            return

        # Execute
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


def run_complete_backtest(
    symbols=None,
    start_date='2025-09-01',
    end_date='2026-03-02',
    initial_equity=10000,
    **kwargs
):
    """Run complete workflow backtest."""
    from .simple_fetch import fetch_binance_history
    from .engine import format_results

    if symbols is None:
        # All major futures per workflow
        symbols = [
            'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT',
            'XRPUSDT', 'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT',
            'DOTUSDT', 'MATICUSDT', 'LINKUSDT', 'UNIUSDT',
            'ATOMUSDT', 'LTCUSDT', 'ETCUSDT', 'XLMUSDT',
        ]

    # Fetch data
    data = {}
    for sym in symbols:
        print(f'Fetching {sym}...')
        data[sym] = fetch_binance_history(sym, '1h', start_date, end_date, 5000)

    # Filter out symbols with no data
    valid_symbols = [s for s in symbols if len(data.get(s, [])) > 50]
    print(f'Valid symbols: {len(valid_symbols)}/{len(symbols)}')

    engine = BacktestEngine(initial_equity)
    strategy = CompleteWorkflowStrategy(**kwargs)

    min_len = min(len(data[s]) for s in valid_symbols)
    if min_len < 50:
        print('Not enough data')
        return format_results(engine.get_results())

    print(f'Running backtest with {len(valid_symbols)} symbols, {min_len} time points...')

    for i in range(50, min_len):
        ts = data[valid_symbols[0]][i]['timestamp']
        current_time = data[valid_symbols[0]][i]['datetime']

        for sym in valid_symbols:
            strategy.execute(engine, sym.replace('USDT', '-USDT'), {'1h': data[sym][:i+1]}, current_time, ts)

    result = engine.get_results()
    return format_results(result)
