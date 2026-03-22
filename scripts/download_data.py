import argparse
import time
from datetime import datetime
from pathlib import Path
from typing import List, Any

import pandas as pd
import requests

BINANCE_API_URL = "https://api.binance.com/api/v3/klines"
LIMIT = 1000

COLUMNS = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "number_of_trades",
    "taker_buy_base_asset_volume",
    "taker_buy_quote_asset_volume",
    "ignore",
]

def fetch_klines(symbol: str, interval: str, start_ts: int, end_ts: int) -> List[List[Any]]:
    all_klines = []
    current_start = start_ts

    while current_start < end_ts:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "endTime": end_ts,
            "limit": LIMIT,
        }

        try:
            print(f"Fetching {symbol} {interval} from {datetime.fromtimestamp(current_start/1000)}...")
            response = requests.get(BINANCE_API_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            if not data:
                break

            all_klines.extend(data)
            
            # Update current_start to the close time of the last candle + 1ms
            current_start = data[-1][6] + 1

            if len(data) < LIMIT:
                break

            time.sleep(0.5)  # Rate limit safety

        except Exception as e:
            print(f"Error fetching data: {e}")
            print("Waiting 5 seconds before retrying...")
            time.sleep(5)

    return all_klines

def main():
    parser = argparse.ArgumentParser(description="Download historical data from Binance.")
    parser.add_argument("--symbol", type=str, required=True, help="Trading pair symbol (e.g., BTCUSDT)")
    parser.add_argument("--interval", type=str, required=True, help="Candle interval (e.g., 1h, 1d)")
    parser.add_argument("--start", type=str, required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, required=True, help="End date (YYYY-MM-DD)")
    
    args = parser.parse_args()

    # Convert dates to timestamps in milliseconds
    start_ts = int(datetime.strptime(args.start, "%Y-%m-%d").timestamp() * 1000)
    end_ts = int(datetime.strptime(args.end, "%Y-%m-%d").timestamp() * 1000)

    print(f"Starting download for {args.symbol} ({args.interval}) from {args.start} to {args.end}")
    
    raw_data = fetch_klines(args.symbol, args.interval, start_ts, end_ts)
    
    if not raw_data:
        print("No data fetched.")
        return

    # Convert to DataFrame
    df = pd.DataFrame(raw_data, columns=COLUMNS)
    
    # Convert numeric columns
    numeric_cols = ["open", "high", "low", "close", "volume", "quote_asset_volume", 
                    "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors='coerce')
        
    # Convert timestamps to datetime for the main timestamp column
    # Keeping the original milliseconds is often useful, but standard pandas datetime is better
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit='ms')

    # Save to CSV
    output_dir = Path("data/historical")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    output_file = output_dir / f"{args.symbol}_{args.interval}.csv"
    df.to_csv(output_file, index=False)
    
    print(f"Successfully downloaded {len(df)} candles and saved to {output_file}")

if __name__ == "__main__":
    main()
