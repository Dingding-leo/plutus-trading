# Plutus Trading System - Developer Guide

> Last Updated: 2026-03-03

## Quick Start

### CLI Usage

```bash
# Run a backtest
python -m src.cli backtest --symbols BTC-USDT,ETH-USDT --start 2025-01-01 --end 2025-03-01 --equity 10000

# Run market analysis
python -m src.cli analyze --save

# Run intraday scanner
python -m src.cli scan --symbols BTC-USDT

# Generate trade plan
python -m src.cli trade --symbol BTC-USDT --direction LONG --entry 50000 --stop 49500 --target 52000

# Log feedback
python -m src.cli feedback

# Get help
python -m src.cli --help
python -m src.cli backtest --help
```

### Python Usage

```python
# Run a backtest
from src.backtest.strategy import run_backtest, StrategyConfig

config = StrategyConfig(
    base_risk_pct=0.01,  # 1% risk per trade
    pos_mult=1.0,
    max_leverage=50.0,
    min_rr=1.5,
)

result = run_backtest(
    symbols=["BTC-USDT", "ETH-USDT"],
    start_date="2025-01-01",
    end_date="2025-03-01",
    initial_equity=10000,
    config=config
)
print(result["output"])
```

---

## System Overview

Plutus is a crypto futures trading system that:
- Trades 45+ coins across OKX leverage tiers (100x, 50x, 20x)
- Uses multi-timeframe analysis (5m, 15m, 30m, 1h, 4h)
- Applies EMA/RSI for trend and momentum
- Uses Volume Profile for key level identification
- Implements risk-based position sizing with CLAUDE.md rules

---

## Project Structure

```
src/
├── cli.py                 # CLI entry point
├── config.py              # Global configuration
├── data/                  # Data fetching
│   ├── binance_client.py  # Binance API
│   ├── okx_client.py     # OKX API
│   ├── coingecko_client.py # CoinGecko API
│   ├── news_fetcher.py   # News checking
│   ├── coin_tiers.py     # Tier parameters ← EDIT THIS
│   ├── futures.py        # Tradable futures list
│   ├── workflow_analyzer.py # Market analysis
│   └── llm_client.py     # LLM integration
├── analysis/              # Technical analysis
│   ├── indicators.py     # EMA, RSI, ATR, S/R
│   ├── volume_profile.py # LVN/HVN calculations
│   └── market_context.py # Risk classification
├── execution/             # Trade execution
│   ├── position_sizer.py # Risk-based sizing
│   ├── decision_engine.py # Trade decisions
│   └── trade_plan.py     # Trade validation
├── backtest/             # Backtesting
│   ├── engine.py         # Core backtest engine
│   ├── strategy.py       # Main strategy logic
│   ├── production_strategy.py # Production-ready strategy
│   ├── optimized_strategy.py # Optimized strategy
│   └── *.py              # Other strategies
└── storage/              # Data persistence
    ├── daily_logger.py   # Daily analysis logs
    └── feedback_logger.py # Feedback logs
```

---

## CLI Commands

### backtest

```bash
python -m src.cli backtest \
    --symbols BTC-USDT,ETH-USDT,SOL-USDT \
    --start 2025-01-01 \
    --end 2025-03-01 \
    --equity 10000 \
    --risk 1.0 \
    --leverage 50 \
    --pos-mult 1.0 \
    --min-rr 1.5
```

Options:
- `--symbols`: Comma-separated symbols (default: BTC-USDT,ETH-USDT,SOL-USDT)
- `--start`: Start date (YYYY-MM-DD)
- `--end`: End date (YYYY-MM-DD)
- `--equity`: Initial equity (default: 10000)
- `--risk`: Risk per trade % (default: 1.0)
- `--leverage`: Max leverage (default: 50)
- `--pos-mult`: Position multiplier (default: 1.0)
- `--min-rr`: Minimum risk/reward ratio (default: 1.5)

### analyze

```bash
python -m src.cli analyze --save
```

Runs full market analysis and optionally saves to daily log.

### scan

```bash
python -m src.cli scan --symbols BTC-USDT,ETH-USDT
```

Runs intraday scanner across multiple timeframes.

### trade

```bash
python -m src.cli trade \
    --symbol BTC-USDT \
    --direction LONG \
    --entry 50000 \
    --stop 49500 \
    --target 52000
```

Generates and validates a trade plan.

### feedback

```bash
python -m src.cli feedback
```

Logs feedback for learning.

---

## Trading Rules (CLAUDE.md)

The system implements these rules from TRADING_WORKFLOW.md:

### Position Sizing

```
base_risk_$ = equity × 1%
effective_risk_$ = base_risk_$ × pos_mult
max_position_value = effective_risk_$ / stop_distance%
```

**Risk Environment Multipliers:**
- LOW: 1.0x-1.2x (no major news + normal volatility)
- MODERATE: 0.7x-1.0x (general news OR elevated volatility)
- HIGH: 0.3x-0.5x (CPI/FOMC/war/regulation OR ATR ≥ 1.5x)

**HIGH RISK Triggers:**
- Major macro (CPI/FOMC/NFP/war escalation/SEC regulation)
- 15m ATR ≥ 20-bar average × 1.5
- 30m structure just broke

### Asset Selection

```
BTC > ETH > ALT
```

- If BTC shows strength → trade BTC
- If risk_off + BTC weakness → NO TRADE or BTC SHORT only
- ALT longs forbidden in risk_off environments

### Decision Framework

```
PHASE 1: No trigger → NO TRADE
PHASE 2: Trigger but no confirmation → WAIT
PHASE 3: Trigger + confirmation → CHECK GATE → EXECUTE
```

**Execution Gate:**
- structure_break = True
- macro_aligned = True
- invalidation_clear = True
- RR ≥ 1.5 (including fees)

### Stop Loss Rules

- Stop must be ≥ 0.5% OR use stricter pos_mult (≤ 0.3)
- Major coins: liquidation buffer = 0.5%
- Small caps: liquidation buffer = 1.5%
- Open in tranches: 50% / 30% / 20%

---

## How to Modify

### 1. Change Trading Parameters

Edit `src/data/coin_tiers.py`:

```python
TIER_PARAMS = {
    "TIER_1": {
        "max_leverage": 100,    # OKX: 100x for BTC/ETH
        "risk_pct": 0.022,      # Risk % per trade
        "stop_pct": 0.008,      # Stop loss %
        "target_mult": 8.5,      # Target = stop * target_mult
    },
    # ... TIER_2 (50x), TIER_3 (20x)
}
```

### 2. Change Risk Rules

Edit `src/execution/position_sizer.py`:

```python
def get_position_multiplier(risk_level: str) -> float:
    if risk_level == "LOW":
        return 1.0   # 1.0x - 1.2x
    elif risk_level == "MODERATE":
        return 0.85  # 0.7x - 1.0x
    elif risk_level == "HIGH":
        return 0.4   # 0.3x - 0.5x
```

### 3. Change Entry Logic

Edit `src/backtest/strategy.py`:

```python
def check_entry(self, symbol, analysis, equity):
    # Entry criteria here
    if quality < self.quality_threshold:
        return None

    # RR check
    rr = abs(target - entry) / abs(entry - stop)
    if rr < self.min_rr:
        return None
```

### 4. Change Trading Fees

Edit `src/backtest/engine.py`:

```python
def __init__(
    self,
    initial_equity: float = 10000,
    maker_fee: float = 0.0002,  # 0.02%
    taker_fee: float = 0.0005,  # 0.05%
    slippage: float = 0.0005,    # 0.05%
):
```

---

## Key Classes

### BacktestEngine

```python
from src.backtest.engine import BacktestEngine

engine = BacktestEngine(initial_equity=10000)

# Open trade
engine.open_trade(
    symbol="BTC-USDT",
    direction=TradeDirection.LONG,
    entry_price=50000,
    size=0.1,
    leverage=10,
    stop_loss=49000,
    take_profit=52000,
    timestamp=datetime.now()
)

# Check exits
engine.check_stop_take("BTC-USDT", current_price, timestamp)

# Close trade
engine.close_trade("BTC-USDT", current_price, timestamp, "REASON")

# Get results (pass final prices for open trades)
results = engine.get_results(final_prices={"BTC-USDT": 51000})
```

### StrategyConfig

```python
from src.backtest.strategy import StrategyConfig

config = StrategyConfig(
    base_risk_pct=0.01,     # 1% risk per trade
    pos_mult=1.0,            # Position multiplier
    max_leverage=50.0,       # Max leverage
    training_mode=True,      # Training mode
    min_rr=1.5,             # Minimum RR
    min_resonance=2,         # Min timeframes for level alignment
    min_volume_profile_confidence=0.6,
)
```

---

## Technical Indicators

### EMA

```python
from src.analysis import calculate_ema

ema20 = calculate_ema(closes, 20)
ema50 = calculate_ema(closes, 50)
ema200 = calculate_ema(closes, 200)
```

### RSI

```python
from src.analysis import calculate_rsi

rsi = calculate_rsi(closes, 14)  # 14-period RSI
```

### Volume Profile

```python
from src.analysis import calculate_volume_profile, find_lvn, find_hvn

# With high-low distribution (recommended)
profile = calculate_volume_profile(closes, volumes, highs, lows, bins=50)
lvns = find_lvn(profile, threshold_percentile=20, num_nodes=3)
hvns = find_hvn(profile, threshold_percentile=80, num_nodes=3)
```

### Market Context

```python
from src.analysis import classify_risk_level, determine_macro_state

# Classify risk level
risk = classify_risk_level(
    has_war_news=False,
    has_macro_news=True,
    atr_multiplier=1.2,
    structure_broken=False,
    fear_greed_index=35
)
# Returns: "LOW", "MODERATE", or "HIGH"

# Determine macro state
macro = determine_macro_state(btc_analysis)
# Returns: "risk_on" or "risk_off"
```

---

## Position Sizing

```python
from src.execution import (
    get_position_multiplier,
    calculate_max_leverage,
    calculate_position_size,
    calculate_rr,
)

# Get position multiplier based on risk level
pos_mult = get_position_multiplier("MODERATE")  # Returns 0.85

# Calculate max leverage
max_lev = calculate_max_leverage(
    stop_distance=0.02,  # 2%
    coin_type="major"    # "major" or "small"
)

# Calculate position size
position = calculate_position_size(
    equity=10000,
    risk_pct=0.01,
    stop_distance=0.02,
    pos_mult=1.0,
    coin_type="major",
    training_mode=True
)

# Calculate RR with fees
rr = calculate_rr(entry=50000, stop=49500, target=51500, fees_pct=0.1)
# Returns: {risk, reward, rr_gross, rr_net, fees_pct}
```

---

## Trade Plan

```python
from src.execution import create_trade_plan, validate_trade_plan

# Create trade plan
plan = create_trade_plan(
    symbol="BTC-USDT",
    direction="LONG",
    entry=50000,
    stop=49500,
    target=52000,
    equity=10000,
    risk_pct=1.0,
    coin_type="major"
)

# Validate
validation = validate_trade_plan(plan)
# Returns: {valid, errors, warnings}
```

---

## OKX Leverage Tiers

| Tier | Max Leverage | Coins |
|------|--------------|-------|
| TIER_1 | 100x | BTC, ETH |
| TIER_2 | 50x | BNB, SOL, XRP, ADA, DOGE, AVAX, DOT, LINK, UNI, ATOM, etc. |
| TIER_3 | 20x | All others |

---

## LLM Integration

```python
from src.data import analyze_market

result = analyze_market(
    btc_data={'current_price': 65000, 'trend': 'UPTREND', 'rsi': 45},
    eth_data={'current_price': 3500, 'trend': 'UPTREND'},
    market_overview={'fear_greed_index': 55}
)
# Returns: {'decision': 'BUY', 'symbol': 'BTC', 'risk_level': 'MODERATE'}
```

API settings in `src/data/llm_client.py`:
- Base URL: `https://api.minimaxi.com/v1`
- Model: `MiniMax-M2.5`

---

## News Checking

```python
from src.data import NewsChecker

checker = NewsChecker()
result = checker.check_for_major_events()
# Returns: {has_critical_news, has_war_news, has_macro_news, risk_level}

# Manual override via environment variables:
# PLUTUS_HAS_CRITICAL_NEWS=true
# PLUTUS_HAS_WAR_NEWS=true
# PLUTUS_HAS_MACRO_NEWS=true
```

---

## Data Fetching

### Binance

```python
from src.data import fetch_klines, get_price_data, get_current_price

# Fetch klines
candles = fetch_klines("BTCUSDT", "1h", limit=200)

# Get multiple timeframes
data = get_price_data("BTCUSDT", ["1h", "4h", "1d"])

# Get current price
price = get_current_price("BTCUSDT")
```

### OKX

```python
from src.data import OKXClient

client = OKXClient()
candles = client.fetch_ohlcv("BTC-USDT", "1h", start_time="2025-01-01")
```

### CoinGecko

```python
from src.data import get_global_data, get_market_overview

data = get_global_data()
overview = get_market_overview()
```

---

## Common Issues

### 1. No trades generated
- Check quality_threshold is not too high
- Ensure data is fetching correctly
- Verify symbols exist on Binance

### 2. High drawdown
- Reduce risk_pct in TIER_PARAMS
- Increase stop_pct for wider stops
- Lower target_mult for lower RR

### 3. Low win rate
- Increase quality_threshold
- Increase RR minimum
- Tighten BTC trend filter

### 4. Import errors
- Make sure to activate virtual environment: `source .venv/bin/activate`
- Check all __init__.py files export correctly

---

## Files Reference

| File | Purpose |
|------|---------|
| `cli.py` | CLI entry point |
| `config.py` | Global configuration |
| `coin_tiers.py` | Tier parameters (risk, leverage, stops) |
| `engine.py` | Trade execution, P&L calculation |
| `strategy.py` | Main strategy logic |
| `indicators.py` | EMA, RSI, ATR, S/R calculations |
| `volume_profile.py` | LVN/HVN calculations |
| `market_context.py` | Risk classification |
| `position_sizer.py` | Risk-based position sizing |
| `decision_engine.py` | Trade decision logic |
| `trade_plan.py` | Trade validation |
| `llm_client.py` | LLM API integration |
| `binance_client.py` | Binance data fetching |
| `okx_client.py` | OKX data fetching |
| `daily_logger.py` | Daily analysis logging |
| `feedback_logger.py` | Feedback logging |

---

## Testing

```python
# Quick test
from src.backtest.strategy import run_backtest, StrategyConfig

config = StrategyConfig()
result = run_backtest(
    symbols=["BTC-USDT"],
    start_date="2025-01-01",
    end_date="2025-02-01",
    initial_equity=10000,
    config=config
)
print(result["output"])
```

---

## Contact

For issues or questions, refer to:
- `TRADING_WORKFLOW.md` - Original trading rules from CLAUDE.md
- `CLAUDE.md` - Detailed trading rules
- Source code comments in each module
