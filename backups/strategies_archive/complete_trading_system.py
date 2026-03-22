"""
Complete Trading System - Full TRADING_WORKFLOW.md Implementation.
"""

from datetime import datetime
from typing import Dict, List
from .engine import BacktestEngine, TradeDirection
from ..analysis import indicators
from ..execution import position_sizer
from ..data.workflow_analyzer import WorkflowAnalyzer, analyze_market_rule_based
from ..data import coin_tiers


class CompleteTradingSystem:
    """
    Complete trading system following TRADING_WORKFLOW.md exactly.
    Uses rule-based "LLM" analysis for decisions.
    """

    def __init__(self, config: dict = None):
        self.config = config or {}

        # Workflow analyzer (the "brain")
        self.analyzer = WorkflowAnalyzer()

        # Track BTC for macro context
        self.btc_analysis = None
        self.eth_analysis = None
        self.alt_analyses = {}

    def analyze(self, candles: List[dict]) -> dict:
        """Full technical analysis per workflow Step 3."""
        if len(candles) < 50:
            return None

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        current = closes[-1]

        try:
            ema20 = indicators.calculate_ema(closes, 20)
            ema50 = indicators.calculate_ema(closes, 50)
            ema200 = indicators.calculate_ema(closes, 200)
            rsi = indicators.calculate_rsi(closes, 14)
        except Exception:
            return None

        # Trend (Step 3.1)
        if ema50 > ema200:
            trend = "UPTREND"
        elif ema50 < ema200:
            trend = "DOWNTREND"
        else:
            trend = "SIDEWAYS"

        # Support/Resistance
        sr = indicators.find_support_resistance(closes, highs, lows)

        # Momentum (Step 3.1)
        momentum = indicators.calculate_momentum(closes)

        # Signal (Step 3.2)
        if trend == "UPTREND" and rsi < 65:
            signal = "BUY"
        elif trend == "DOWNTREND" and rsi > 35:
            signal = "SELL"
        else:
            signal = "NEUTRAL"

        # Quality score
        quality = 0
        if trend == "UPTREND" and rsi < 60:
            quality += 2
        if trend == "DOWNTREND" and rsi > 40:
            quality += 2
        if sr["position_in_range"] < 30:
            quality += 2
        if sr["position_in_range"] > 70:
            quality += 1

        return {
            "current": current,
            "ema20": ema20,
            "ema50": ema50,
            "ema200": ema200,
            "trend": trend,
            "rsi": rsi,
            "signal": signal,
            "support": sr["low"],
            "resistance": sr["high"],
            "position_in_range": sr["position_in_range"],
            "momentum": momentum,
            "quality": quality,
        }

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

        # Track for macro analysis
        symbol_base = symbol.replace("-USDT", "USDT")
        if symbol_base == "BTCUSDT":
            self.btc_analysis = analysis
        elif symbol_base == "ETHUSDT":
            self.eth_analysis = analysis
        else:
            self.alt_analyses[symbol_base] = analysis

        # Get tier parameters
        params = coin_tiers.get_params(symbol_base)
        min_quality = params.get("min_quality", 4)

        # Check quality threshold
        if analysis["quality"] < min_quality:
            return

        current = analysis["current"]

        # Check open position
        if symbol in engine.open_trades:
            trade = engine.open_trades[symbol]

            # Check stop/take (Step 3)
            engine.check_stop_take(symbol, current, timestamp)

            # Exit on reversal
            if analysis["trend"] == "DOWNTREND" and trade.direction == TradeDirection.LONG:
                engine.close_trade(symbol, current, timestamp, "TREND_REVERSAL")
            elif analysis["trend"] == "UPTREND" and trade.direction == TradeDirection.SHORT:
                engine.close_trade(symbol, current, timestamp, "TREND_REVERSAL")
            return

        # Get tier parameters
        max_leverage = params["max_leverage"]
        risk_pct = params["risk_pct"]
        stop_pct = params["stop_pct"]
        target_mult = params["target_mult"]

        # Determine direction per workflow (Step 4 & 5)
        direction = None

        # Must have trend alignment
        if analysis["trend"] == "UPTREND" and analysis["signal"] in ["BUY", "NEUTRAL"]:
            # Check if allowed per workflow
            if symbol_base == "BTCUSDT":
                direction = TradeDirection.LONG
            elif symbol_base == "ETHUSDT" and self.btc_analysis and self.btc_analysis["trend"] != "DOWNTREND":
                direction = TradeDirection.LONG
            elif self.btc_analysis and self.btc_analysis["trend"] == "UPTREND":
                direction = TradeDirection.LONG

        elif analysis["trend"] == "DOWNTREND" and analysis["signal"] in ["SELL", "NEUTRAL"]:
            direction = TradeDirection.SHORT

        if not direction:
            return

        # Calculate entry/stop/target
        if direction == TradeDirection.LONG:
            entry = current
            stop = analysis["support"] * (1 - stop_pct)
            target = entry * (1 + stop_pct * target_mult)
        else:
            entry = current
            stop = analysis["resistance"] * (1 + stop_pct)
            target = entry * (1 - stop_pct * target_mult)

        # Verify stop distance
        stop_dist = abs(entry - stop) / entry
        if stop_dist < 0.01:
            return

        # Position sizing (Step 9)
        coin_type = "major" if symbol_base in coin_tiers.TIER_1 else "small"
        position = position_sizer.calculate_position_size(
            equity=engine.equity,
            risk_pct=risk_pct,
            stop_distance=stop_dist,
            pos_mult=1.0,
            coin_type=coin_type,
            training_mode=False,
        )

        if not position["valid"]:
            return

        # RR check
        rr = abs(target - entry) / abs(entry - stop)
        if rr < 1.5:
            return

        # Execute
        size = position["max_position"] / entry
        leverage = min(position["recommended_leverage"], max_leverage)

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


def run_full_backtest(
    start_date='2025-09-01',
    end_date='2026-03-02',
    initial_equity=10000,
    tiers=None,
):
    """Run complete workflow backtest."""
    from .simple_fetch import fetch_binance_history
    from .engine import format_results

    if tiers is None:
        tiers = ["TIER_1", "TIER_2", "TIER_3"]

    # Get symbols from tiers
    symbols = []
    for tier in tiers:
        if tier == "TIER_1":
            symbols.extend(coin_tiers.TIER_1)
        elif tier == "TIER_2":
            symbols.extend(coin_tiers.TIER_2)
        elif tier == "TIER_3":
            symbols.extend(coin_tiers.TIER_3)
        elif tier == "TIER_4":
            symbols.extend(coin_tiers.TIER_4)

    # Fetch data
    data = {}
    for sym in symbols:
        print(f'Fetching {sym}...')
        data[sym] = fetch_binance_history(sym, '1h', start_date, end_date, 5000)

    # Filter valid
    valid = [s for s in symbols if len(data.get(s, [])) > 50]
    print(f'Valid: {len(valid)}/{len(symbols)}')

    engine = BacktestEngine(initial_equity)
    strategy = CompleteTradingSystem()

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
