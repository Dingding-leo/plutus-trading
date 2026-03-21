"""
Optimized Strategy - High win rate with all coins.
Combines quality signals from improved_strategy with tier-based parameters.
"""

from datetime import datetime
from typing import Dict, List
from .engine import BacktestEngine, TradeDirection
from ..analysis import indicators
from ..execution import position_sizer
from ..data import coin_tiers


class OptimizedStrategy:
    """
    Optimized strategy with high win rate and all coins.
    Based on improved_strategy's quality approach.
    """

    def __init__(
        self,
        risk_pct: float = 0.02,
        max_leverage: float = 50,
        quality_threshold: int = 4,
    ):
        self.risk_pct = risk_pct
        self.max_leverage = max_leverage
        self.quality_threshold = quality_threshold

        # Track BTC for macro context
        self.btc_trend = "SIDEWAYS"
        self.btc_in_uptrend = False

    def analyze(self, candles: List[dict]) -> dict:
        """Analysis with multiple confirmations."""
        if len(candles) < 50:
            return None

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        current = closes[-1]

        try:
            ema20 = indicators.calculate_ema(closes, 20)
            ema50 = indicators.calculate_ema(closes, 50)
            rsi = indicators.calculate_rsi(closes, 14)
        except Exception:
            return None

        # Trend
        if ema20 > ema50:
            trend = "UPTREND"
        elif ema20 < ema50:
            trend = "DOWNTREND"
        else:
            trend = "SIDEWAYS"

        # Support/Resistance
        sr = indicators.find_support_resistance(closes, highs, lows)

        # Quality score (same as improved_strategy)
        quality_score = 0

        # Trend aligned with RSI
        if trend == "UPTREND" and rsi < 60 and rsi > 30:
            quality_score += 2
        elif trend == "DOWNTREND" and rsi > 40 and rsi < 70:
            quality_score += 2

        # Near support in uptrend
        if trend == "UPTREND" and sr["position_in_range"] < 40:
            quality_score += 2

        # Near resistance in downtrend
        if trend == "DOWNTREND" and sr["position_in_range"] > 60:
            quality_score += 2

        # RSI oversold/overbought reversal
        if rsi < 35 and trend == "UPTREND":
            quality_score += 3
        elif rsi > 65 and trend == "DOWNTREND":
            quality_score += 3

        return {
            "current": current,
            "trend": trend,
            "rsi": rsi,
            "ema20": ema20,
            "ema50": ema50,
            "support": sr["low"],
            "resistance": sr["high"],
            "position_in_range": sr["position_in_range"],
            "quality_score": quality_score,
        }

    def execute(
        self,
        engine: BacktestEngine,
        symbol: str,
        data: Dict[str, List[dict]],
        timestamp: datetime,
        ts_int: int
    ):
        """Execute with quality entries."""
        candles = data.get("1h", [])
        if len(candles) < 50:
            return

        analysis = self.analyze(candles)
        if not analysis:
            return

        # Track BTC
        symbol_base = symbol.replace("-USDT", "USDT")
        if symbol_base == "BTCUSDT":
            self.btc_trend = analysis["trend"]
            self.btc_in_uptrend = analysis["trend"] == "UPTREND"

        current = analysis["current"]
        trend = analysis["trend"]
        quality = analysis["quality_score"]

        # Need minimum quality score
        if quality < self.quality_threshold:
            return

        # Check position
        if symbol in engine.open_trades:
            trade = engine.open_trades[symbol]
            engine.check_stop_take(symbol, current, timestamp)

            # Exit on trend reversal
            if trend == "DOWNTREND" and trade.direction == TradeDirection.LONG:
                engine.close_trade(symbol, current, timestamp, "TREND_REVERSAL")
            elif trend == "UPTREND" and trade.direction == TradeDirection.SHORT:
                engine.close_trade(symbol, current, timestamp, "TREND_REVERSAL")
            return

        # Determine direction
        direction = None

        # Only in trend direction with quality
        if trend == "UPTREND" and quality >= self.quality_threshold:
            direction = TradeDirection.LONG
        elif trend == "DOWNTREND" and quality >= self.quality_threshold:
            direction = TradeDirection.SHORT

        if not direction:
            return

        # No alt longs when BTC not in uptrend
        if symbol_base != "BTCUSDT" and not self.btc_in_uptrend and direction == TradeDirection.LONG:
            return

        # Get tier parameters
        tier = coin_tiers.get_tier(symbol_base)
        params = coin_tiers.TIER_PARAMS.get(tier, coin_tiers.TIER_PARAMS["TIER_4"])

        # Calculate stops - use support/resistance approach
        stop_pct = params.get("stop_pct", 0.015)
        target_mult = params.get("target_mult", 3)
        tier_risk_pct = params.get("risk_pct", 0.02)

        if direction == TradeDirection.LONG:
            entry = current
            stop = analysis["support"] * (1 - stop_pct * 0.5)
            target = entry * (1 + stop_pct * target_mult)
        else:
            entry = current
            stop = analysis["resistance"] * (1 + stop_pct * 0.5)
            target = entry * (1 - stop_pct * target_mult)

        stop_dist = abs(entry - stop) / entry

        if stop_dist < 0.008:
            return

        # Position sizing per tier - use tier-specific risk
        coin_type = "major" if tier in ["TIER_1", "TIER_2"] else "small"
        leverage_cap = params.get("max_leverage", 50)

        position = position_sizer.calculate_position_size(
            equity=engine.equity,
            risk_pct=tier_risk_pct,  # Use tier-specific risk
            stop_distance=stop_dist,
            pos_mult=1.0,
            coin_type=coin_type,
            training_mode=False,
        )

        if not position["valid"]:
            return

        # Check RR - require good risk reward (lower threshold for higher targets)
        rr = abs(target - entry) / abs(entry - stop)
        if rr < 2.0:
            return

        size = position["max_position"] / entry
        leverage = min(position["recommended_leverage"], leverage_cap, self.max_leverage)

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


def run_optimized_backtest(
    start_date='2025-09-01',
    end_date='2026-03-02',
    initial_equity=10000,
    **kwargs
):
    """Run optimized backtest with all tier coins."""
    from .simple_fetch import fetch_binance_history
    from .engine import format_results

    # Get all coins from all tiers
    symbols = coin_tiers.ALL_TIERS[:50]  # Top 50 coins

    # Fetch data
    data = {}
    for sym in symbols:
        print(f'Fetching {sym}...')
        data[sym] = fetch_binance_history(sym, '1h', start_date, end_date, 5000)

    # Filter valid
    valid = [s for s in symbols if len(data.get(s, [])) > 50]
    print(f'Valid: {len(valid)}/{len(symbols)}')

    engine = BacktestEngine(initial_equity)
    strategy = OptimizedStrategy(**kwargs)

    min_len = min(len(data[s]) for s in valid)
    print(f'Running with {len(valid)} symbols, {min_len} points...')

    for i in range(50, min_len):
        ts = data[valid[0]][i]['timestamp']
        current_time = data[valid[0]][i]['datetime']

        for sym in valid:
            strategy.execute(
                engine,
                sym.replace('USDT', '-USDT'),
                {'1h': data[sym][:i+1]},
                current_time,
                ts
            )

    return format_results(engine.get_results())
