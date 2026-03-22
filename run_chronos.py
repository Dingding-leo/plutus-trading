import pandas as pd
from src.backtest.chronos_engine import run_chronos_backtest, BacktestMode

def main():
    print("Loading BTCUSDT 1h data from Data Lake...")
    
    # Load the local CSV
    df = pd.read_csv("data/historical/BTCUSDT_1h.csv")
    
    # Ensure correct data types
    numeric_cols = ["open", "high", "low", "close", "volume"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors='coerce')
        
    # Standardize the timestamp column
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    
    # Sort chronologically just in case
    df = df.sort_values("timestamp").reset_index(drop=True)

    print(f"Loaded {len(df)} candles. Starting from {df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]}")

    # Start the Chronos Engine Backtest
    print("\nStarting Plutus V3.1 Chronos Backtest (LIVE)...")
    
    # API keys are loaded from environment variables only.
    # Set them in your shell before running, e.g.:
    #   export LLM_API_KEY="your-key-here"
    #   export LLM_BASE_URL="https://api.minimaxi.com/v1"
    #   export LLM_MODEL="MiniMax-Text-01"
    import os

    result = run_chronos_backtest(
        df=df.head(200),  # Limit to 200 candles to verify it works without breaking the bank
        mode=BacktestMode.LIVE,  # Real LLM API calls
        initial_equity=10_000.0,
        min_confidence=40,
        log_file="logs/btcusdt_live_test.json"
    )

    print("\nBacktest Complete! Check logs/btcusdt_live_test.json for full details.")

if __name__ == "__main__":
    main()
