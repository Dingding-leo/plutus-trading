"""
Volume Profile Levels Backtest - RAI-384

Strategy: Identify high-volume nodes (HVN) as resistance and low-volume nodes (LVN) as support.
Enter positions when price re-tests these levels with confirming volume.
"""

import sys
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.backtest.engine import BacktestEngine, MultiCoinBacktester, TradeDirection, format_results
from src.backtest.data_client import data_client
from src.analysis import volume_profile, indicators


# Symbols to backtest
DEFAULT_SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "DOGE-USDT", "XRP-USDT",
    "ADA-USDT", "AVAX-USDT", "DOT-USDT", "MATIC-USDT", "LINK-USDT",
]

DEFAULT_TIMEFRAMES = ["1h"]


def volume_profile_strategy(
    engine: BacktestEngine,
    symbol: str,
    data: Dict[str, List[dict]],
    current_time: datetime,
    timestamp: int
):
    """
    Volume Profile Levels Strategy:
    - Identify LVN (support) and HVN (resistance) from volume profile
    - Enter long when price tests LVN with bullish confirmation
    - Enter short when price tests HVN with bearish confirmation
    """
    candles_1h = data.get("1h", [])

    if len(candles_1h) < 200:
        return

    # Get candles up to current timestamp
    candles = [c for c in candles_1h if c["timestamp"] <= timestamp]

    if len(candles) < 200:
        return

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    # Use only closed candles for signal generation to avoid lookahead bias (Issue #14)
    # closed_candles excludes the current incomplete candle
    closed_candles = candles[:-1]
    if len(closed_candles) < 200:
        return

    closed_closes = [c["close"] for c in closed_candles]
    closed_highs = [c["high"] for c in closed_candles]
    closed_lows = [c["low"] for c in closed_candles]
    closed_volumes = [c["volume"] for c in closed_candles]

    # current_price is the last closed candle's close (not the incomplete current candle)
    current_price = closed_closes[-1]
    current_volume = closed_volumes[-1]
    avg_volume = sum(closed_volumes[-20:]) / 20

    # Check if we already have a position
    if symbol in engine.open_trades:
        return  # Already in position

    # Calculate volume profile for last 200 candles
    try:
        profile = volume_profile.calculate_volume_profile(
            closes[-200:], volumes[-200:], highs[-200:], lows[-200:], bins=50
        )
        lvns = volume_profile.find_lvn(profile, threshold_percentile=20, num_nodes=5)
        hvns = volume_profile.find_hvn(profile, threshold_percentile=80, num_nodes=5)
    except Exception:
        return

    # Get recent high/low
    recent_high = max(closes[-50:])
    recent_low = min(closes[-50:])

    # Determine if price is near a level
    direction = None
    entry_price = current_price
    stop_loss = None
    take_profit = None

    # FIX #54: Pre-compute all indicators ONCE before iterating LVN/HVN levels.
    # Previously each candidate level re-ran calculate_ema/rsi/momentum/atr from
    # scratch — O(n) per level.  Now all four indicators are computed once and
    # reused, reducing complexity from O(n * num_levels) to O(n) total.
    _ema20_cache: float = None
    _ema50_cache: float = None
    _rsi_cache: float = None
    _mom_cache: float = None
    _atr_cache: float = None
    try:
        _ema20_cache = indicators.calculate_ema(closed_closes, 20)
        _ema50_cache = indicators.calculate_ema(closed_closes, 50)
        _rsi_cache = indicators.calculate_rsi(closed_closes, 14)
        _mom_raw = indicators.calculate_momentum(closed_closes)
        _mom_cache = _mom_raw.get("change_24h", 0) if _mom_raw else 0
        _atr_cache = indicators.calculate_atr(closed_highs, closed_lows, closed_closes, period=14)
    except Exception:
        # Indicators unavailable; skip signal generation for this candle
        pass

    # Check for LONG entry at LVN (support)
    for lvn in lvns:
        lvn_price = lvn["price"]
        # Price within 1.5% of LVN
        if abs(current_price - lvn_price) / current_price < 0.015:
            # Bullish confirmation: price above EMA 20, upward momentum
            if (
                _ema20_cache is not None
                and _ema50_cache is not None
                and _rsi_cache is not None
                and _mom_cache is not None
                and _atr_cache is not None
                and _ema20_cache > _ema50_cache
                and _mom_cache > -1
                and _rsi_cache < 70
                and current_volume > avg_volume * 0.5
            ):
                direction = TradeDirection.LONG
                # Volatility-adjusted stop: stop_distance = ATR_multiplier * ATR
                atr_multiplier = 2.0  # 2x ATR
                stop_distance = atr_multiplier * _atr_cache if _atr_cache > 0 else current_price * 0.02
                stop_loss = current_price - stop_distance
                take_profit = current_price + stop_distance * 2  # 2R
                break

    # Check for SHORT entry at HVN (resistance) if no long signal
    if direction is None:
        for hvn in hvns:
            hvn_price = hvn["price"]
            # Price within 1.5% of HVN
            if abs(current_price - hvn_price) / current_price < 0.015:
                # Bearish confirmation: price below EMA 20, downward momentum
                if (
                    _ema20_cache is not None
                    and _ema50_cache is not None
                    and _rsi_cache is not None
                    and _mom_cache is not None
                    and _atr_cache is not None
                    and _ema20_cache < _ema50_cache
                    and _mom_cache < 1
                    and _rsi_cache > 30
                    and current_volume > avg_volume * 0.5
                ):
                    direction = TradeDirection.SHORT
                    # Volatility-adjusted stop: stop_distance = ATR_multiplier * ATR
                    atr_multiplier = 2.0  # 2x ATR
                    stop_distance = atr_multiplier * _atr_cache if _atr_cache > 0 else current_price * 0.02
                    stop_loss = current_price + stop_distance
                    take_profit = current_price - stop_distance * 2  # 2R
                    break

    # If signal, open trade
    if direction:
        # Risk-based position sizing (Issue #16):
        # position_size = (equity * risk_percent) / stop_distance_pct
        risk_percent = 0.01  # 1% of equity per trade
        stop_distance_pct = abs(current_price - stop_loss) / current_price
        position_size = (engine.equity * risk_percent) / stop_distance_pct

        engine.open_trade(
            symbol=symbol,
            direction=direction,
            entry_price=current_price,
            size=position_size,
            leverage=10.0,
            stop_loss=stop_loss,
            take_profit=take_profit,
            timestamp=current_time,
        )


def _simulate_trade_outcome(
    trade,
    current_price: float,
    stop_loss: float,
    take_profit: float,
    direction: str
) -> dict:
    """
    Simulate trade outcome with liquidation check (Issue #17).

    Returns a dict with keys:
        liquidated (bool): True if position was liquidated
        exit_reason (str): "LIQUIDATION", "STOP_LOSS", "TAKE_PROFIT", or "SIGNAL"
        exit_price (float): Price at which position was closed
    """
    # Calculate liquidation price
    buffer = 0.001  # 0.1% buffer (conservative)
    if direction == "LONG":
        liq_price = trade.entry_price * (1 - 1.0 / trade.leverage - buffer)
        if current_price <= liq_price:
            return {
                "liquidated": True,
                "exit_reason": "LIQUIDATION",
                "exit_price": liq_price,
            }
    else:  # SHORT
        # Buffer is SUBTRACTED so liquidation triggers slightly earlier (conservative)
        liq_price = trade.entry_price * (1 + 1.0 / trade.leverage - buffer)
        if current_price >= liq_price:
            return {
                "liquidated": True,
                "exit_reason": "LIQUIDATION",
                "exit_price": liq_price,
            }

    # No liquidation - determine if stop or take profit hit
    if direction == "LONG":
        if stop_loss and current_price <= stop_loss:
            return {"liquidated": False, "exit_reason": "STOP_LOSS", "exit_price": stop_loss}
        if take_profit and current_price >= take_profit:
            return {"liquidated": False, "exit_reason": "TAKE_PROFIT", "exit_price": take_profit}
    else:  # SHORT
        if stop_loss and current_price >= stop_loss:
            return {"liquidated": False, "exit_reason": "STOP_LOSS", "exit_price": stop_loss}
        if take_profit and current_price <= take_profit:
            return {"liquidated": False, "exit_reason": "TAKE_PROFIT", "exit_price": take_profit}

    return {"liquidated": False, "exit_reason": "SIGNAL", "exit_price": current_price}


def check_exits(engine: BacktestEngine, symbol: str, current_price: float, current_time: datetime):
    """Check for exit conditions."""
    if symbol not in engine.open_trades:
        return

    # Check stop/take
    reason = engine.check_stop_take(symbol, current_price, current_time)


def run_backtest(
    symbols: List[str] = None,
    start_date: str = None,
    end_date: str = None,
    initial_equity: float = 10000.0,
    timeframes: List[str] = None,
) -> Dict:
    """Run the volume profile backtest."""
    symbols = symbols or DEFAULT_SYMBOLS
    timeframes = timeframes or DEFAULT_TIMEFRAMES

    # Default dates - use 90 days for meaningful backtest
    if not start_date:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=90)
        start_date = start_dt.strftime("%Y-%m-%d")
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")

    print(f"Running Volume Profile Levels Backtest")
    print(f"Symbols: {len(symbols)}")
    print(f"Period: {start_date} to {end_date}")
    print(f"Timeframes: {', '.join(timeframes)}")
    print(f"Initial Equity: ${initial_equity:,.2f}")
    print("=" * 60)

    # Initialize
    engine = BacktestEngine(initial_equity=initial_equity)

    # Create backtester
    backtester = MultiCoinBacktester(engine)

    # Fetch data
    backtester.fetch_data(symbols, timeframes, start_date, end_date)

    # Get all unique timestamps
    all_timestamps = set()
    for symbol in symbols:
        for tf in timeframes:
            for c in backtester.data_cache.get(symbol, {}).get(tf, []):
                all_timestamps.add(c["timestamp"])

    sorted_timestamps = sorted(all_timestamps)

    print(f"\nRunning backtest from {start_date} to {end_date}...")
    print(f"Total time points: {len(sorted_timestamps)}")

    # Run strategy at each timestamp
    for i, ts in enumerate(sorted_timestamps):
        current_time = datetime.fromtimestamp(ts / 1000)

        if i % 1000 == 0:
            print(f"  Progress: {i}/{len(sorted_timestamps)}")

        # Run strategy for each symbol
        for symbol in symbols:
            candles_1h = backtester.data_cache.get(symbol, {}).get("1h", [])

            if not candles_1h:
                continue

            # Get current price
            current_candles = [c for c in candles_1h if c["timestamp"] <= ts]
            if not current_candles:
                continue

            current_price = current_candles[-1]["close"]

            # Check exits first
            check_exits(engine, symbol, current_price, current_time)

            # Then check entries
            volume_profile_strategy(
                engine,
                symbol,
                backtester.data_cache.get(symbol, {}),
                current_time,
                ts
            )

    # Get last close price for each symbol so open positions are closed
    # at the actual final close price, not the entry price (FIX #2)
    final_prices = {}
    for symbol in symbols:
        candles = backtester.data_cache.get(symbol, {}).get("1h", [])
        if candles:
            final_prices[symbol] = candles[-1]["close"]

    # Get results
    result = engine.get_results(final_prices=final_prices)
    output = format_results(result)

    return {
        "result": result,
        "output": output,
    }


if __name__ == "__main__":
    result = run_backtest()
    print("\n" + "=" * 60)
    print("BACKTEST RESULTS")
    print("=" * 60)
    print(result["output"])
