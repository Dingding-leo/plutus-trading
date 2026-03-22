# Plutus Trading System — Developer Guide

> Last Updated: 2026-03-22 (V4.2)

---

## Quick Start

```bash
# Run a backtest
python -m src.cli backtest --symbols BTCUSDT,ETHUSDT --start 2022-01-01 --end 2024-12-31 --equity 10000

# Run market analysis
python -m src.cli analyze --save

# Run intraday scanner
python -m src.cli scan --symbols BTCUSDT,ETHUSDT,SOLUSDT

# Generate trade plan
python -m src.cli trade --symbol BTCUSDT --direction BUY --risk-level MODERATE --equity 10000

# Log feedback
python -m src.cli feedback

# Get help
python -m src.cli --help
python -m src.cli backtest --help

# Run Docker (live trading)
cd docker && docker compose up -d
```

---

## System Overview

Plutus V4 is a crypto futures trading system with two operational modes:

1. **Chronos Backtest** — Event-driven historical simulation using VanguardScanner (vectorised NumPy/Pandas), 3 LLM personas, MoEWeighter, DynamicAllocator, GeneticOptimizer, and SQLite MemoryBank RAG.
2. **Live Trading** — Wired 6-connection pipeline: Binance WebSocket → Redis Stream → IdempotentScannerWorker → LiveExecutionNode → SmartRouter → BinanceExecutor → RiskGuard.

Key capabilities:
- Trades 45+ coins (Binance Futures)
- Multi-timeframe analysis (5m, 15m, 30m, 1h, 4h)
- LLM-augmented decision making with self-healing memory
- Institutional-grade risk management (9 sequential RiskGuard checks)
- Docker-based deployment with TimescaleDB + Redis

---

## Project Structure

```
src/
├── cli/                     # CLI refactored into command modules
│   ├── commands/
│   │   ├── analyze.py       # Market analysis
│   │   ├── backtest.py      # ChronosBacktester
│   │   ├── feedback.py      # Lesson logging
│   │   ├── scan.py         # VanguardScanner
│   │   └── trade.py        # Trade plan + validation
│   ├── main.py              # Entry point
│   └── utils.py             # Shared utilities
│
├── config.py                 # Global constants (PROJECT_ROOT, LEVERAGE_BUFFERS, RISK_MULTIPLIERS)
│
├── data/
│   ├── binance_client.py   # Binance OHLCV + local CSV data lake
│   ├── coingecko_client.py  # Global market metrics
│   ├── coin_tiers.py       # Tier system + symbol normalisation
│   ├── llm_client.py       # LLM client (lazy-loaded via LazyLLMClientProxy)
│   ├── memory.py            # SQLite MemoryBank RAG (~/.plutus/memory.db)
│   ├── personas.py          # 3 LLM personas + reflexion
│   ├── scanner.py           # VanguardScanner — vectorised NumPy/Pandas anomaly detector
│   └── streams/             # Real-time data sources
│       ├── binance_websocket.py  # Binance WebSocket → XADD pipeline
│       └── glassnode.py          # Glassnode on-chain metrics
│
├── analysis/
│   ├── indicators.py       # EMA, RSI, ATR, SMA, momentum, volatility
│   ├── volume_profile.py   # LVN/HVN, multi-TF resonance
│   └── market_context.py  # Risk classification, macro state
│
├── models/                  # Meta-learning + parameter evolution
│   ├── meta_learning.py   # MoEWeighter, GeneticOptimizer, ReflexionEvolver,
│   │                       DynamicAllocator, CorrelationEngine
│   └── params.py          # Single source of truth for GA-evolvable parameters
│
├── execution/              # Order execution + risk management
│   ├── __main__.py       # LiveExecutionNode (live trading service)
│   ├── exchanges/
│   │   └── binance_executor.py  # Binance Futures execution
│   ├── order_router.py   # SmartRouter: TWAP / VWAP / LimitQueue
│   ├── portfolio_matrix.py # SpreadTrader, RiskManager, CorrelationEngine
│   ├── position_sizer.py # Gate A/B, calculate_max_leverage
│   ├── risk_limits.py    # RiskGuard (9 sequential checks)
│   ├── trade_plan.py    # Standardised trade output + validation
│   └── risk/            # Risk utilities
│
├── engine/                # Live infrastructure
│   ├── __main__.py       # Docker entry point for plutus_engine
│   ├── server.py         # FastAPI + Redis pub/sub (plutus_engine)
│   ├── scanner_cli.py     # BinanceConnector → XADD Redis stream producer
│   ├── scanner_worker.py  # IdempotentScannerWorker (asyncio.create_task in server.py)
│   └── realtime_pipeline.py # XADD → XREADGROUP pipeline
│
├── backtest/               # Historical simulation
│   ├── chronos/          # Chronos sub-modules
│   ├── chronos_engine.py # ChronosBacktester (orchestrator)
│   ├── engine.py         # BacktestEngine (V1/V2)
│   ├── strategy.py        # WorkflowStrategy
│   └── simple_fetch.py    # Historical data fetcher
│
└── dashboard/
    ├── app.py            # Streamlit forensics dashboard
    └── data_loader.py    # Dashboard data loader

docker/
├── docker-compose.yml     # 5-service topology: plutus_engine, execution_node,
│                          #   scanner, timescaledb, redis
├── plutus_engine.Dockerfile
├── execution_node.Dockerfile
├── scanner.Dockerfile
├── Dockerfile.quant_worker
├── init-scripts/001_init.sql  # TimescaleDB schema
└── nginx.conf
```

---

## CLI Commands

### backtest

```bash
python -m src.cli backtest \
    --symbols BTCUSDT,ETHUSDT \
    --start 2022-01-01 --end 2024-12-31 \
    --equity 10000 \
    --v3-chronos --v3-mode dry_run
```

Options:
- `--symbols`: Comma-separated symbols (default: BTCUSDT)
- `--start` / `--end`: Date range (YYYY-MM-DD)
- `--equity`: Initial equity (default: 10000)
- `--v3-chronos`: Enable Chronos V3 engine
- `--v3-mode`: `dry_run` (mock personas, no API cost) or `live` (real LLM calls)
- `--v3-min-confidence`: Minimum blended confidence to execute (0-100)

### analyze

```bash
python -m src.cli analyze --market futures --save
```

Runs full market context analysis and optionally saves to daily log.

### scan

```bash
python -m src.cli scan --symbols BTCUSDT,ETHUSDT,SOLUSDT --market futures
```

Vectorised scanner across multiple timeframes. Outputs anomaly events only (99%+ of candles filtered as noise).

### trade

```bash
python -m src.cli trade --symbol BTCUSDT --direction BUY --risk-level MODERATE --equity 10000
```

Generates and validates a trade plan. RiskGuard gates applied.

### feedback

```bash
python -m src.cli feedback --date 2026-03-22
```

Logs feedback for MemoryBank RAG.

---

## Trading Rules (from CLAUDE.md)

### Position Sizing

```
base_risk_$  = equity × 1%
effective_risk_$ = base_risk_$ × pos_mult
max_position_value = effective_risk_$ / stop_distance%
```

**Gate A (Small Stop Penalty):**
```
if stop_distance% < 0.5% AND risk_env == HIGH:
    pos_mult = min(pos_mult, 0.3)
```

**Gate B (Position Cap):**
```
max_position_value ≤ equity × 0.7 (training) or equity × 1.0 (advanced)
```

### Risk Environment → Position Multiplier

| Level | Trigger | pos_mult |
|-------|---------|-----------|
| LOW | No news + normal volatility + clear structure | 1.0x–1.2x |
| MODERATE | General news OR elevated volatility | 0.7x–1.0x |
| HIGH | CPI/FOMC/war/regulation OR ATR ≥ 1.5x OR structure broken | 0.3x–0.5x |

### RiskGuard — 9 Sequential Checks

```
[0] kill_switch          — permanent halt after session loss
[1] global_drawdown      — halt when equity < 92% of peak
[2] fat_finger_notional  — max notional per symbol ($5k live, $10k dry)
[3] correlated_exposure   — crypto beta > 50% when BTC downtrend
[4] leverage_circuit    — max leverage by risk level (5x-15x)
[5] session_loss         — hard stop at -5% session loss
[6] liquidation_buffer   — 0.5% major / 1.5% small cap buffer
[7] absolute_equity_floor — permanent halt below $1,000 equity
[8] black_swan          — flatten on -5% intraday drawdown
```

### Asset Selection

```
BTC > ETH > ALT
```

If BTC shows strength → trade BTC. If macro == risk_off and BTC weakness → NO TRADE or BTC SHORT. ALT LONG forbidden in risk_off.

### Execution Gate (DecisionEngine)

```
PHASE 1: No trigger → NO TRADE
PHASE 2: Trigger but no confirmation → WAIT
PHASE 3: Trigger + confirmation → CHECK EXECUTION GATE → EXECUTE or SKIP
```

Gate conditions: structure_break AND macro_aligned AND invalidation_clear AND RR >= 1.5 (including extension).

### Stop Loss Rules

- Major coins: liquidation buffer = 0.5%
- Small caps: liquidation buffer = 1.5%
- Stop must be ≥ 0.5% OR pos_mult capped at 0.3
- Gate A penalty threshold = 0.5%
- Open in tranches: 50% / 30% / 20%

---

## Key Classes

### ChronosBacktester

```python
from src.backtest.chronos_engine import ChronosBacktester

engine = ChronosBacktester(
    initial_equity=10000,
    mode=BacktestMode.DRY_RUN,
)
results = engine.run_backtest(symbols=["BTCUSDT", "ETHUSDT"])
```

Returns: equity curve, trade log, forensics metrics, session summary.

### VanguardScanner

```python
from src.data.scanner import VanguardScanner, ScannerConfig

scanner = VanguardScanner()
events = scanner.scan(df)  # df: OHLCV DataFrame
# Returns list of ScannerEvent with anomaly_type, confidence, context_data
```

### RiskGuard

```python
from src.execution.risk_limits import RiskGuard

guard = RiskGuard(initial_capital=10000, mode="dry_run")
passed, msg = guard.check_all(
    proposed_symbol="BTCUSDT",
    proposed_notional=5000,
    proposed_leverage=10,
    btc_trend="UPTREND",
)
```

### BinanceExecutor

```python
from src.execution.exchanges.binance_executor import BinanceExecutor

exec = BinanceExecutor(test_mode=False)  # live mode = real Binance API
exec.record_fill(order_id, symbol, side, quantity, fill_price)
position = exec.get_position("BTCUSDT")
```

### LiveExecutionNode

```python
# Entry point: python -m src.execution
# Wires: scanner → decision → risk → execution → fill tracking
```

### SmartRouter

```python
from src.execution.order_router import SmartRouter

router = SmartRouter(binance_exec=BinanceExecutor())
result = router.route(
    intent={"aggressive_fill": 1.0},
    symbol="BTCUSDT",
    side="BUY",
    notional=5000,
    stop_loss=49000,
    take_profit=52000,
)
```

---

## Genetic Algorithm — params.py

```python
from src.models.params import GAConfig, GAIndividual

GAConfig bounds:
  sweep_threshold:        [0.0001, 0.05]
  vol_squeeze_atr_mult:   [0.05, 3.0]
  deviation_z_score:         [0.5, 5.0]
  min_confidence_threshold:  [1, 100]
  max_positions:            [1, 5]

GA fitness: Sharpe * (1 - max_drawdown_weight)
Hard constraints: sweep_threshold >= 0.005 (0.5%)
```

---

## Docker Services

```bash
cd docker
docker compose up -d

# Services:
#   plutus_engine :8000  FastAPI + IdempotentScannerWorker
#   execution_node          LiveExecutionNode
#   scanner                 BinanceConnector → XADD Redis
#   timescaledb :5432       Hypertables for OHLCV, fills, scanner events
#   redis :6379             Pub/sub + orderbook stream (MAXLEN 50k, noeviction)
```

---

## Requirements

```
# Core
requests>=2.28.0
pandas>=2.0.0
numpy>=1.26.0

# Async I/O
aiohttp>=3.9.0
redis>=5.0.0
asyncpg>=0.29.0

# LLM
openai>=1.0.0    # or anthropic>=0.20.0

# Infra
fastapi>=0.110.0
uvicorn>=0.27.0
streamlit>=1.30.0

# ML
scikit-learn>=1.4.0   # ReflexionEvolver TF-IDF
ta>=0.11.0            # Technical indicators
python-dotenv>=1.0.0

pip install -r requirements.txt
```

---

## Data Sources

| Source | Use |
|--------|-----|
| Binance fapi v1 | Futures OHLCV (primary, max 1500 candles/request) |
| Binance spot v3 | Spot OHLCV (fallback) |
| CoinGecko | Global market cap, fear & greed |
| Glassnode | On-chain metrics (OI, whale wallets, MVRV) |
| LLM Provider (Minimax/OpenAI) | Persona analysis, reflexion |
| TimescaleDB | OHLCV, fills, scanner events, portfolio snapshots |
| Redis | Real-time orderbook stream, pub/sub signals |
| SQLite (~/.plutus/memory.db) | Lesson persistence (MemoryBank RAG) |

---

## Common Issues

### 1. No trades generated
- Check `--v3-min-confidence` threshold (try lowering to 30)
- Verify Binance API credentials are set
- Ensure `--v3-mode` is correct (`dry_run` vs `live`)

### 2. High drawdown
- Reduce `--v3-equity` (try 5000)
- Increase `--v3-min-confidence`
- Run with `--risk-level MODERATE` instead of `LOW`

### 3. LLM not called in backtest
- `--v3-mode` must be `live` to call the LLM API. `dry_run` uses deterministic mock personas.
- Check `LLM_API_KEY` and `LLM_BASE_URL` are set in `.env`

### 4. Import errors
- Ensure virtual environment is activated: `source .venv/bin/activate`
- `pip install -r requirements.txt` has been run

### 5. Docker won't start
- `docker compose config` to validate compose file
- `docker compose logs plutus_engine` to see startup errors
- Check `.env` has all required variables set

### 6. TimescaleDB schema not created
- `docker compose logs timescaledb` — check if init scripts ran
- Verify `docker/init-scripts/001_init.sql` exists

---

## Files Reference

| File | Purpose |
|------|---------|
| `src/cli/main.py` | CLI entry point |
| `src/cli/commands/backtest.py` | ChronosBacktester invocation |
| `src/config.py` | Global constants (PROJECT_ROOT, LEVERAGE_BUFFERS, RISK_MULTIPLIERS, SMALL_STOP_THRESHOLD) |
| `src/data/scanner.py` | VanguardScanner — vectorised anomaly detector |
| `src/data/memory.py` | SQLite MemoryBank (~/.plutus/memory.db) |
| `src/data/personas.py` | 3 LLM personas + reflexion |
| `src/data/binance_client.py` | Binance OHLCV + local CSV lake |
| `src/execution/risk_limits.py` | RiskGuard (9 checks) |
| `src/execution/position_sizer.py` | Gate A/B, calculate_max_leverage |
| `src/execution/order_router.py` | SmartRouter: TWAP/VWAP/LimitQueue |
| `src/execution/exchanges/binance_executor.py` | Binance Futures execution |
| `src/models/params.py` | GA-evolvable parameter bounds |
| `src/backtest/chronos_engine.py` | ChronosBacktester orchestrator |
| `src/engine/server.py` | FastAPI + IdempotentScannerWorker |
| `src/engine/scanner_worker.py` | IdempotentScannerWorker consumer |
| `src/engine/realtime_pipeline.py` | XADD → XREADGROUP pipeline |
| `src/dashboard/app.py` | Streamlit forensics dashboard |
| `docker/init-scripts/001_init.sql` | TimescaleDB schema |

---

## Architecture: Live Pipeline (Connections)

```
Binance WebSocket → BinanceConnector._on_message()
     ↓ XADD
Redis Stream (plutus:scanner:stream)
     ↓ XREADGROUP
IdempotentScannerWorker.run()
     ↓ PUBLISH
Redis Pub/Sub (scanner.events)
     ↓
LiveExecutionNode._on_anomaly()
     ↓
HybridWorkflowStrategy.analyze_symbol()
     ↓
DecisionEngine.check_execution_gate()
     ↓
RiskGuard.check_all()  ← 9 sequential checks
     ↓
SmartRouter.route()
     ↓
BinanceExecutor.place_order()
     ↓
RiskGuard.update_position_from_fill()
     ↓
TimescaleDB write (OHLCV, fills, portfolio snapshots)
```
