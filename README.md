# Plutus Trading System

A systematic cryptocurrency trading platform built in Python. From pure rule-based analysis to a **Multi-Agent Mixture of Experts (MoE) Trading Engine** with self-healing memory. Plutus combines lightning-fast quantitative mathematics with deep LLM reasoning, behaving like a fully staffed institutional quantitative hedge fund.

---

## Version History

| Version | Name | Core Innovation |
|---------|------|----------------|
| V1 | Pure Rule Engine | Technical indicators, 3-phase decision framework, position sizing |
| V2 | Hybrid Rule + LLM | LLM Macro Risk Officer execution gate, volatility shield |
| V3 | MoE Trading Floor | Chronos Engine, VanguardScanner, 3 LLM personas, DynamicAllocator |
| V3.1 | Reflexion Memory Matrix | SQLite RAG, post-mortem reflexion, self-healing personas |
| V4 | Operation BLACK SWAN PREP | Risk infrastructure, KillSwitch, RiskGuard, Docker, TimescaleDB |
| V4.1 | OMNIPOTENCE | Active Command Console, forensics dashboard, wired live pipeline |
| V4.2 | Complete System Audit | Full 10-agent architecture review, execution wiring, fixes |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           PLUTUS V4 — WIRES PIPELINE                            │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────────┐      │
│  │                    LIVE TRADING PATH (wired in V4.1)                │      │
│  │                                                              │      │
│  │  BINANCE WEBSOCKET                                             │      │
│  │       ↓                                                        │      │
│  │  BinanceConnector._on_message() → XADD → Redis Stream            │      │
│  │       ↓                                                        │      │
│  │  IdempotentScannerWorker (asyncio.create_task in plutus_engine)     │      │
│  │       ↓ Anomaly detected                                         │      │
│  │       ↓ PUBLISH → Redis Pub/Sub                                  │      │
│  │       ↓                                                        │      │
│  │  LiveExecutionNode._on_anomaly()                                  │      │
│  │       ↓                                                        │      │
│  │  HybridWorkflowStrategy.analyze_symbol()                         │      │
│  │  DecisionEngine.check_execution_gate()  ← 3-phase framework        │      │
│  │  RiskGuard.check_all()  ← 9 sequential safety checks             │      │
│  │       ↓                                                        │      │
│  │  SmartRouter.route()  ← TWAP / VWAP / LimitQueue / Market        │      │
│  │       ↓                                                        │      │
│  │  BinanceExecutor.place_order()  ← real Binance Futures API         │      │
│  │       ↓                                                        │      │
│  │  RiskGuard.update_position_from_fill()                            │      │
│  │       ↓                                                        │      │
│  │  TimescaleDB write (OHLCV, fills, portfolio snapshots)           │      │
│  │       ↓                                                        │      │
│  │  Feedback → MetaLearning → MemoryBank (lesson learned)           │      │
│  └──────────────────────────────────────────────────────────────────┘      │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────────┐      │
│  │                    BACKTEST PATH (ChronosEngine)                    │      │
│  │                                                              │      │
│  │  VanguardScanner.scan(df)  ← vectorised NumPy/Pandas             │      │
│  │       ↓ wakes only on anomaly (99%+ of candles filtered)          │      │
│  │  3 LLM Personas (DRY_RUN = deterministic mock, LIVE = real LLM)   │      │
│  │       ↓                                                        │      │
│  │  MoEWeighter + DynamicAllocator  ← Sortino-softmax allocation    │      │
│  │       ↓                                                        │      │
│  │  _simulate_trade_outcome()  ← close-only exit, RiskGuard in loop │      │
│  │       ↓                                                        │      │
│  │  ReflexionEvolver  ← lessons → MemoryBank (SQLite RAG)           │      │
│  │  GeneticOptimizer  ← evolves ScannerConfig via params.py          │      │
│  └──────────────────────────────────────────────────────────────────┘      │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## What's New in V4 — Wired Pipeline

### V4.2 — Complete System Audit (this release)

- **10-agent architecture audit** covering: execution, risk, data layer, backtesting, CLI, dashboard, security, infrastructure, concurrency, state management, and parameter governance
- **Execution layer fully wired**: SmartRouter → BinanceExecutor → RiskGuard → TimescaleDB all connected
- **LiveExecutionNode** (`src/execution/__main__.py`) with full 6-connection pipeline
- **IdempotentScannerWorker** wired into PlutusEngine as asyncio task
- **Non-blocking RateLimiter** (Semaphore, no lock-during-sleep)
- **RiskGuard** integrated into backtest loop
- **13 strategy corpses** archived to `backups/strategies_archive/`
- **Duplicate PairsTrader class** eliminated; second class renamed SpreadTrader
- **Vectorized RSI** replacing Python for-loop
- **Incremental EMA cache** in scanner
- **Docker infrastructure** fixed (COPY paths, health checks, init-scripts, 5-service compose)
- **params.py** single source of truth for all GA-evolvable scanner parameters

### V4.1 — Active Command Console + Forensics Dashboard

- **Dashboard** (`src/dashboard/app.py`): real-time backtest forensics with live terminal streaming, equity curve, drawdown, win rate, heatmaps
- **Dry-run banner**: synthetic results shown with explicit warning
- **Scanner streaming**: Binance WebSocket + Redis pipeline
- **PLUTUS_API_KEY** bearer-token auth on FastAPI endpoints

### V4 — Operation BLACK SWAN PREP

- **RiskGuard** (`src/execution/risk_limits.py`): 9 sequential checks — kill switch, global drawdown, fat-finger notional, correlated beta exposure, leverage circuit, session loss, liquidation buffer, equity floor, black swan
- **Gate A/B** enforcement inside `calculate_position_size()`
- **Correlation gate**: ALT longs blocked when BTC in downtrend or risk_level == HIGH
- **Docker compose** with 5 services: plutus_engine, execution_node, scanner, timescaledb, redis
- **TimescaleDB** hypertable schema for OHLCV, trades, scanner_events, portfolio_snapshots

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/Dingding-leo/plutus-trading.git
cd plutus-trading

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env with your API keys (LLM_API_KEY, BINANCE_API_KEY, etc.)

# 4. Run the dashboard
streamlit run src/dashboard/app.py

# 5. Run a backtest
python -m src.cli backtest --symbols BTCUSDT --start 2022-01-01 --end 2024-12-31 --equity 10000

# 6. Run live trading (Docker)
cd docker && docker compose up -d
```

---

## CLI Commands

```bash
# ── Backtest ────────────────────────────────────────────────
python -m src.cli backtest \
  --symbols BTCUSDT,ETHUSDT \
  --start 2022-01-01 --end 2024-12-31 \
  --equity 10000 \
  --v3-chronos --v3-mode dry_run

# ── Scan ─────────────────────────────────────────────────
python -m src.cli scan --symbols BTCUSDT,ETHUSDT,SOLUSDT --market futures

# ── Trade Plan ───────────────────────────────────────────
python -m src.cli trade --symbol BTCUSDT --direction BUY --risk-level MODERATE --equity 10000

# ── Analyze ──────────────────────────────────────────────
python -m src.cli analyze --save

# ── Feedback ────────────────────────────────────────────
python -m src.cli feedback --date 2026-03-22
```

---

## Project Structure

```
src/
├── cli/                    # CLI refactored into commands/
│   ├── commands/
│   │   ├── analyze.py       # Market analysis
│   │   ├── backtest.py      # ChronosBacktester
│   │   ├── feedback.py      # Lesson logging
│   │   ├── scan.py         # VanguardScanner
│   │   └── trade.py        # Trade plan + validation
│   └── main.py             # Entry point
│
├── data/
│   ├── binance_client.py   # Binance OHLCV + local data lake
│   ├── coingecko_client.py  # Global market metrics
│   ├── coin_tiers.py       # Tier system + symbol normalisation
│   ├── llm_client.py        # LLM client (lazy-loaded)
│   ├── memory.py            # SQLite MemoryBank RAG
│   ├── personas.py          # 3 LLM personas + reflexion
│   ├── scanner.py           # VanguardScanner (vectorised)
│   └── streams/            # WebSocket clients
│       ├── binance_websocket.py  # Binance real-time stream
│       └── glassnode.py          # Glassnode metrics
│
├── analysis/
│   ├── indicators.py       # EMA, RSI, ATR, SMA, momentum
│   ├── volume_profile.py    # LVN/HVN, multi-TF resonance
│   └── market_context.py    # Risk classification, macro state
│
├── models/
│   ├── meta_learning.py    # MoEWeighter, GeneticOptimizer,
│   │                         # ReflexionEvolver, DynamicAllocator
│   └── params.py           # Single source for GA-evolvable params
│
├── execution/
│   ├── __main__.py         # LiveExecutionNode (live trading service)
│   ├── exchanges/
│   │   └── binance_executor.py  # Binance Futures execution
│   ├── order_router.py     # SmartRouter: TWAP/VWAP/LimitQueue
│   ├── portfolio_matrix.py # SpreadTrader, RiskManager, CorrelationEngine
│   ├── position_sizer.py   # Gate A/B, calculate_max_leverage
│   ├── risk_limits.py      # RiskGuard (9 checks)
│   ├── trade_plan.py       # Standardised trade output + validation
│   └── risk/               # Risk calculation utilities
│
├── engine/                  # Live infrastructure
│   ├── __main__.py         # Docker entry point for plutus_engine
│   ├── server.py           # FastAPI + Redis pub/sub
│   ├── scanner_worker.py    # IdempotentScannerWorker
│   ├── realtime_pipeline.py  # XADD → XREADGROUP pipeline
│   └── scanner_cli.py       # BinanceConnector → XADD producer
│
├── backtest/
│   ├── chronos_engine.py    # ChronosBacktester (orchestrator)
│   ├── engine.py            # BacktestEngine (V1)
│   ├── strategy.py          # WorkflowStrategy
│   ├── simple_fetch.py     # Historical data fetcher
│   └── chronos/             # Chronos sub-modules
│
├── config.py                # Global constants
└── dashboard/
    ├── app.py              # Streamlit forensics dashboard
    └── data_loader.py       # Dashboard data loader
```

---

## Risk Guard (9 Sequential Checks)

```
check_all() order of evaluation:
  [0] kill_switch           — permanent halt after session loss
  [1] global_drawdown      — halt when equity < 92% of peak
  [2] fat_finger_notional  — max notional per symbol (LIVE $5k, DRY $10k)
  [3] correlated_exposure   — crypto beta > 50% when BTC downtrend
  [4] leverage_circuit     — max leverage by risk level (5-15x)
  [5] session_loss         — hard stop at -5% session loss
  [6] liquidation_buffer   — 0.5% major / 1.5% small cap buffer
  [7] absolute_equity_floor — permanent halt below $1,000
  [8] black_swan          — flatten on -5% intraday drawdown
```

---

## Docker Services

```bash
cd docker
docker compose up -d

# Services:
#   plutus_engine :8000      FastAPI + IdempotentScannerWorker
#   execution_node            LiveExecutionNode (BinanceExecutor)
#   scanner                  BinanceConnector → Redis stream
#   timescaledb :5432        OHLCV, fills, portfolio snapshots
#   redis :6379               Pub/sub + orderbook stream (MAXLEN 50k, noeviction)
```

---

## Requirements

```
# Core
requests>=2.28.0
pandas>=2.0.0
numpy>=1.26.0
aiohttp>=3.9.0
redis>=5.0.0
asyncpg>=0.29.0

# Analysis
ta>=0.11.0           # Technical indicators
scikit-learn>=1.4.0  # ReflexionEvolver TF-IDF

# LLM
openai>=1.0.0         # or anthropic>=0.20.0

# Infra
streamlit>=1.30.0
fastapi>=0.110.0
uvicorn>=0.27.0
python-dotenv>=1.0.0

pip install -r requirements.txt
```

---

## Data Sources

| Source | Use |
|--------|-----|
| Binance fapi v1 | Futures OHLCV (primary) |
| Binance spot api v3 | Spot OHLCV (fallback) |
| CoinGecko | Global market cap, fear & greed |
| Glassnode | On-chain metrics (OI, whale wallets, MVRV) |
| LLM Provider (Minimax/OpenAI) | Persona analysis, reflexion |
| TimescaleDB | OHLCV, fills, scanner events, portfolio snapshots |
| Redis | Real-time orderbook stream, pub/sub signals |
