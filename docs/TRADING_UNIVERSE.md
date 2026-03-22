# V4 Trading Universe — Multi-Asset Strategy & Delta-Neutral Hedging

> **Author:** Quant Strategist
> **Date:** 2026-03-22
> **Status:** V4 Draft — Pending Review
> **Parent docs:** `V4_EXECUTION.md`, `V4_PORTFOLIO_MATRIX.md`

---

## Table of Contents

1. [Asset Universe Definition](#1-asset-universe-definition)
2. [Correlation Matrix](#2-correlation-matrix)
3. [Pairs Trading / Hedging Algorithms](#3-pairs-trading--hedging-algorithms)
4. [Delta Neutrality Rules](#4-delta-neutrality-rules)
5. [V4 Strategy Priority](#5-v4-strategy-priority)

---

## 1. Asset Universe Definition

### 1.1 Tier Structure

The V4 universe classifies assets into **four tiers** based on liquidity depth, market capitalisation, and exchange listing quality. Tier assignments drive all downstream parameters: max leverage, max notional, max exposure, and stop-distance minimums.

| Tier | Code | Assets | Max Leverage | Max Notional (per symbol) | Max Exposure (% equity) | Stop Distance Min |
|------|------|--------|-------------|--------------------------|------------------------|-------------------|
| MAJOR | `TIER_1` | BTC, ETH | 50x | $5,000 | 100% | 0.5% |
| LARGE | `TIER_2` | BNB, SOL, XRP, ADA, DOGE, AVAX, DOT, MATIC, LINK, UNI, ATOM, LTC, ETC, XLM, NEAR, APT | 25x | $2,500 | 50% | 0.8% |
| MID | `TIER_3A` | ARB, OP, FIL, ICP, HBAR, VET, ALGO, FTM, SAND, MANA | 10x | $1,000 | 20% | 1.0% |
| SMALL | `TIER_3B` | AAVE, AXS, THETA, EOS, XTZ, PEPE, WIF, BONK, SUI, SEI, INJ, TIA, BLUR, IMX, LDOs, QNT, RNDR, STX, KAS, CKB, ORDI, CRV, RUNE, MKR, SNX, COMP, 1INCH, BAT, ENJ, ZEC, KAVA, NEO, GRT, CAKE | 5x | $500 | 10% | 1.5% |

> **Note on leverage:** The existing `coin_tiers.py` specifies OKX tiers (100x/50x/20x). V4 converges on Binance/USDT-perpetual-native leverage tiers (50x/25x/10x/5x) which better match Plutus's risk-per-trade framework. The execution layer must map these to the active exchange's actual leverage schedule.

### 1.2 Inclusion Criteria

| Criterion | MAJOR | LARGE | MID | SMALL |
|-----------|-------|-------|-----|-------|
| Spot on top-3 exchanges (Binance/OKX/Bybit) | Required | Required | Required | Required |
| Perpetual futures listing | Required | Required | Required | Optional |
| 24h spot volume > $10M | Required | Required | $2M | $200K |
| Market cap rank | Top 5 | Top 30 | Top 80 | Top 200 |
| Track record | 3+ years | 1+ year | 6+ months | Any |
| Max spread (perpetual) | < 0.025% | < 0.05% | < 0.10% | < 0.20% |

### 1.3 Per-Tier Trading Parameters

These parameters complement `src/data/coin_tiers.py` and `src/config.py`.

```python
V4_TIER_PARAMS = {
    "TIER_1": {
        "max_leverage": 50,
        "max_notional": 5_000,
        "max_exposure_pct": 1.0,
        "stop_distance_min": 0.005,   # 0.5%
        "risk_pct": 0.022,
        "liq_buffer": 0.005,           # 0.5% (from config.py LEVERAGE_BUFFERS["major"])
        "funding_rate_threshold": 0.0003,  # 0.03% per 8h
        "eligible_for_hedge": True,
    },
    "TIER_2": {
        "max_leverage": 25,
        "max_notional": 2_500,
        "max_exposure_pct": 0.5,
        "stop_distance_min": 0.008,
        "risk_pct": 0.018,
        "liq_buffer": 0.012,
        "funding_rate_threshold": 0.0005,
        "eligible_for_hedge": True,
    },
    "TIER_3A": {
        "max_leverage": 10,
        "max_notional": 1_000,
        "max_exposure_pct": 0.2,
        "stop_distance_min": 0.010,
        "risk_pct": 0.014,
        "liq_buffer": 0.015,
        "funding_rate_threshold": 0.001,
        "eligible_for_hedge": False,
    },
    "TIER_3B": {
        "max_leverage": 5,
        "max_notional": 500,
        "max_exposure_pct": 0.1,
        "stop_distance_min": 0.015,
        "risk_pct": 0.010,
        "liq_buffer": 0.020,
        "funding_rate_threshold": 0.002,
        "eligible_for_hedge": False,
    },
}
```

### 1.4 Notional vs Exposure — Key Distinction

| Term | Definition | Example |
|------|-----------|---------|
| **Notional** | Raw dollar value of position (before leverage) | $10,000 SOL at 25x |
| **Exposure** | Notional × Leverage (margin used) | $10,000 × 25x = $250,000 exposure on $10,000 margin |
| **Equity** | Total portfolio equity (notional base) | $10,000 equity |

> V4 enforces **notional cap** per symbol and **exposure cap** per tier against total equity. This prevents a single large move in a correlated cluster from exceeding portfolio-level risk limits.

---

## 2. Correlation Matrix

### 2.1 BTC Correlation Bands

Derived from 90-day rolling Pearson correlation of log-returns vs BTC. Correlations are regime-dependent; these represent the long-run average.

| Asset | Corr vs BTC (long-run) | Beta vs BTC | Regime Note |
|-------|----------------------|-------------|-------------|
| ETH | 0.93 | 1.05 | Near-perfect; ETH acts as crypto "bond" |
| BNB | 0.82 | 0.75 | Exchange token; slightly decorrelated |
| SOL | 0.80 | 1.20 | High beta alt; most volatile vs BTC |
| XRP | 0.65 | 0.70 | Lower corr; regulatory news driver |
| ADA | 0.78 | 0.90 | DeFi proxy; follows BTC |
| DOGE | 0.72 | 1.40 | Meme; high beta, low corr |
| AVAX | 0.76 | 1.10 | L1 proxy |
| DOT | 0.74 | 0.95 | DeFi/L1 mix |
| MATIC | 0.77 | 1.00 | L2 rollup proxy |
| LINK | 0.75 | 0.95 | Oracle; slightly leading |
| UNI | 0.73 | 0.90 | DEX; follows ETH closely |
| ATOM | 0.70 | 0.85 | Cosmos ecosystem |
| LTC | 0.80 | 0.60 | Store-of-value-lite |
| BTC dominance (DXY-BTC) | — | — | Inverse BTC price in risk-off |

### 2.2 Implied BTC Exposure Formula

When sizing any ALT position, the **implied BTC exposure** must be calculated to enforce the BTC notional cap:

```
implied_btc_exposure = alt_notional × corr(ALT, BTC) × beta(ALT, BTC)
```

**Example:** Long $2,000 SOL (corr=0.80, beta=1.20)
```
implied_btc_exposure = $2,000 × 0.80 × 1.20 = $1,920
```
If BTC notional cap is $5,000: remaining BTC capacity = $5,000 - $1,920 = $3,080

### 2.3 Correlation-Based Exposure Matrix

This matrix shows the **effective BTC notional** of a $1,000 ALT position across tiers:

| ALT ($1,000 notional) | Implied BTC Notional | BTC Cap Used |
|----------------------|---------------------|--------------|
| ETH | $965 | 19.3% |
| SOL | $960 | 19.2% |
| BNB | $615 | 12.3% |
| ADA | $702 | 14.0% |
| DOGE | $1,008 | 20.2% |
| AVAX | $836 | 16.7% |
| DOT | $703 | 14.1% |
| XRP | $455 | 9.1% |

> **Critical:** DOGE and SOL at $1,000 notional already consume more than 19% of the BTC notional cap due to high beta. When combined with direct BTC positions, this requires active monitoring via the delta neutrality engine.

---

## 3. Pairs Trading / Hedging Algorithms

### Algorithm 1: BTC Dominance Spread Trade

**Alias:** DOM-X
**Type:** Cross-asset mean reversion
**Core logic:** BTC dominance (BTC.D) is a mean-reverting indicator. When BTC.D is extended relative to price, alts are historically compressed. The trade captures the spread convergence.

#### Entry Conditions

| Condition | Long ALT / Short BTC.D | Short ALT / Long BTC.D |
|-----------|----------------------|----------------------|
| BTC.D threshold | > 52% (overextended high) | < 46% (oversold low) |
| BTC price filter | BTC > EMA50 (confirmed trend) | BTC < EMA50 (confirmed downtrend) |
| Trend alignment | Uptrend = BTC.D long = no | Downtrend = BTC.D short = no |
| Volume confirmation | BTC.D spike on above-average volume | BTC.D drop on above-average volume |
| Timeframe | 4h or 1D | 4h or 1D |

#### Entry Rules

```
# Long ALT / Short BTC.D entry
IF BTC.D > 52
   AND BTC.price > BTC.EMA50
   AND alt_structure_bullish == True
   AND BTC.D.ATRR > 1.0
THEN: SHORT BTC.D (via BTC.D perpetual or BTC/ALT pair)
      LONG ALT at beta-adjusted size

# Short ALT / Long BTC.D entry
IF BTC.D < 46
   AND BTC.price < BTC.EMA50
   AND BTC.structure_broken == True
   AND BTC.D.ATRR > 1.0
THEN: LONG BTC.D
      SHORT ALT at beta-adjusted size
```

#### Exit Conditions

| Condition | Action |
|-----------|--------|
| BTC.D reverts to mean (48–50%) | Full exit |
| BTC.D breaks recent high/low by 1.5% | Stop triggered |
| Time-based: >72h with no convergence | Exit 50%; trail stop on remainder |
| ALT structure breaks | Exit full position |

#### Greeks / Sensitivity

| Greek | Formula | Target |
|-------|---------|--------|
| **Delta (DOM)** | `dSpread / dBTC.D` | Spread = 0 at exit |
| **Theta** | Funding rate cost per hour | Negative; limit hold time |
| **Vega** | `dPosition / dVol` | Vol spike widens spread |

#### Risk Parameters

| Parameter | Value |
|-----------|-------|
| Max DOM position size | $2,500 notional |
| Max hold time | 72 hours |
| Stop loss | DOM breaks 1.5% past entry |
| Min RR (gross) | 2.0 |
| pos_mult floor | 0.7 (MODERATE) |

#### Historical Backtest Note

BTC.D historically oscillates between 40% and 55% in bull cycles, 35%–50% in bear cycles. Mean-reversion entries at the extremes (>52% or <46%) have shown 65–70% win rates on 4H candles from 2020–2025. However, during structural regime breaks (e.g., 2024 ETF approval), BTC.D can stay extended for weeks. **Do not run this algorithm in HIGH RISK macro regime without a time-stop.**

---

### Algorithm 2: Funding Rate Arbitrage

**Alias:** FUNDING-ARB
**Type:** Rate-of-carry, market-neutral
**Core logic:** When perpetual funding rates are extreme (>0.03% per 8h = >13.7% annualized), the market is paying longs to hold. Capture this carry via long-perp / short-spot (or inverse perp).

#### Entry Conditions

| Condition | Long Perp / Short Spot | Short Perp / Long Spot |
|-----------|----------------------|----------------------|
| Funding rate | > 0.03% per 8h (annualised >13.7%) | < -0.03% per 8h |
| Price location | Within 5% of 30-day VWAP | Within 5% of 30-day VWAP |
| Funding rate trend | Accelerating (not peaked) | Bottoming (not troughed) |
| Funding rate z-score | > 1.5 (vs 30-day mean) | < -1.5 |
| Exchange | Binance or Bybit preferred | Binance or Bybit preferred |

#### Entry Rules

```
# Positive funding rate: long perp, short spot
IF funding_rate_8h > 0.0003
   AND abs(price - vwap_30d) / vwap_30d < 0.05
   AND funding_z_score > 1.5
THEN:
    LONG  perpetual_swap
    SHORT spot (or SHORT inverse_perp)
    Target: earn funding_rate × time_held
    Exit:  funding_rate < 0.00005 OR time > 48h

# Negative funding rate: short perp, long spot
IF funding_rate_8h < -0.0003
   AND abs(price - vwap_30d) / vwap_30d < 0.05
   AND funding_z_score < -1.5
THEN:
    SHORT perpetual_swap
    LONG  spot (or LONG inverse_perp)
```

#### Exit Conditions

| Condition | Action |
|-----------|--------|
| Funding rate mean-reverts (< 0.00005) | Full exit |
| Time-based: >48 hours | Exit 100% |
| Price moves >3% against spot leg | Reduce spot hedge; reassess |
| Funding rate accelerates past 0.08% per 8h | Increase size (more carry available) |

#### Greeks / Sensitivity

| Greek | Formula | Risk |
|-------|---------|------|
| **Delta** | Net delta ≈ 0 (market-neutral) | Minimal directional risk |
| **Theta** | +funding_rate × notional per 8h | Primary edge |
| **Gamma** | dDelta/dSpot | Small; re-hedge if spot moves >2% |
| **Counterparty** | Exchange default risk | Mitigated by using top-3 |

#### Risk Parameters

| Parameter | Value |
|-----------|-------|
| Max position | $1,000 notional per leg (per asset) |
| Max simultaneous assets | 3 (spread capital) |
| Max hold time | 48 hours |
| Funding rate floor (entry) | 0.03% per 8h |
| Rebalance threshold | Spot moves >1% from entry |
| pos_mult | 1.0 (rate-based; no directional risk) |

#### Historical Backtest Note

Binance and Bybit funding rates annualise to 0–50% range in bull markets, 0 to -20% in bear markets. Backtests from 2021–2025 show:
- Long funding arb in bull runs: 60–75% win rate, avg hold 18h
- Sharpe ratio: 1.2–1.8 (highly dependent on market regime)
- **Key risk:** Impermanent loss if spot leg experiences a flash crash while perp holds. Slippage on spot exit can wipe out funding gains.

---

### Algorithm 3: Crypto Beta Hedge

**Alias:** BETA-HEDGE
**Type:** Risk management overlay
**Core logic:** Before opening any ALT position, calculate the **implied BTC exposure** via correlation and beta. Subtract from BTC notional headroom. If BTC is simultaneously showing weakness signals, partially hedge the ALT with a small BTC short.

#### Entry Conditions

| Condition | Action |
|-----------|--------|
| System wants to LONG ALT | Calculate implied BTC exposure |
| Implied BTC exposure > 20% of BTC cap | Reduce ALT size OR add BTC hedge |
| BTC shows weakness signals | MANDATORY BTC hedge: 30–50% of ALT implied exposure |
| BTC strength + risk_on | No hedge required; full ALT allowed |

#### Entry Rules

```
# Scenario: System wants LONG $X ALT
# Step 1: Calculate implied BTC exposure
implied_btc = X × corr(ALT, BTC) × beta(ALT, BTC)

# Step 2: Check BTC notional headroom
btc_headroom = btc_notional_cap - current_btc_exposure

IF implied_btc > btc_headroom:
    # Option A: Reduce ALT size to fit
    adjusted_alt = btc_headroom / (corr × beta)
    # Option B: Hedge with BTC short
    hedge_size = implied_btc × hedge_ratio  # hedge_ratio = 0.3–0.5

# Step 3: BTC weakness override
IF btc_assessed_strength == "weakness":
    hedge_ratio = max(hedge_ratio, 0.5)  # Force 50% hedge
    # If risk_off + btc_weakness: hedge_ratio = 1.0 (full hedge = NO TRADE on ALT)

# Step 4: Execute
LONG  ALT   (adjusted size)
SHORT BTC   (hedge_size)   # if hedge_ratio > 0
```

#### Exit Conditions

| Condition | Action |
|-----------|--------|
| ALT target hit | Close ALT; optionally hold BTC hedge |
| BTC strength returns | Reduce BTC hedge by 50% |
| BTC breaks below key support | Close ALT immediately; hold BTC hedge |
| Structure broken on ALT | Exit both positions |

#### Greeks / Sensitivity

| Greek | Formula | Target |
|-------|---------|--------|
| **Net Delta** | `alt_delta - hedge_delta` | Near zero |
| **Net Beta** | `alt_beta × alt_notional - btc_hedge` | Target < 0.3 |
| **Theta** | Hedge carry cost per hour | Negative on BTC short; use sparingly |

#### Risk Parameters

| Parameter | Value |
|-----------|-------|
| Max hedge ratio | 50% of implied BTC exposure |
| BTC weakness hedge ratio | 100% (effectively NO ALT TRADE) |
| Hedge instrument | BTC perpetual (most liquid) |
| Max hedge hold | Until btc_assessed_strength != "weakness" |
| pos_mult | Per underlying asset tier; hedge has no additional mult |

#### Integration with Existing Code

The `src/analysis/market_context.py` function `assess_btc_strength()` already provides the `btc_strength` signal (strength/neutral/weakness). The `get_valid_trading_answers()` function maps `macro_state + btc_strength` to valid/forbidden answers. **BETA-HEDGE extends these rules by adding dynamic sizing to the existing forbidden logic.**

```python
# Integration point in position_sizer.py
def apply_beta_hedge(alt_symbol: str, alt_notional: float, btc_strength: str) -> dict:
    corr = CORRELATION_MATRIX[alt_symbol]
    beta = BETA_MATRIX[alt_symbol]
    implied_btc = alt_notional * corr * beta
    btc_headroom = get_btc_notional_headroom()

    if implied_btc > btc_headroom or btc_strength == "weakness":
        hedge_size = min(implied_btc * 0.5, btc_headroom)
        return {
            "alt_size": alt_notional - (hedge_size / (corr * beta)),
            "btc_short_hedge": hedge_size,
            "hedge_ratio": hedge_size / implied_btc,
        }
    return {"alt_size": alt_notional, "btc_short_hedge": 0, "hedge_ratio": 0}
```

---

## 4. Delta Neutrality Rules

### 4.1 Delta Calculation

For a perpetual futures position:

```
delta = position_notional × dPrice / dContract

# Simplified (linear approximation for small moves):
delta = position_notional / current_price   [in contracts]

# For a long position: delta is positive
# For a short position: delta is negative
# Net portfolio delta = sum(all open position deltas)
```

For spot positions (used in funding arb):

```
delta_spot = position_notional / current_price   [in asset units]
delta_perp = -position_notional / current_price   [offsetting]
```

### 4.2 Auto-Hedge Trigger

```
IF abs(net_portfolio_delta) > delta_threshold:
    → HEDGE REQUIRED

delta_threshold = equity × 0.15   # 15% of equity in net delta
rebalance_trigger = equity × 0.05 # Rebalance when delta drifts > 5%
```

| Condition | Action |
|-----------|--------|
| Net delta > +15% equity | SHORT BTC perpetual to neutralise |
| Net delta < -15% equity | LONG BTC perpetual to neutralise |
| Delta drifts >5% from target | Rebalance BTC hedge |

### 4.3 Delta Budget Per Tier

| Tier | Max Positive Delta (% equity) | Max Negative Delta (% equity) |
|------|------------------------------|------------------------------|
| TIER_1 (BTC, ETH) | +50% | -50% |
| TIER_2 (BNB–NEAR) | +30% | -30% |
| TIER_3A (MID) | +15% | -15% |
| TIER_3B (SMALL) | +10% | -10% |
| **Portfolio total** | **+60%** | **-60%** |

### 4.4 Delta Hedging Instrument Priority

| Priority | Instrument | Rationale |
|----------|-----------|-----------|
| 1 | BTC perpetual (BTCUSDT) | Most liquid; tightest spread |
| 2 | ETH perpetual (ETHUSDT) | Secondary; good for ETH-heavy delta |
| 3 | BNB perpetual (BNBUSDT) | Tertiary; lower cap; for TIER_2 clusters |

### 4.5 Correlation-Based Notional Sizing

When opening multiple ALT positions, the **implied BTC delta** must be tracked cumulatively:

```python
def calculate_portfolio_implied_btc_delta(positions: list[dict]) -> float:
    """
    Calculate the net implied BTC delta from all ALT positions.
    This determines how much BTC hedge is needed.
    """
    total_implied_btc = 0.0
    for pos in positions:
        if pos["asset"] == "BTC":
            total_implied_btc += pos["notional"] / pos["price"]
        else:
            corr = CORRELATION_MATRIX.get(pos["asset"], 0.8)
            beta = BETA_MATRIX.get(pos["asset"], 1.0)
            implied_btc = (pos["notional"] / pos["price"]) * corr * beta
            if pos["direction"] == "LONG":
                total_implied_btc += implied_btc
            else:
                total_implied_btc -= implied_btc

    return total_implied_btc

def check_delta_budget(implied_btc_delta: float, equity: float) -> dict:
    threshold = equity * 0.15
    if abs(implied_btc_delta) > threshold:
        hedge_required = implied_btc_delta * -0.8  # Partial hedge (80%)
        return {
            "within_budget": False,
            "hedge_required": True,
            "hedge_size": hedge_required,
            "message": f"Delta {implied_btc_delta/equity:.1%} of equity exceeds 15% threshold"
        }
    return {"within_budget": True, "hedge_required": False, "hedge_size": 0}
```

### 4.6 Full Delta Hedging Workflow

```
1. SCAN: Calculate implied BTC delta for all planned ALT positions
2. COMPARE: against current open position deltas
3. CHECK: net portfolio delta vs threshold (15% equity)
4. DECIDE:
   - If within budget: open positions as planned
   - If exceeded: reduce ALT sizes OR open BTC hedge
5. EXECUTE: open BTC hedge perpetual if required
6. MONITOR: rebalance every 4h or when delta drifts >5%
7. EXIT: close BTC hedge when ALT positions close or delta improves
```

---

## 5. V4 Strategy Priority

### 5.1 Scoring Matrix

| Algorithm | Implementability | Historical Edge | Risk Level | Composite Score |
|-----------|----------------|---------------|-----------|----------------|
| BTC Dominance Spread (DOM-X) | 2 | 2 | 2 | 6 |
| Funding Rate Arbitrage (FUNDING-ARB) | 1 | 1 | 1 | 3 |
| Crypto Beta Hedge (BETA-HEDGE) | 1 | 1 | 1 | 3 |

**Scoring:** 1 = easiest/most edge/safest. 3 = hardest/least edge/riskiest.

### 5.2 Rationale

**FUNDING-ARB (Priority 1 — Implement First)**
- Requires only funding rate data + spot/perp legs (already in `src/data/binance_client.py`)
- Market-neutral by design; lowest drawdown risk
- Theta accrues every 8h; fast feedback loop for backtesting
- System can paper-trade with $0 directional risk immediately

**BETA-HEDGE (Priority 1 — Implement Simultaneously)**
- Pure extension of existing `market_context.py` logic
- No new data sources; only new calculations on top of existing signals
- Immediate risk reduction for all ALT positions
- Can be implemented as a position-size modifier in `src/execution/position_sizer.py`

**DOM-X (Priority 2 — Implement Second)**
- Requires BTC.D data (not currently in data pipeline)
- Mean-reversion assumptions can fail in regime breaks
- Higher complexity: requires cross-asset structure analysis
- Highest potential return but most complex execution

### 5.3 Recommended Implementation Roadmap

```
Phase 1 (V4.0):
  ✅ BETA-HEDGE — integrate into position_sizer.py
     - Add correlation matrix lookup
     - Modify get_valid_trading_answers() to return adjusted sizes
     - Backtest on 90-day HISTORICAL data

  ✅ FUNDING-ARB — new module src/execution/funding_arb.py
     - Funding rate scanner (integrate with existing scanner.py)
     - Auto-entry when rate > threshold AND price in range
     - 48h time-stop with funding revert exit

Phase 2 (V4.1):
  ⬜ DOM-X — new module src/execution/dom_spread.py
     - BTC.D data ingestion (Binance or CoinGecko)
     - Entry/exit thresholds (52%/46% with EMA filter)
     - Cross-asset structure check (BTC + ALT alignment)

Phase 3 (V4.2):
  ⬜ Delta Dashboard — integrate with portfolio_matrix.py
     - Real-time delta display per tier
     - Auto-hedge trigger alerts
     - Implied BTC exposure monitor
```

### 5.4 Risk Warnings

| Algorithm | Key Risk | Mitigation |
|-----------|---------|-----------|
| DOM-X | BTC.D can stay extended for weeks | Time-stop (72h); avoid in HIGH RISK |
| FUNDING-ARB | Impermanent loss on spot leg | Max 48h hold; rebalance if spot >1% |
| BETA-HEDGE | Over-hedging kills ALT PnL | Cap hedge at 50%; use BTC strength signal |

---

*Document version: 1.0 | Next review: After V4.0 backtesting results*
