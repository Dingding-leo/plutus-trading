# Plutus Trading System

A systematic cryptocurrency trading platform built in Python. Analyzes markets, generates trade plans with precise risk management, and backtests strategies against historical Binance futures data.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLI (src/cli.py)                         │
│              analyze | scan | trade | backtest                 │
└──────────────┬────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                      DATA LAYER                                  │
│  Binance Futures ──► DataClient ─► Normalized candles            │
│  Binance Spot       BinanceClient                               │
│  OKX Futures       CoinGecko (market overview)                 │
│  News APIs         NewsService (announcements, Fear & Greed)     │
└──────────────┬────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                   ANALYSIS LAYER                                  │
│  indicators.py      ─ EMA, RSI, ATR, SMA, momentum, volatility  │
│  volume_profile.py  ─ LVN/HVN, multi-TF resonance              │
│  market_context.py ─ risk_on/off, BTC strength, valid answers   │
│  workflow_analyzer.py ─ rule-based market decision engine        │
└──────────────┬────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                 EXECUTION LAYER                                 │
│  decision_engine.py  ─ 3-phase framework (未动 → 冲击 → 确认)        │
│  position_sizer.py  ─ loss-based sizing, leverage, tranches      │
│  trade_plan.py      ─ standardized trade plan output           │
└──────────────┬────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                 BACKTEST LAYER                                   │
│  engine.py        ─ BacktestEngine + MultiCoinBacktester         │
│  strategy.py      ─ WorkflowStrategy (main strategy)             │
│  time_based.py    ─ proper time-iterated backtesting            │
│  10+ strategy variants  ─ production, LLM, EMA, aggressive     │
└─────────────────────────────────────────────────────────────────┘
```

---

## Workflow (Daily Analysis)

```
STEP 1: Data Collection
  └─► Fetch Binance futures candles (1h, 4h, 1d) for BTC, ETH, SOL + alts
  └─► Fetch CoinGecko global metrics (market cap, BTC dominance)
  └─► Fetch Fear & Greed Index
  └─► Fetch news (Binance announcements, CryptoPanic)

STEP 2: News & Macro Check
  └─► Classify risk level (LOW / MODERATE / HIGH)
  └─► Check for war/macro/regulation events
  └─► Determine macro state (risk_on vs risk_off)

STEP 3: Technical Analysis
  └─► Calculate EMA20/EMA50/EMA200
  └─► Detect trend (UPTREND / DOWNTREND / SIDEWAYS)
  └─► Calculate RSI, ATR, momentum
  └─► Find support/resistance
  └─► Volume Profile: identify LVN (support) and HVN (resistance)

STEP 4: Market Context
  └─► Assess BTC strength/weakness
  └─► Apply BTC > ETH > ALT hierarchy
  └─► Determine valid trading answers per macro state

STEP 5: Reasoning & Decision
  └─► Apply 3-phase framework:
        未动 (no trigger)  → NO TRADE
        冲击 (shock)      → WAIT
        确认 (confirmation) → EXECUTE or SKIP
  └─► Check Execution Gate:
        ✓ structure_break?
        ✓ macro_aligned?
        ✓ invalidation_clear?
        ✓ RR >= 1.5?
  └─► Generate standardized trade plan

STEP 6: Position Sizing
  └─► Base risk = equity × 1%
  └─► Apply position multiplier (environment-based)
  └─► Calculate stop distance
  └─► Determine max leverage (major: -0.5% buffer, small: -1.5%)
  └─► Open in tranches: 50% / 30% / 20%
```

---

## Position Sizing Rules

### Risk Environment → Position Multiplier

| Risk Level | Trigger | pos_mult |
|------------|---------|----------|
| LOW | No news + normal volatility + clear structure | 1.0x–1.2x |
| MODERATE | General news OR elevated volatility | 0.7x–1.0x |
| HIGH | War/CPI/FOMC/SEC OR ATR ≥ 1.5x OR structure broken | 0.3x–0.5x |

### Leverage Formula

```
Max Leverage = Stop Distance − Liquidation Buffer
  Major coins (BTC, ETH):  − 0.5%
  Small caps (alts):        − 1.5%
```

### Gate Checks

```
Gate A: Stop < 0.7% in HIGH risk → pos_mult ≤ 0.3
Gate B: Position > 100% of equity (training) → cap at 100%
```

---

## Asset Selection Rules

> **Not "pick the strongest." Pick "hardest to kill.**

```
BTC > ETH > ALT
```

| Macro | BTC Status | Valid Answers | Forbidden |
|-------|-----------|-------------|-----------|
| risk_off | weakness | BTC SHORT, NO TRADE | ALT LONG |
| risk_off | neutral | BTC SHORT, NO TRADE | ALT LONG |
| risk_on | any | BTC LONG, ETH LONG, ALT LONG | — |

---

## CLI Commands

```bash
# Market analysis
python -m src.cli analyze --market futures       # Full market analysis (futures data)
python -m src.cli analyze --market spot          # Full market analysis (spot data)

# Intraday scanner
python -m src.cli scan --symbols BTCUSDT,ETHUSDT,SOLUSDT --market futures

# Generate trade plan
python -m src.cli trade --symbol BTCUSDT --direction BUY --risk-level MODERATE --equity 10000 --market futures

# Backtest
python -m src.cli backtest \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT \
  --start 2025-01-01 --end 2026-01-01 \
  --market futures \
  --risk 1.0 --leverage 50 --pos-mult 1.0

# Log feedback
python -m src.cli feedback \
  --date 2026-03-21 \
  --analysis "Said no trade" \
  --reality "BTC dropped 5%" \
  --correction "Should have shorted"
```

---

## Project Structure

```
src/
├── cli.py                  # CLI entry point
├── config.py               # All configuration constants
├── analysis/
│   ├── indicators.py       # EMA, RSI, ATR, SMA, momentum, S/R
│   ├── volume_profile.py    # LVN/HVN, multi-TF resonance
│   └── market_context.py   # Risk classification, macro state
├── data/
│   ├── binance_client.py   # Binance spot/futures OHLCV
│   ├── coingecko_client.py # Global metrics, Fear & Greed
│   ├── news_fetcher.py     # News, announcements
│   ├── coin_tiers.py       # Tier system + symbol normalization
│   ├── llm_client.py      # LLM-powered analysis
│   └── okx_client.py      # OKX futures data
├── execution/
│   ├── decision_engine.py  # 3-phase trading framework
│   ├── position_sizer.py   # Risk-based position sizing
│   └── trade_plan.py      # Standardized trade output
├── backtest/
│   ├── engine.py          # BacktestEngine + MultiCoinBacktester
│   ├── strategy.py         # WorkflowStrategy (main)
│   ├── time_based.py       # Time-iterated backtester
│   └── data_client.py      # Unified historical data fetching
└── storage/
    ├── daily_logger.py     # Daily analysis persistence
    └── feedback_logger.py  # Feedback & learning log
```

---

## Configuration

Key settings in `src/config.py`:

| Setting | Value | Description |
|---------|-------|-------------|
| `DEFAULT_DATA_SOURCE` | `"futures"` | Data source: futures or spot |
| `BINANCE_FUTURES_URL` | `fapi.binance.com` | Futures API base |
| `DEFAULT_RISK_PCT` | `1%` | Max risk per trade |
| `POSITION_CAP_TRAINING` | `1.0x equity` | Max position size |
| `RISK_MULTIPLIERS` | LOW/MOD/HIGH | Position multipliers |
| `SMALL_STOP_THRESHOLD` | `0.7%` | Gate A trigger |

---

## Running Backtests

```python
from src.backtest import run_backtest, StrategyConfig

config = StrategyConfig(
    base_risk_pct=0.01,  # 1% risk
    max_leverage=50,
    pos_mult=1.0,
    min_rr=1.5,
)

result = run_backtest(
    symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    start_date="2025-01-01",
    end_date="2026-03-01",
    initial_equity=10000,
    config=config,
    market="futures",  # or "spot"
)

print(result["output"])
```

---

## Requirements

```
requests>=2.28.0
```

Install:
```bash
pip install -r requirements.txt
```

---

## Data Sources

| Source | Use |
|--------|-----|
| Binance fapi | Futures OHLCV (default) |
| Binance api | Spot OHLCV |
| CoinGecko | Global market metrics |
| alternative.me | Fear & Greed Index |
| CryptoPanic | News aggregation |
| OKX api | Optional futures backup |
