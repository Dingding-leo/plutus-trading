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

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLI (src/cli.py)                         │
│              analyze | scan | trade | backtest                   │
│              V1/V2/V3 modes — fully backward compatible         │
└────────────────────────┬─────────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                              ▼
┌──────────────────────────┐  ┌────────────────────────────────┐
│   PLUTUS V1/V2 LAYER     │  │    PLUTUS V3 CHRONOS ENGINE    │
│                          │  │                                │
│  indicators.py           │  │  ┌──────────────────────────┐  │
│  volume_profile.py       │  │  │ VanguardScanner (Layer 1) │  │
│  market_context.py       │  │  │  Vectorised numpy/pandas  │  │
│  workflow_analyzer.py     │  │  │  3 anomaly triggers:     │  │
│  decision_engine.py      │  │  │  - LIQUIDITY_SWEEP       │  │
│  position_sizer.py       │  │  │  - EXTREME_DEVIATION     │  │
│  llm_client.py           │  │  │  - VOLATILITY_SQUEEZE    │  │
│  (Macro Risk Officer)    │  │  └──────────┬───────────────┘  │
│  hybrid_strategy.py       │  │             │ "wake" only on anomaly
│  (Execution Gate)         │  │             ▼                   │
└──────────────────────────┘  │  ┌──────────────────────────┐  │
                              │  │ 3 LLM Personas (Layer 2) │  │
                              │  │  - SMC_ICT               │  │
                              │  │  - ORDER_FLOW            │  │
                              │  │  - MACRO_ONCHAIN         │  │
                              │  │  + RAG lesson injection  │  │
                              │  │  + reflexion on loss     │  │
                              │  └──────────┬───────────────┘  │
                              │             │                    │
                              │             ▼                    │
                              │  ┌──────────────────────────┐  │
                              │  │ DynamicAllocator (Layer 3)│  │
                              │  │ Softmax over fitness:    │  │
                              │  │ (Sortino*WinRate) /      │  │
                              │  │ (1+Turnover*Penalty)     │  │
                              │  └──────────┬───────────────┘  │
                              │             │                    │
                              │             ▼                    │
                              │  ┌──────────────────────────┐  │
                              │  │ MemoryBank (V3.1)        │  │
                              │  │ SQLite RAG — lessons     │  │
                              │  │ persisted at ~/.plutus/  │  │
                              │  └──────────────────────────┘  │
                              └────────────────────────────────┘
```

---

## What's New in V3: The Autonomous AI Trading Floor

### 1. The Chronos Engine (Event-Driven Wakelock)

Traditional LLM backtesting is bottlenecked by cost — processing every hourly candle with an LLM is prohibitively expensive and causes hallucinations from data overload. Plutus V3 introduces the **Chronos Engine**.

- **Vanguard Scanner (Layer 1):** A blazing-fast, vectorised NumPy/Pandas radar that scans years of historical data in seconds. It filters out 99%+ of market noise using pure math — no LLM needed.
- **Event-Driven Wakelock:** The costly LLM API is strictly dormant until the Scanner detects a severe mathematical anomaly. Only then does the engine "wake up" the AI committee.
- **Result:** Backtesting that used to cost thousands of dollars in API fees now costs pennies, with drastically higher signal accuracy.

**Anomaly triggers:**

| Trigger | Definition | Signal |
|---------|-----------|--------|
| `LIQUIDITY_SWEEP` | Price wicks below 20-bar rolling low, closes back above | BULLISH/BEARISH |
| `EXTREME_DEVIATION` | Price > 2 ATR from EMA50 AND RSI < 35 or > 65 | BULLISH/BEARISH |
| `VOLATILITY_SQUEEZE` | BB Width ≤ 5% above 20-bar rolling minimum | BULLISH/BEARISH/NEUTRAL |

**Scanner thresholds** (all tunable via `ScannerConfig`):
- `sweep_lookback = 20` bars
- `deviation_atr_multiplier = 2.0`
- `rsi_oversold = 35 / rsi_overbought = 65`
- `squeeze_lookback = 20` bars, `squeeze_threshold_pct = 5%`

### 2. Mixture of Experts (MoE) Personas

When an anomaly fires, a committee of three distinct, deeply specialised LLM personas independently analyses the setup. No single persona dominates — capital is allocated algorithmically based on recent performance.

| Persona | Specialty | Key Indicators |
|---------|-----------|---------------|
| **`SMC_ICT`** | Smart Money Concepts | Liquidity pools, FVGs, Market Structure Shifts, Order Blocks |
| **`ORDER_FLOW`** | Market microstructure | Open Interest, funding rates, liquidation clusters, volume delta |
| **`MACRO_ONCHAIN`** | Global macro + on-chain | ETF flows, whale wallets, MVRV, DXY, halving cycle regime |

**Response schema** (universal across all personas):
```json
{
  "thesis": "1-3 sentence reasoning",
  "direction": "LONG | SHORT | NEUTRAL",
  "confidence_score": 0-100,
  "recommended_leverage": 1-10,
  "_warnings": ["edge-case caveats"]
}
```

### 3. The Quant Evaluator (Dynamic Portfolio Manager)

Personas do not have equal power. A ruthless algorithmic Portfolio Manager evaluates their rolling performance using institutional metrics:

- **Fitness formula:** `(Sortino × WinRate) / (1 + Turnover × Penalty)`
- **Softmax allocation:** Capital is dynamically redistributed. If SMC_ICT is on a losing streak in chop, its weight drops toward `0.0` — ORDER_FLOW takes over seamlessly.
- **Lookback window:** Rolling 30-epoch window (configurable)
- **No LLM needed:** All math is pure Python/NumPy — zero API cost

### 4. The Reflexion Memory Matrix (V3.1 — Self-Healing RAG)

Plutus is not a static model — it learns from its own failures.

**Post-Mortem Engine:** When a trade loses > 1%, the responsible LLM persona is forced to output a **1-sentence strict rule** about what went wrong.

**SQLite Memory Bank** (`~/.plutus/memory.db`):
```sql
CREATE TABLE lessons (
  id           INTEGER PRIMARY KEY,
  timestamp    TEXT,
  persona      TEXT,       -- e.g. "SMC_ICT"
  anomaly_type TEXT,      -- e.g. "LIQUIDITY_SWEEP"
  pnl          REAL,       -- signed % loss
  thesis       TEXT,       -- what the persona believed
  lesson       TEXT        -- 1-sentence rule the LLM produced
);
CREATE INDEX idx_persona_anomaly ON lessons (persona, anomaly_type);
```

**Retrieval-Augmented Generation (RAG):** Before evaluating a *new* anomaly, each persona queries the Memory Bank for past lessons on that anomaly type. Those lessons are injected into the LLM system prompt as a hard constraint block — the model literally cannot repeat the same mistake twice.

---

## CLI Commands

```bash
# ── V1: Pure Rule-Based Engine ──────────────────────────────────
python3 -m src.cli backtest --symbols BTCUSDT,ETHUSDT

# ── V2: Hybrid Engine (LLM Macro Risk Officer) ───────────────────
python3 -m src.cli backtest --use-llm --llm-provider minimax --symbols BTCUSDT

# ── V3: Chronos Event-Driven MoE (DRY RUN — no LLM calls) ──────
python3 -m src.cli backtest --v3-chronos --v3-mode dry_run --symbols BTCUSDT

# ── V3: Chronos with real LLM personas (LIVE — API costs apply) ─
python3 -m src.cli backtest --v3-chronos --v3-mode live --symbols BTCUSDT

# ── V3: Tune scanner sensitivity ─────────────────────────────────
python3 -m src.cli backtest --v3-chronos --v3-mode dry_run \
  --symbols BTCUSDT --v3-equity 50000 --v3-min-confidence 50

# ── V1: Full market analysis ─────────────────────────────────────
python3 -m src.cli analyze --market futures

# ── V1: Intraday scanner ─────────────────────────────────────────
python3 -m src.cli scan --symbols BTCUSDT,ETHUSDT,SOLUSDT --market futures

# ── V1: Generate trade plan ──────────────────────────────────────
python3 -m src.cli trade --symbol BTCUSDT --direction BUY --risk-level MODERATE \
  --equity 10000 --market futures
```

### Backtest Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--v3-chronos` | `False` | Enable Chronos V3 engine (instead of V1/V2) |
| `--v3-mode` | `dry_run` | `dry_run` (mock personas, no API cost) or `live` (real LLM calls) |
| `--v3-equity` | `10000` | Initial equity for Chronos backtest |
| `--v3-min-confidence` | `40` | Minimum blended confidence to execute a trade (0–100) |
| `--use-llm` | `False` | Enable LLM Macro Risk Officer in V1/V2 backtest |
| `--llm-provider` | `minimax` | LLM provider for V2 (`minimax`, `openai`, etc.) |

---

## Project Structure

```
src/
├── cli.py                     # CLI entry point — routes V1/V2/V3 modes
├── config.py                  # All configuration constants
│
├── analysis/
│   ├── indicators.py          # EMA, RSI, ATR, SMA, momentum, volatility
│   ├── volume_profile.py      # LVN/HVN, multi-TF resonance
│   └── market_context.py      # Risk classification, macro state
│
├── data/
│   ├── binance_client.py      # Binance spot/futures OHLCV
│   ├── coingecko_client.py    # Global metrics, Fear & Greed
│   ├── coin_tiers.py          # Tier system + symbol normalisation
│   ├── llm_client.py          # LLM client + Macro Risk Officer (V2)
│   ├── personas.py             # 3 LLM personas + reflexion engine (V3)
│   ├── scanner.py              # VanguardScanner — anomaly detector (V3)
│   └── memory.py              # MemoryBank SQLite RAG store (V3.1)
│
├── execution/
│   ├── decision_engine.py     # 3-phase trading framework
│   ├── position_sizer.py      # Risk-based position sizing
│   └── trade_plan.py         # Standardised trade output
│
├── backtest/
│   ├── engine.py             # BacktestEngine + MultiCoinBacktester
│   ├── strategy.py            # WorkflowStrategy + HybridWorkflowStrategy
│   ├── hybrid_strategy.py     # V2 Execution Gate + Volatility Shield
│   ├── portfolio_manager.py   # DynamicAllocator + fitness math (V3)
│   ├── chronos_engine.py      # ChronosBacktester orchestrator (V3)
│   └── data_client.py         # Unified historical data fetching
│
└── storage/
    ├── daily_logger.py        # Daily analysis persistence
    └── feedback_logger.py     # Feedback & learning log
```

---

## V3 Internals

### Fitness Math (DynamicAllocator)

```python
# Sortino: downside-only risk-adjusted return
sortino = (mean_return - target) / downside_std

# Fitness: penalise churn, reward consistency
fitness = (sortino * win_rate) / (1 + turnover * penalty_factor)

# Softmax allocation (temperature = 1.0)
weights = softmax(fitness_scores, temperature=1.0)
```

### Chronos Outcome Simulation

For dry-run backtesting, trade outcomes are simulated via 48-candle lookahead:
- **WIN:** Take-Profit hit before Stop-Loss
- **LOSS:** Stop-Loss hit before Take-Profit (or loss > 1% triggers reflexion)
- **HOLD:** Neither level hit within 48 candles

### Reflexion Trigger

```
if trade_result == "LOSS" and pnl_pct < -1.0:
    for persona in losing_voters:
        rule = persona.reflect_on_loss(anomaly_type, thesis, pnl)
        memory_bank.save_lesson(persona, anomaly_type, pnl, thesis, rule)
```

---

## Position Sizing Rules

### Risk Environment → Position Multiplier

| Risk Level | Trigger | pos_mult |
|-----------|---------|----------|
| LOW | No news + normal volatility + clear structure | 1.0x–1.2x |
| MODERATE | General news OR elevated volatility | 0.7x–1.0x |
| HIGH | War/CPI/FOMC/SEC OR ATR ≥ 1.5× OR structure broken | 0.3x–0.5x |

### V2 Execution Gate Rules

| Condition | Action |
|-----------|--------|
| `macro_regime == RISK_OFF` | Block ALT LONG |
| `btc_strength == WEAK` | Block all LONG |
| `volatility == HIGH` | Force `pos_mult = 0.3×` (Volatility Shield) |

---

## Requirements

```
requests>=2.28.0
pandas
numpy
```

Install:
```bash
pip install -r requirements.txt
```

---

## Data Sources

| Source | Use |
|--------|-----|
| Binance fapi | Futures OHLCV (default, max 1500 candles/request) |
| Binance api | Spot OHLCV |
| CoinGecko | Global market metrics |
| alternative.me | Fear & Greed Index |
| LLM Provider API | Macro Risk Officer (V2), Persona analysis (V3) |
