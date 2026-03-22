"""
Time-based Backtester - Iterates through time properly.
"""

from datetime import datetime, timedelta
from typing import List, Dict
from collections import defaultdict

from .engine import BacktestEngine
from .data_client import data_client


class TimeBasedBacktester:
    """Proper time-based backtester."""

    def __init__(self, engine: BacktestEngine):
        self.engine = engine
        self.data: Dict[str, Dict[str, List[dict]]] = defaultdict(dict)

    def load_data(
        self,
        symbols: List[str],
        timeframes: List[str],
        start_date: str,
        end_date: str
    ):
        """Load data for all symbols and timeframes."""
        print(f"Loading data for {len(symbols)} symbols...")

        for symbol in symbols:
            for tf in timeframes:
                print(f"  {symbol} {tf}...", end=" ")
                try:
                    candles = data_client.fetch_history(
                        symbol, tf, start_date, end_date, max_candles=5000
                    )
                    self.data[symbol][tf] = candles
                    print(f"{len(candles)} candles")
                except Exception as e:
                    print(f"Error: {e}")
                    self.data[symbol][tf] = []

    def get_data_at(self, symbol: str, tf: str, timestamp: int) -> List[dict]:
        """Get all candles up to timestamp."""
        candles = self.data.get(symbol, {}).get(tf, [])
        return [c for c in candles if c["timestamp"] <= timestamp]

    def run(
        self,
        symbols: List[str],
        timeframes: List[str],
        strategy_fn,
        start_date: str,
        end_date: str
    ):
        """Run backtest."""
        # Load data
        self.load_data(symbols, timeframes, start_date, end_date)

        # Get all unique timestamps from 1h timeframe (the anchor)
        all_timestamps = set()
        for symbol in symbols:
            for c in self.data.get(symbol, {}).get("1h", []):
                all_timestamps.add(c["timestamp"])

        sorted_timestamps = sorted(all_timestamps)
        print(f"\nRunning backtest from {start_date} to {end_date}...")
        print(f"Total time points: {len(sorted_timestamps)}")

        # Iterate through time
        for i, ts in enumerate(sorted_timestamps):
            current_time = datetime.fromtimestamp(ts / 1000)

            if i > 0 and i % 500 == 0:
                print(f"  Progress: {i}/{len(sorted_timestamps)}")

            # Run strategy for each symbol
            for symbol in symbols:
                # Prepare data for strategy
                symbol_data = {}
                for tf in timeframes:
                    symbol_data[tf] = self.get_data_at(symbol, tf, ts)

                # Run strategy
                strategy_fn(self.engine, symbol, symbol_data, current_time, ts)


def run_proper_backtest(
    symbols: List[str],
    strategy_fn,
    start_date: str,
    end_date: str = None,
    initial_equity: float = 10000,
) -> dict:
    """Run proper time-based backtest."""
    from .engine import BacktestEngine, format_results
    from datetime import timedelta

    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    engine = BacktestEngine(initial_equity=initial_equity)
    backtester = TimeBasedBacktester(engine)

    backtester.run(
        symbols=symbols,
        timeframes=["15m", "1h", "4h"],
        strategy_fn=strategy_fn,
        start_date=start_date,
        end_date=end_date,
    )

    return format_results(engine.get_results())
