# Plutus V4.0 — Cross-Asset Portfolio Matrix

> **Author:** Plutus V4.0 Agent
> **Status:** Implemented
> **Prerequisites:** `numpy`, `math`, `statistics`, `collections`

---

## 1. Problem Statement

### The Isolated BTC Trading Problem

Prior Plutus iterations treated each trade as a **standalone, regime-dependent decision**. This creates three systemic failure modes:

| Failure Mode | Symptom | Consequence |
|---|---|---|
| **Correlation Blindness** | Opening a LONG on ETH while already LONG BTC adds concentrated directional risk | Portfolio blowup in risk-off events |
| **No Cross-Asset Heat Tracking** | Multiple positions in the same regime accumulate unseen leverage | Real margin calls surprise the trader |
| **No Hedging Mechanism** | A BTC SHORT with an open ETH LONG is treated as two independent bets | Net delta can be 3-5x larger than intended |
| **Pairs Alpha Leakage** | ETH-BTC ratio divergences are visible but untradeable | Leaving statistical arbitrage on the table |

The **Cross-Asset Portfolio Matrix** solves all four by building a shared state layer that all trading decisions must query before execution.

---

## 2. Correlation Engine

### Purpose

Tracks rolling return series for all portfolio assets and exposes:
- **Pearson correlation** between any two assets
- **Full NxN correlation matrix** for the portfolio
- **Beta** of each asset relative to a benchmark (default: BTCUSDT)

### Pearson Correlation Formula

```
ρ(A, B) = Σ(r_Ai - μ_A)(r_Bi - μ_B) / (N · σ_A · σ_B)
```

Where:
- `r_Ai`, `r_Bi` = decimal returns for asset A and B at observation i
- `μ_A`, `μ_B` = rolling means over the lookback window
- `σ_A`, `σ_B` = rolling standard deviations
- `N` = number of observations (clipped to shorter series)

**Implementation:** Uses `collections.deque(maxlen=lookback)` for O(1) rolling eviction of old observations. Re-computes from scratch on each `get_correlation()` call — no incremental covariance matrix maintenance required.

### Beta Formula

```
β_asset = Cov(asset, benchmark) / Var(benchmark)
```

- `β > 1`: Asset is more volatile than BTC (amplifies moves)
- `β < 1`: Asset is less volatile than BTC (defensive)
- `β < 0`: Asset moves inversely to BTC (rare in crypto)

### Usage

```python
from src.execution import CorrelationEngine

ce = CorrelationEngine(assets=["BTCUSDT", "ETHUSDT", "SOLUSDT"], lookback=60)

# After each price tick:
ce.update("BTCUSDT", return_=0.0125)  # +1.25%
ce.update("ETHUSDT", return_=0.0180)  # +1.80%

rho = ce.get_correlation("ETHUSDT", "BTCUSDT")  # ~0.85 in bull markets
beta_eth = ce.get_beta("ETHUSDT")
```

---

## 3. Risk Manager

### Global Portfolio Heat Formula

```
heat = Σ(|position_value_i| × |β_i|) / equity
```

With VIX modifier:

```
if VIX > 30: heat *= 1.5
```

**Interpretation:**
- `heat < 1.0`: Comfortable — all positions normal size
- `heat = 1.0–2.0`: Elevated — position size reductions apply
- `heat > 2.0`: Maximum — new LONG positions blocked; SHORT allowed

### Size Reduction Matrix

When opening a new position in the **same direction** as an existing correlated position:

| Correlation (ρ) | Direction Match | Size Reduction | Resulting Size |
|---|---|---|---|
| `ρ > 0.8` | LONG-LONG or SHORT-SHORT | **75%** (`×0.25`) | 25% of planned |
| `ρ > 0.6` | LONG-LONG or SHORT-SHORT | **50%** (`×0.50`) | 50% of planned |
| `ρ < 0.6` | Any | **none** (`×1.0`) | Full planned size |
| `ρ > 0.6` | **Opposite directions** | **+25%** (`×1.25`) | 125% of planned (hedge boost) |

### VIX Modifier Logic

```
VIX ≤ 30 → no modifier
VIX > 30 → heat × 1.5
```

This accounts for elevated cross-asset correlation during market stress (the "all correlations go to 1" phenomenon).

### Max Leverage Enforcement

```
Major coins (BTC, ETH):
  max_leverage = floor((distance - 0.005) × 100)

Small caps:
  max_leverage = floor((distance - 0.015) × 100)

where distance = position_value / equity

Minimum leverage = 1x (no leverage)
```

Example: 50% of equity in BTC position → distance = 0.50
→ `max_leverage = floor((0.50 - 0.005) × 100) = floor(49.5) = 49x`

---

## 4. Pairs Trading

### Concept

When two assets are highly correlated (`ρ > 0.8`) but their **price ratio** has diverged from its historical mean, the ratio is expected to mean-revert. This creates a market-neutral trade: Long the underperforming asset, Short the overperforming asset.

### Ratio Breakout Math

```
ratio_t = price_A_t / price_B_t

z_score = (ratio_t - MA_ratio) / σ_ratio

Entry trigger: |z_score| > 2.0
```

**Z-score interpretation:**

| Z-Score | Ratio State | Trade |
|---|---|---|
| `z > +2` | Ratio too HIGH (A overvalued vs B) | SHORT A / LONG B |
| `z < -2` | Ratio too LOW (A undervalued vs B) | LONG A / SHORT B |
| `|z| < 2` | Ratio within normal range | No trade |

**Confidence levels:**

| |z| > 3 | 2.5 < |z| ≤ 3 | 2 < |z| ≤ 2.5 |
|---|---|---|---|
| **Confidence** | 3 (high) | 2 (medium) | 1 (low) |

### Entry Conditions (all must be true)

```
1. Rolling correlation(returns_A, returns_B) > 0.8
2. |z_score| > 2.0
3. At least 20 observations in ratio lookback
```

### Hedge Ratio

```
h_edge = -(β_A × pos_A) / β_hedge_instrument

Positive h_edge → SHORT hedge instrument
Negative h_edge → LONG hedge instrument
```

For a BTC-ETH pairs trade, the hedge ratio is computed in `PortfolioMatrix.rebalance_hedge()`.

---

## 5. Delta-Neutral Framework

### Beta-Weighted Delta Formula

```
Δ_portfolio = Σ(position_value_i × direction_i × β_i) / equity

direction_i = +1 for LONG, -1 for SHORT
```

**Interpretation:**
- `Δ = +1.0`: Fully long BTC-equivalent (100% of capital)
- `Δ = 0.0`: Delta-neutral (hedged)
- `Δ = -0.5`: 50% of capital net short BTC-equivalent

### Delta-Neutral Check

```
is_delta_neutral = |Δ_portfolio| < threshold

Default threshold = 0.1 (10% of capital)
```

### Rebalancing Trigger

```
if |Δ| > threshold:
    execute rebalance_hedge()
```

### Rebalance Hedge Computation

```
Σ_assets = Σ(β_i × position_value_i)
h_edge = -Σ_assets / β_hedge

Direction:
  h_edge > 0 → SHORT hedge instrument
  h_edge < 0 → LONG hedge instrument
```

---

## 6. Position Sizing — Integrated with Heat and Correlation

### Full Sizing Pipeline

```
Step 1: Determine planned position size (base 1% risk rule)
Step 2: Query CorrelationEngine for ρ with all existing positions
Step 3: RiskManager.assess_trade() → size_reduction factor
Step 4: Apply reduction: effective_size = planned_size × size_reduction
Step 5: Check global heat → if > MAX_HEAT, block LONG or require further reduction
Step 6: Compute max_leverage (RiskManager.enforce_max_leverage)
Step 7: Open in tranches: 50% / 30% / 20%
```

### Worked Example

```
Account equity: $10,000
Existing: LONG BTCUSDT $5,000
Planned: LONG ETHUSDT $3,000
ρ(BTC, ETH) = 0.75  (> 0.6 but ≤ 0.8)

Step 1: base_risk = $10,000 × 1% = $100
Step 2: ρ = 0.75  → size_reduction = 0.50
Step 3: effective_risk = $100 × 0.50 = $50
Step 4: effective_size = $50 / 0.02 (2% stop) = $2,500
Step 5: heat = Σ(5000 × 1.0) / 10000 = 0.5  → below max, proceed
Step 6: distance = 2500/10000 = 0.25  → max_leverage = floor((0.25-0.005)×100) = 24x
Step 7: 50% now ($1,250), 30% confirmation ($750), 20% reserve ($500)
```

### Decision Summary Table

| Condition | Action |
|---|---|
| `ρ > 0.8`, same direction | Reduce size to 25% of planned |
| `0.6 < ρ ≤ 0.8`, same direction | Reduce size to 50% of planned |
| `ρ > 0.6`, opposite direction | Boost size to 125% of planned |
| `global_heat ≥ 2.0` | Reject LONG; allow SHORT only |
| `VIX > 30` | Apply 1.5× heat multiplier |
| `|delta| > 0.1` | Trigger rebalance hedge |
| Leverage > max | Cap leverage to max allowed |

---

## File Reference

| File | Class | Purpose |
|---|---|---|
| `src/execution/__init__.py` | — | Module exports |
| `src/execution/portfolio_matrix.py` | `CorrelationEngine` | Rolling correlations & betas |
| `src/execution/portfolio_matrix.py` | `RiskManager` | Heat, size reduction, leverage |
| `src/execution/portfolio_matrix.py` | `PairsTrader` | Ratio breakout signals |
| `src/execution/portfolio_matrix.py` | `PortfolioMatrix` | Delta tracking & rebalancing |

---

*Plutus V4.0 — Cross-Asset Portfolio Matrix. All sizing decisions route through this layer.*
