# Plutus V4.0 — Self-Evolving MoE Architecture

> **Status:** Implemented
> **Date:** 2026-03-22
> **Component:** Alpha Research — `src/models/`

---

## 1. Current State (Static Baseline)

Plutus V3.x uses a **fixed, uniform** MoE weighting scheme:

```python
# V3 static weights (hard-coded in src/data/personas.py)
WEIGHTS = {
    "Momentum":      0.33,
    "MeanReversion": 0.33,
    "Breakout":      0.33,
}
```

The `ScannerConfig` is also static — parameters like `sweep_threshold = 0.002` and `min_confidence_threshold = 60` never adapt to changing market regimes. This means:

- The system cannot adapt when one persona's edge degrades.
- The scanner fires identically in low-vol (tight ranges) and high-vol (trending) environments.
- Losing trades are logged but not systematically converted into corrective lessons.

---

## 2. Target State — RLHF Loop

The V4.0 self-evolving loop closes the gap between static parameters and dynamic market reality:

```
┌─────────────────────────────────────────────────────────────────────┐
│                         RLHF EVOLUTION LOOP                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────┐    ┌──────────┐    ┌──────────────┐                  │
│  │  TRADE   │───▶│  OUTCOME │───▶│  REFLEXION   │                  │
│  │ executed │    │  PnL     │    │  LLM prompt  │                  │
│  └──────────┘    └──────────┘    └──────────────┘                  │
│                                           │                         │
│                                           ▼                         │
│  ┌──────────┐    ┌──────────┐    ┌──────────────┐                  │
│  │ NEW TRADE│◀───│  CONFIG  │◀───│   EVOLVE     │                  │
│  │  with    │    │  UPDATE  │    │  GA + Weights│                  │
│  │updated   │    │          │    │              │                  │
│  │  params  │    └──────────┘    └──────────────┘                  │
│  └──────────┘                                                   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### Step-by-step

| Step | Component | Action |
|------|-----------|--------|
| 1. Trade | `execution/*` | Execute signal from weighted persona blend |
| 2. Outcome | `storage/*` | Record realised PnL in SQLite memory bank |
| 3. Reflexion | `ReflexionEvolver` | LLM generates diagnostic lesson from losing trades |
| 4. Evolve — Weights | `MoEWeighter` | Update rolling Sharpe ratios; softmax reallocates weights |
| 5. Evolve — Scanner | `GeneticOptimizer` | Tournament selection + crossover + mutation on `ScannerConfig` |
| 6. New Trade | All components | Next cycle uses evolved parameters |

---

## 3. Genetic Algorithm Design

### 3.1 Chromosome Encoding

The `ScannerConfig` is encoded as a 4-element real-valued chromosome:

```
Chromosome = [sweep_threshold, vol_squeeze_atr_mult, deviation_z_score, min_confidence_threshold]
            ─────────────────────────────────────────────────────────────────────────────────────
            Gene 0              Gene 1                    Gene 2                Gene 3
```

**Allele bounds:**

| Gene | Field | Lower | Upper |
|------|-------|-------|-------|
| 0 | `sweep_threshold` | 0.0001 | 0.05 |
| 1 | `vol_squeeze_atr_mult` | 0.05 | 3.0 |
| 2 | `deviation_z_score` | 0.5 | 5.0 |
| 3 | `min_confidence_threshold` | 0 | 100 |

### 3.2 Fitness Function

```
fitness(config) = rolling_30day_Sortino(config)
```

Where:

```
Sortino = (mean_return − target_return) / downside_deviation

target_return = 0  (simplified; can be replaced with the risk-free rate)
downside_deviation = sqrt(mean(max(0, −r_i)²))
```

Rolling 30-day Sortino is recomputed per persona, then aggregated using the current `MoEWeighter` weights to produce a single scalar fitness per config.

### 3.3 Selection — Tournament Selection (k=4)

```
1. Randomly sample k = 4 individuals from the population
2. Return the individual with the highest fitness
3. Repeat to select both parents
```

Tournament selection is elitist-preserving: the top-2 individuals by fitness are copied unchanged into the next generation (elitism = 2).

### 3.4 Crossover — Blend Crossover (BLX-α)

For each gene `i`:

```
child_gene_i = U(min(parent1_i, parent2_i) − α × range,
                  max(parent1_i, parent2_i) + α × range)

where α = 0.5  (standard BLX)
and   range = |parent1_i − parent2_i|
```

Integer-valued genes (`min_confidence_threshold`) are rounded to the nearest integer after crossover.

### 3.5 Mutation — Additive Gaussian

```
gene_i_new = gene_i_old + N(0, σ²)

σ = 0.01  (per gene, normalised to gene range)
```

Mutation probability is 100 % per gene (self-regulating due to small σ — most mutations are negligible).

### 3.6 Population & Generations

| Parameter | Value |
|-----------|-------|
| Population size | 32 |
| Elitism count | 2 |
| Selection | Tournament (k=4) |
| Crossover | Blend (BLX-0.5) |
| Mutation | Gaussian (σ=0.01) |
| Convergence | ~20–50 generations (fitness plateau) |

---

## 4. MoE Dynamic Weighting

### 4.1 Rolling Sharpe Ratio

Each persona maintains a rolling window of the last 30 realised returns. The Sharpe ratio for persona `i` at time `t` is:

```
SR_i(t) = mean(r_i[t−29:t]) / std(r_i[t−29:t])
```

If `len(window) < 5`, the persona is considered data-scarce and receives a neutral Sharpe of 0.0 (which collapses to uniform weight under softmax).

### 4.2 Softmax Weight Allocation

```
w_i = exp(SR_i / T) / Σ_j exp(SR_j / T)
```

| Parameter | Effect |
|-----------|--------|
| `T = 1.0` | Standard softmax (moderate concentration) |
| `T < 1.0` | Sharper weights — winner-take-most |
| `T > 1.0` | Flatter weights — more uniform |

The softmax is applied to the vector `[SR_1, SR_2, ..., SR_N]` for `N` personas. Subtracting the maximum value before exponentiation ensures numerical stability (equivalent transformation).

### 4.3 Lookback Window Rationale

- **30 days** ≈ one trading month at one signal per day.
- Short enough to adapt to regime changes (e.g., a persona's edge degrades after a market structure change).
- Long enough to smooth noise (daily PnL variance is high).

The window is implemented as a `collections.deque(maxlen=30)` in `MoEWeighter`.

### 4.4 Minimum Sample Gate

```
if len(returns_i) < 5:
    SR_i = 0  →  w_i = 1/N (uniform)
```

This prevents a persona with 1–2 lucky trades from receiving outsized weight.

---

## 5. Reflexion Evolver

### 5.1 Lesson Generation Pipeline

```
1. Extract all trades with pnl < 0 from SQLite memory bank
2. For each losing trade, format a reflexion prompt:
   Prompt includes: persona, entry, exit, pnl, market context
3. Call LLM with the prompt → parse JSON {anomaly_type, lesson_text}
4. Deduplicate via cosine similarity (TF-IDF vectors)
5. Contradiction check (keyword negation overlap)
6. Store approved lesson in rlhf_lessons table
```

### 5.2 Deduplication — Cosine Similarity

All stored lesson texts are converted to TF-IDF vectors. Before inserting a new lesson:

```
similarity = max_i cosine(TF-IDF(new_lesson), TF-IDF(existing_i))
if similarity > 0.85:
    skip  # duplicate detected
```

### 5.3 Contradiction Detection

A new lesson is discarded if it semantically contradicts an existing lesson. Detection is keyword-based:

1. Extract negation prefixes from new lesson: `["not", "never", "don't", "avoid"]`
2. Extract the concept words following the negation
3. If any concept word appears **without** negation in an existing lesson → contradiction → discard

Example:
- Existing: `"Always fade the 结构 break on high-volume spike."`
- New: `"Never fade the 结构 break"` → **contradiction** (discarded)

### 5.4 Anomaly Taxonomy

Lessons are tagged with one of:

| `anomaly_type` | Description |
|----------------|--------------|
| `liquidity_sweep` | Price wicked through level, triggered stops, then reversed |
| `structure_break` | 30m/1h structure broke without confirmation |
| `fakeout` | False breakout below/above key level |
| `news_gap` | Overnight news caused gap beyond stop |
| `volatility_explosion` | ATR spike (≥ 1.5× baseline) caught stop |

---

## 6. Risk of Overfitting

### 6.1 Minimum Sample Requirements

| Component | Gate | Purpose |
|-----------|------|---------|
| `MoEWeighter` | ≥ 5 samples per persona | Prevents over-weighting lucky streaks |
| `GeneticOptimizer` | Fitness dict must include score for each individual | Unknown individuals receive neutral fitness (0.0) |
| `ReflexionEvolver` | ≥ 5 losing trades before generating lessons | Prevents over-reaction to small sample |

### 6.2 Out-of-Sample Testing Gate

Before a newly evolved `ScannerConfig` is deployed to production:

```
1. Paper-trade the new config for N = 50 trades (out-of-sample window)
2. Compute out-of-sample Sortino
3. If oos_Sortino < in-sample_Sortino × 0.7 (70 % threshold):
       → DISCARD config; revert to previous generation's elite
4. If oos_Sortino ≥ threshold:
       → DEPLOY new config
```

This prevents the GA from over-fitting to in-sample noise.

### 6.3 Diversity Maintenance

The GA uses elitism = 2 only. At least 30 / 32 individuals are replaced each generation, maintaining genetic diversity and reducing the risk of premature convergence.

### 6.4 Regime-Aware Weight Reset

If `MoEWeighter` detects that all Sharpe ratios are negative (adverse market conditions for all personas):

```
if all(SR_i < 0 for i in personas):
    weights = uniform  # pause specialisation; treat as no-edge environment
```

This prevents the system from doubling down on a losing strategy.

---

*Document version: 1.0 — Plutus V4.0 Alpha Research*
