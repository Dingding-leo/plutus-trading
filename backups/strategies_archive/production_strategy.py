"""
Production Trading System - Best of both worlds.
Combines quality signals with tier parameters.
"""

from datetime import datetime
from typing import Dict, List
from .engine import BacktestEngine, TradeDirection
from ..analysis import indicators
from ..execution import position_sizer
from ..data import coin_tiers
from ..data.coin_tiers import normalize_symbol, is_major


class ProductionStrategy:
    """
    Production-ready strategy with quality entries.
    """

    def __init__(self, risk_pct=0.02, max_leverage=100, risk_level="LOW"):
        self.risk_pct = risk_pct
        self.max_leverage = max_leverage
        self.risk_level = risk_level  # 'LOW', 'MODERATE', or 'HIGH'
        self.btc_trend = "SIDEWAYS"
        self.pos_mult = position_sizer.get_position_multiplier(risk_level)

    def analyze(self, candles: List[dict]) -> dict:
        """Quality analysis with multiple confirmations."""
        if len(candles) < 50:
            return None

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        current = closes[-1]

        try:
            ema20 = indicators.calculate_ema(closes, 20)
            ema50 = indicators.calculate_ema(closes, 50)
            ema200 = indicators.calculate_ema(closes, 200) if len(closes) >= 200 else None
            rsi = indicators.calculate_rsi(closes, 14)
        except Exception:
            return None

        # Trend based on EMA50 vs EMA200 (workflow standard)
        if ema200 is None:
            trend = "SIDEWAYS"
        elif ema50 > ema200:
            trend = "UPTREND"
        elif ema50 < ema200:
            trend = "DOWNTREND"
        else:
            trend = "SIDEWAYS"

        # Support/Resistance
        sr = indicators.find_support_resistance(closes, highs, lows)

        # Quality score
        score = 0

        # Trend + RSI alignment
        if trend == "UPTREND" and 30 < rsi < 55:
            score += 3
        elif trend == "DOWNTREND" and 45 < rsi < 70:
            score += 3

        # Near support in uptrend
        if trend == "UPTREND" and sr["position_in_range"] < 35:
            score += 3
        elif trend == "DOWNTREND" and sr["position_in_range"] > 65:
            score += 3

        # RSI extremes = reversal potential
        if rsi < 32 and trend == "UPTREND":
            score += 4  # Strong bounce
        elif rsi > 68 and trend == "DOWNTREND":
            score += 4

        return {
            "current": current,
            "trend": trend,
            "rsi": rsi,
            "ema20": ema20,
            "ema50": ema50,
            "ema200": ema200,
            "support": sr["low"],
            "resistance": sr["high"],
            "position_in_range": sr["position_in_range"],
            "quality": score,
        }

    def execute(self, engine, symbol, data, timestamp, ts_int):
        """Execute with quality."""
        candles = data.get("1h", [])
        if len(candles) < 50:
            return

        analysis = self.analyze(candles)
        if not analysis:
            return

        # Track BTC
        if symbol == "BTCUSDT":
            self.btc_trend = analysis["trend"]

        current = analysis["current"]
        quality = analysis["quality"]
        trend = analysis["trend"]

        # Check position
        if symbol in engine.open_trades:
            trade = engine.open_trades[symbol]
            engine.check_stop_take(symbol, current, timestamp)

            if trend == "DOWNTREND" and trade.direction == TradeDirection.LONG:
                engine.close_trade(symbol, current, timestamp, "REVERSAL")
            elif trend == "UPTREND" and trade.direction == TradeDirection.SHORT:
                engine.close_trade(symbol, current, timestamp, "REVERSAL")
            return

        # Quality threshold
        if quality < 5:
            return

        # Direction
        direction = None
        if trend == "UPTREND":
            direction = TradeDirection.LONG
        elif trend == "DOWNTREND":
            direction = TradeDirection.SHORT

        if not direction:
            return

        # ── Correlation Gate ─────────────────────────────────────────────────────────
        # Rule: NO alt longs when macro = risk-off OR BTC is in downtrend.
        # This prevents the "strongest alt" trap — in risk-off BTC drops first,
        # ETH follows, ALTs get crushed last and hardest.
        symbol_base = normalize_symbol(symbol)
        if not is_major(symbol_base):
            if direction == TradeDirection.LONG:
                if self.btc_trend == "DOWNTREND":
                    return  # R4: BTC weakness → no alt longs
                # Also block if risk environment is HIGH (from rule set)
                if self.risk_level == "HIGH":
                    return  # R4: HIGH risk environment → no alt longs

        # Stops
        stop_pct = 0.015
        if direction == TradeDirection.LONG:
            entry = current
            stop = current * (1 - stop_pct)
            target = current * (1 + stop_pct * 4)  # 4R
        else:
            entry = current
            stop = current * (1 + stop_pct)
            target = current * (1 - stop_pct * 4)

        stop_dist = abs(entry - stop) / entry
        if stop_dist < 0.01:
            return

        # Position sizing
        tier = coin_tiers.get_tier(symbol_base)
        params = coin_tiers.TIER_PARAMS.get(tier, coin_tiers.TIER_PARAMS["TIER_4"])

        coin_type = "major" if tier in ["TIER_1", "TIER_2"] else "small"
        leverage_cap = params["max_leverage"]

        position = position_sizer.calculate_position_size(
            equity=engine.equity,
            risk_pct=self.risk_pct,
            stop_distance=stop_dist,
            pos_mult=self.pos_mult,
            coin_type=coin_type,
            training_mode=False,
            risk_level=self.risk_level,
        )

        if not position["valid"]:
            return

        # RR check
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


def run_production_backtest(symbols=None, start_date='2025-09-01', end_date='2026-03-02', initial_equity=10000, **kwargs):
    """Run production backtest."""
    from .simple_fetch import fetch_binance_history
    from .engine import format_results

    if symbols is None:
        symbols = coin_tiers.ALL_TIERS[:30]  # Top 30

    data = {}
    for sym in symbols:
        print(f'Fetching {sym}...')
        data[sym] = fetch_binance_history(sym, '1h', start_date, end_date, 5000)

    valid = [s for s in symbols if len(data.get(s, [])) > 50]
    print(f'Valid: {len(valid)}/{len(symbols)}')

    engine = BacktestEngine(initial_equity)
    strategy = ProductionStrategy(**kwargs)

    min_len = min(len(data[s]) for s in valid)
    print(f'Running {len(valid)} symbols, {min_len} points...')

    for i in range(50, min_len):
        ts = data[valid[0]][i]['timestamp']
        current_time = data[valid[0]][i]['datetime']

        for sym in valid:
            strategy.execute(engine, sym, {'1h': data[sym][:i+1]}, current_time, ts)

    return format_results(engine.get_results())
