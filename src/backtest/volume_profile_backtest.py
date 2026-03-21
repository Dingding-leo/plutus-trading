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

    current_price = closes[-1]
    current_volume = volumes[-1]
    avg_volume = sum(volumes[-20:]) / 20

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

    # Check for LONG entry at LVN (support)
    for lvn in lvns:
        lvn_price = lvn["price"]
        # Price within 1.5% of LVN
        if abs(current_price - lvn_price) / current_price < 0.015:
            # Bullish confirmation: price above EMA 20, upward momentum
            try:
                ema20 = indicators.calculate_ema(closes, 20)
                ema50 = indicators.calculate_ema(closes, 50)
                rsi = indicators.calculate_rsi(closes, 14)
                momentum = indicators.calculate_momentum(closes)
                mom = momentum.get("change_24h", 0) if momentum else 0
            except Exception:
                continue

            if ema20 > ema50 and mom > -1 and rsi < 70 and current_volume > avg_volume * 0.5:
                direction = TradeDirection.LONG
                stop_loss = lvn_price * 0.97  # 3% stop
                take_profit = current_price + (current_price - stop_loss) * 2  # 2R
                break

    # Check for SHORT entry at HVN (resistance) if no long signal
    if direction is None:
        for hvn in hvns:
            hvn_price = hvn["price"]
            # Price within 1.5% of HVN
            if abs(current_price - hvn_price) / current_price < 0.015:
                # Bearish confirmation: price below EMA 20, downward momentum
                try:
                    ema20 = indicators.calculate_ema(closes, 20)
                    ema50 = indicators.calculate_ema(closes, 50)
                    rsi = indicators.calculate_rsi(closes, 14)
                    momentum = indicators.calculate_momentum(closes)
                    mom = momentum.get("change_24h", 0) if momentum else 0
                except Exception:
                    continue

                if ema20 < ema50 and mom < 1 and rsi > 30 and current_volume > avg_volume * 0.5:
                    direction = TradeDirection.SHORT
                    stop_loss = hvn_price * 1.03  # 3% stop
                    take_profit = current_price - (stop_loss - current_price) * 2  # 2R
                    break

    # If signal, open trade
    if direction:
        # Use 10% of equity per trade, 10x leverage
        position_size = (engine.equity * 0.10) / current_price

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

    # Get results
    result = engine.get_results()
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
