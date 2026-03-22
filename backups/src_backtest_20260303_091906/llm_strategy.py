"""
LLM-Powered Strategy - Following TRADING_WORKFLOW.md with LLM analysis.
"""

from datetime import datetime
from typing import Dict, List, Optional
from .engine import BacktestEngine, TradeDirection
from .analysis import indicators
from .execution import position_sizer
from .data_client import data_client
from ..data.llm_client import analyze_market


class LLMWorkflowStrategy:
    """
    Strategy using LLM for market analysis as per TRADING_WORKFLOW.md.
    """

    def __init__(
        self,
        risk_pct: float = 0.02,
        max_leverage: float = 50,
        use_llm: bool = True,
        llm_interval: int = 4,  # Run LLM every N candles
    ):
        self.risk_pct = risk_pct
        self.max_leverage = max_leverage
        self.use_llm = use_llm
        self.llm_interval = llm_interval

        # Track candles for LLM
        self.candle_count = 0
        self.last_llm_decision = None

    def analyze(self, candles: List[dict]) -> Optional[dict]:
        """Technical analysis."""
        if len(candles) < 50:
            return None

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        current = closes[-1]

        try:
            ema50 = indicators.calculate_ema(closes, 50)
            ema200 = indicators.calculate_ema(closes, 200)
            rsi = indicators.calculate_rsi(closes, 14)
        except:
            return None

        trend = indicators.detect_trend(ema50, ema200)
        sr = indicators.find_support_resistance(closes, highs, lows)

        return {
            "trend": trend,
            "signal": indicators.get_signal(ema50, ema200, rsi)["signal"],
            "rsi": rsi,
            "current": current,
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
        candles = data.get("1h", [])
        if len(candles) < 50:
            return

        self.candle_count += 1

        # Analyze
        analysis = self.analyze(candles)
        if not analysis:
            return

        current = analysis["current"]

        # Check open positions
        if symbol in engine.open_trades:
            trade = engine.open_trades[symbol]
            engine.check_stop_take(symbol, current, timestamp)

            # Exit on trend reversal
            if analysis["trend"] == "DOWNTREND" and trade.direction == TradeDirection.LONG:
                engine.close_trade(symbol, current, timestamp, "TREND_REVERSAL")
            elif analysis["trend"] == "UPTREND" and trade.direction == TradeDirection.SHORT:
                engine.close_trade(symbol, current, timestamp, "TREND_REVERSAL")
            return

        # For BTC and ETH, use LLM analysis
        if symbol in ["BTC-USDT", "ETH-USDT"] and self.use_llm and self.candle_count % self.llm_interval == 0:
            # Get both analyses
            btc_data = analysis if symbol == "BTC-USDT" else self._get_btc_analysis(candles, engine)
            eth_data = self._get_eth_analysis(candles, engine) if symbol == "ETH-USDT" else analysis

            # Call LLM
            llm_result = analyze_market(
                btc_data={"current_price": btc_data.get("current", 0), "trend": btc_data.get("trend", "SIDEWAYS"), "rsi": btc_data.get("rsi", 50), "signal": btc_data.get("signal", "NEUTRAL"), "support": btc_data.get("support", 0), "resistance": btc_data.get("resistance", 0)},
                eth_data={"current_price": eth_data.get("current", 0), "trend": eth_data.get("trend", "SIDEWAYS")},
                market_overview={"fear_greed_index": 50},
            )

            self.last_llm_decision = llm_result

            # Execute LLM decision
            if llm_result.get("decision") == "BUY":
                direction = TradeDirection.LONG
            elif llm_result.get("decision") == "SELL":
                direction = TradeDirection.SHORT
            else:
                return  # NO_TRADE
        else:
            # Use technical signals
            if analysis["trend"] == "UPTREND" and analysis["rsi"] < 65:
                direction = TradeDirection.LONG
            elif analysis["trend"] == "DOWNTREND" and analysis["rsi"] > 35:
                direction = TradeDirection.SHORT
            else:
                return

        # Calculate stop/target
        stop_pct = 0.02
        if direction == TradeDirection.LONG:
            entry = current
            stop = current * (1 - stop_pct)
            target = current * (1 + stop_pct * 2)
        else:
            entry = current
            stop = current * (1 + stop_pct)
            target = current * (1 - stop_pct * 2)

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

    def _get_btc_analysis(self, candles, engine):
        """Get BTC analysis for ETH decisions."""
        return {"current": 0, "trend": "SIDEWAYS", "rsi": 50, "signal": "NEUTRAL", "support": 0, "resistance": 0}

    def _get_eth_analysis(self, candles, engine):
        """Get ETH analysis."""
        return self.analyze(candles)


def run_llm_backtest(
    symbols=None,
    start_date='2025-09-01',
    end_date='2026-03-02',
    initial_equity=10000,
    **kwargs
):
    """Run LLM-powered backtest."""
    from .simple_fetch import fetch_binance_history
    from .engine import format_results

    if symbols is None:
        symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'ADAUSDT']

    # Fetch data
    data = {}
    for sym in symbols:
        print(f'Fetching {sym}...')
        data[sym] = fetch_binance_history(sym, '1h', start_date, end_date, 5000)

    engine = BacktestEngine(initial_equity)
    strategy = LLMWorkflowStrategy(**kwargs)

    min_len = min(len(data[s]) for s in symbols)

    for i in range(50, min_len):
        ts = data[symbols[0]][i]['timestamp']
        current_time = data[symbols[0]][i]['datetime']

        for sym in symbols:
            strategy.execute(engine, sym.replace('USDT', '-USDT'), {'1h': data[sym][:i+1]}, current_time, ts)

    result = engine.get_results()
    return format_results(result)
