# Plutus V4.0 — Institutional Execution Architecture

> **Author:** Evo | **Version:** 4.0 | **Date:** 2026-03-22

---

## Overview

The execution layer sits between the decision engine (trade plan + signal) and the exchange API. Its single goal is to minimise the cost of translating a *decision* into a *filled order*.

Cost here has two components:

| Component | Source | Mitigation |
|-----------|--------|-----------|
| **Explicit cost** | Commission, maker/taker fees | Maker rebates via passive orders |
| **Implicit cost** | Slippage + market impact | Smart routing, TWAP/VWAP slicing |

The Plutus V4 execution stack is built on five pillars:

```
Decision Engine
       │
       ▼
┌──────────────────┐
│   Smart Router   │  ← selects the right strategy per intent
└────────┬─────────┘
         │
  ┌──────┼──────────────────┐
  │      │                  │
  ▼      ▼                  ▼
TWAP   VWAP            LimitOrderQueue
                          │
                    Market Impact Model
                    (Almgren-Chriss)
                          │
                          ▼
                  Binance Spot API
```

---

## Section 1: Why Smart Routing

### 1.1 The cost of naive market orders

A market order crosses the spread immediately — convenient, but expensive.

**Example: BTCUSDT, $60,000**

| Event | Cost |
|-------|------|
| Market order for 1 BTC at mid $60,000 | Mid = $60,000 |
| Taker fee (0.10 %) | $60 |
| Adverse slippage (10 bps) | $60 |
| **Total implicit + explicit cost** | **~$120 (20 bps)** |

At 20 bps per market order, a strategy needs > 20 % expected move just to break even against fees and slippage.

### 1.2 Maker rebates as edge

Binance Spot offers:

- **Maker fee:** 0.10 % rebate (paid by exchange to the trader)
- **Taker fee:** 0.10 % charge

If a passive limit order fills at mid, the trader **earns** 10 bps instead of paying it. Over 10 trades per day this is a structural 100 bps advantage.

### 1.3 Intent-based routing

Not every trade should be passive. The SmartRouter maps each decision's intent to the right execution mode:

| Intent | Executor | When to use |
|--------|----------|-------------|
| `aggressive_fill` | MarketExecutor | News-driven, must-enter, T1 |
| `vwap_anchor` | VWAPExecutor | Large institutional orders |
| `twap_sniper` | TWAPExecutor | Mid-size, time-bounded orders |
| `passive_fvg` | LimitOrderQueue | Continuation setups, mean-reversion |

---

## Section 2: TWAP — Time-Weighted Average Price

### 2.1 Core concept

TWAP breaks a parent order into **equal-sized child orders** distributed uniformly over a fixed time window.

```
Parent order:  10 BTC over 600 seconds (10 minutes), slice every 60s
Slices:        6 × 1.667 BTC, one every 60 seconds
```

**Slice formula:**

```
num_slices = duration_secs / slice_interval_secs
slice_qty  = total_quantity / num_slices
```

### 2.2 Duration vs participation trade-off

| Scenario | Recommended duration | Trade-off |
|----------|---------------------|-----------|
| Small order (< 1 % ADV) | 1–5 min | Minimal impact; timing risk is low |
| Medium order (1–5 % ADV) | 10–30 min | Balance between impact and timing risk |
| Large order (> 5 % ADV) | 30–180 min | Avoid moving price; accept timing uncertainty |

**Rule:** The longer the duration, the lower the per-slice impact, but the more the execution is exposed to price drift.

### 2.3 Execution benchmark

TWAP is benchmarked against the **time-weighted average price of the execution window**:

```
Benchmark = (1 / duration) × ∫ price(t) dt  ≈  mean(fill_prices)
Actual    = total_cost / total_filled

Implementation Shortfall = Actual − Benchmark (in bps)
```

Negative IS (Actual < Benchmark) = good execution (price improved during the window).

### 2.4 Slippage formula

```
slippage_bps = (fill_price − expected_price) / expected_price × 10,000
```

Positive = adverse slippage (worse than expected). Negative = price improvement.

### 2.5 Limitations

- TWAP assumes volume is constant over the window — unrealistic intraday.
- TWAP is predictable: sophisticated participants can front-run fixed schedules.
- TWAP does not adapt to changing liquidity.

For intraday with variable volume, prefer **VWAP**.

---

## Section 3: VWAP — Volume-Weighted Average Price

### 3.1 Core concept

VWAP schedules child orders proportional to the **historical intraday volume curve**, concentrating execution when market volume is highest (open and close). This matches the natural flow of liquidity, reducing impact.

```
Volume curve (14 × 30-min buckets, Binance Spot approximate):
09:00  1.20  ← Asia session overlap
09:30  0.95
10:00  0.85
10:30  0.80
... (see VOLUME_CURVE_30M in VWAPExecutor)
15:30  1.35  ← New York open overlap
```

### 3.2 Volume curve calibration

The default curve is approximate. Calibrate with:

```python
# Collect 20+ days of Binance klines (1h interval)
# For each 30-min bucket, compute: bucket_volume / daily_volume
# Average across days → your calibrated curve
```

### 3.3 Participation rate

```
child_qty = market_volume_in_bucket × participation_rate
```

- **10 % participation** = you represent 10 % of market volume in each bucket.
- Participation > 20 % is aggressive and will move the market.
- For orders > 5 % of ADV, use 5–8 % participation to avoid self-impact.

### 3.4 Momentum-adjusted participation

When current volume is abnormally high (e.g. news event), trading at the scheduled rate would represent an outsized fraction of total volume — a signal that reveals the order.

```
if current_volume_rate > 1.5 × avg_volume_rate:
    participation_rate ×= (1 − 0.20)   # reduce by 20 %
```

This prevents the order from becoming a market-moving force during illiquid patches.

### 3.5 VWAP benchmark

```
benchmark_vwap = Σ(fill_qty_i × fill_price_i) / Σ(fill_qty_i)
               = Σ(fill_cost) / total_filled
```

Execution is evaluated the same way as TWAP: actual vs benchmark in bps.

### 3.6 Slippage measurement

```
slippage_bps = (fill_price − bucket_VWAP) / bucket_VWAP × 10,000
```

This isolates the cost of crossing the spread vs the cost of volume timing.

---

## Section 4: Limit Order Queue — Passive FVG / Retracement Logic

### 4.1 Why passive orders work

A limit buy placed below the mid price only executes if the market falls to that level — i.e. **on a retracement**. This means:

- Fill price ≤ limit price (no adverse slippage at time of fill)
- Maker rebate earned (negative cost, not positive)
- ICT / SMC: institutions drive price through FVG zones, triggering buy-side stops, then reverse — the gap fills, price continues.

### 4.2 FVG (Fair Value Gap) placement logic

An FVG forms when price moves discontinuously, leaving a zone where no trading occurred:

```
Candle 1 (bearish): high = 60,200   low = 60,000
Candle 2 (bullish): high = 60,400  low = 60,100
                    ↑
              FVG = (60,100, 60,200)   ← gap between candle 2 low and candle 1 high
```

**Entry price formula:**

```
entry_price = (fvg_low + fvg_high) / 2
            = (60,100 + 60,200) / 2
            = 60,150
            ↓ rounded to nearest tick (0.01)
            = 60,150.00
```

Why the midpoint? The assumption is that institutional price discovery fills the gap
evenly — the midpoint is the statistically fair entry within the zone.

### 4.3 Fibonacci retracement placement

After a directional move, price often retraces a fraction before continuing.

```
Entry price (LONG) = swing_low × (1 − retracement_pct)
Entry price (SHORT) = swing_high × (1 + retracement_pct)

Example: BTC moved from $58,000 → $61,000 (swing low = 58,000)
38.2 % retracement:  entry = 58,000 × (1 − 0.382) = $59,844
61.8 % retracement:  entry = 58,000 × (1 − 0.618) = $58,000 × 0.382 = $22,156
```

Key levels: **38.2 %, 50 %, 61.8 %** (Fibonacci retracement ratios).

### 4.4 Queue management

Managing multiple passive orders requires:

1. **Cancellation discipline:** cancel orders when structure breaks (invalidation hit).
2. **Distance monitoring:** track how far each order is from mid price (in bps).
3. **Partial fill handling:** update pending quantity after each fill.
4. **Timeout:** cancel orders older than a defined window (e.g. 4 hours) if unfilled.

### 4.5 Queue status metrics

```
open_orders         = count of live open orders
total_pending_qty   = Σ(orig_qty − filled_qty)
avg_distance_bps    = mean(|order_price − mid| / mid × 10,000)
```

An avg distance > 50 bps suggests the queue is stale and price has moved away — cancel and re-assess.

---

## Section 5: Almgren-Chriss Market Impact Model

### 5.1 The problem

Every order moves the market. Large orders move it more. The cost of that movement — **market impact** — is the largest source of implicit cost for institutional orders.

### 5.2 Almgren-Chriss framework

The Almgren-Chriss (AC) model (2000, NYU Courant) decomposes execution cost into:

```
Total Cost = Temporary Impact + Permanent Impact + Timing Risk
```

For HFT / intraday execution, **temporary impact dominates**:

```
Temporary Impact = θ × σ × (Q / ADV)^0.6

where:
  θ    = market constant (empirically ~0.1 for BTC, higher for smaller caps)
  σ    = daily volatility (fraction)
  Q    = order size in base units
  ADV  = average daily volume in same units
  0.6  = empirically fitted exponent (Almgren-Chriss 2000)
```

In bps:

```
impact_bps = θ × (Q / ADV)^0.6 × σ × 10,000
```

### 5.3 Parameters

| Parameter | Symbol | Typical value (BTCUSDT) | Source |
|-----------|--------|------------------------|--------|
| Market constant | θ | 0.10 | Calibrated from historical fills |
| Volatility | σ | 0.02–0.05 (2–5 % daily) | 30-day rolling window |
| ADV | ADV | ~500 BTC (Binance) | 30-day average |
| Exponent | γ | 0.6 | AC theory / empirical |

**Calibrating θ from data:**

```
θ_implied = observed_impact_bps / ((Q/ADV)^0.6 × σ × 10,000)
```

Run this across 50+ historical fills and average the result to get your calibrated θ.

### 5.4 Example calculations

**Scenario:** Sell 10 BTC, ADV = 500 BTC, σ = 0.025 (2.5 % daily)

```
participation = 10 / 500 = 0.02
impact_bps    = 0.1 × (0.02)^0.6 × 0.025 × 10,000
             = 0.1 × 0.154 × 0.025 × 10,000
             = 3.85 bps
```

**Scenario:** Sell 50 BTC, ADV = 500 BTC, same σ

```
participation = 50 / 500 = 0.10
impact_bps    = 0.1 × (0.10)^0.6 × 0.025 × 10,000
             = 0.1 × 0.398 × 0.025 × 10,000
             = 9.95 bps  ← almost 10 bps!
```

The non-linear (0.6 exponent) means impact grows quickly for large participation ratios.

### 5.5 Optimal execution split

The key question: **how many child orders** to split the parent into?

```
For each candidate n:
  child_qty = Q / n
  child_impact = θ × (child_qty / ADV)^0.6 × σ × 10,000
  if child_impact ≤ max_impact_bps → acceptable
  → binary search for minimum n
```

**Binary search parameters:**

```
lo = 1          hi = 10,000
max_impact_bps  = 5.0 (default)
```

### 5.6 Practical limits

- AC is calibrated for equities; crypto markets may behave differently.
- During liquidity crises (high volatility events), θ should be scaled up.
- The model ignores **correlation** between child orders — in trending markets,
  splitting uniformly over time is not optimal (prefer front-loading for buys).

---

## Section 6: Smart Router Decision Matrix

### 6.1 Intent → Executor mapping

| Intent | Executor | Commission | Slippage | Timing risk | When to use |
|--------|----------|-----------|----------|-------------|-------------|
| `aggressive_fill` | MarketExecutor | 10 bps (taker) | Variable | Zero | Must-enter, news, T1 |
| `vwap_anchor` | VWAPExecutor | ≤ 10 bps (maker) | Low | Low-medium | Large orders, < 5 % ADV |
| `twap_sniper` | TWAPExecutor | ≤ 10 bps (maker) | Low | Medium | Medium orders, fixed window |
| `passive_fvg` | LimitOrderQueue | −10 bps (rebate) | Zero at entry | High | Continuation, range-bound |

### 6.2 Decision flow

```
START: Order intent received
  │
  ├─ Is speed critical? (T1 / news / stop-run)
  │    YES → MarketExecutor  → execute immediately
  │    NO  ↓
  │
  ├─ Is order > 3 % of ADV?
  │    YES → VWAPExecutor → schedule along volume curve
  │    NO  ↓
  │
  ├─ Is there a defined time window?
  │    YES → TWAPExecutor  → slice evenly over duration
  │    NO  ↓
  │
  └─ Is structure intact + passive entry zone identified?
       YES → LimitOrderQueue → place at FVG or retracement level
       NO  → NO TRADE (no valid execution path)
```

### 6.3 Slippage reporting

After each session, the SmartRouter aggregates:

```
Session Fill Report
  total_orders         = N
  total_filled         = Σ(fill_qty)
  avg_slippage_bps     = mean(slippage_bps across all fills)
  worst_fill_bps       = max(slippage_bps)
  best_fill_bps        = min(slippage_bps)
  by_executor          = breakdown per executor type
```

Use this to audit execution quality weekly and recalibrate θ for MarketImpactModel.

---

## Appendix A: File Structure

```
src/
  execution/
    order_router.py          ← TWAP, VWAP, LimitOrderQueue, SmartRouter, MIM
    exchanges/
      __init__.py
      binance_executor.py    ← Binance API adapter (test/live modes)
docs/
  V4_EXECUTION.md            ← This document
```

## Appendix B: Key Formulas Reference

| Formula | Where |
|---------|-------|
| `slice_qty = total_qty / (duration_secs / slice_interval_secs)` | TWAP |
| `slippage_bps = (fill_price − expected_price) / expected_price × 10,000` | TWAP, VWAP |
| `entry_price = (fvg_low + fvg_high) / 2` (rounded to tick) | LimitOrderQueue |
| `long_entry = base_price × (1 − retracement_pct)` | LimitOrderQueue |
| `impact_bps = θ × (Q / ADV)^0.6 × σ × 10,000` | MarketImpactModel |
| `optimal_n = binary_search(n where impact_bps ≤ max_impact_bps)` | MarketImpactModel |
| `part_reduction = participation_rate × (1 − 0.20)` | VWAP momentum |
| `twap_benchmark = mean(fill_prices)` | TWAP evaluation |
| `vwap_benchmark = Σ(fill_qty × fill_price) / Σ(fill_qty)` | VWAP evaluation |

---

*Plutus V4.0 — Built by Evo, refined by Austin.*
