# Trading Analysis Agent Workflow

> **Purpose:** Create a systematic workflow for market analysis and trading decision support
> **Created:** 2026-03-01
> **Author:** Evo (with Austin's guidance)

---

## 1. Overview

### 1.1 Workflow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                     DAILY WORKFLOW                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    │
│   │   Step 1     │───▶│   Step 2     │───▶│   Step 3     │    │
│   │  Data Collection│   │ News Monitor │   │ Technical    │    │
│   └──────────────┘    └──────────────┘    │  Analysis    │    │
│                                             └──────────────┘    │
│                                                   │             │
│                                                   ▼             │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    │
│   │   Step 6     │◀───│   Step 5     │◀───│   Step 4     │    │
│   │   Learn &    │    │  Reasoning   │    │   Market     │    │
│   │   Remember   │    │  & Judgment  │    │   Context    │    │
│   └──────────────┘    └──────────────┘    └──────────────┘    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 Key Principles

1. **Never assume** - Always get fresh data
2. **Check news first** - Major events override technicals
3. **Quantify what you can** - Use data for signals
4. **Acknowledge what you can't** - Human judgment for the rest
5. **Ask for feedback** - Learning requires correction

---

## 2. Step-by-Step Execution

### STEP 1: Data Collection

**Purpose:** Get current market data from multiple sources

#### 1.1 Price Data (Binance API)

```python
# Execute this for each pair: BTC, ETH, SOL, etc.
curl "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=200"
```

**Collect:**
- OHLCV data (open, high, low, close, volume)
- Timeframes: 1h, 4h, 1d
- Pairs: BTC/USDT, ETH/USDT, SOL/USDT

#### 1.2 Market Metrics

```python
# Get from CoinGecko
curl "https://api.coingecko.com/api/v3/global"
```

**Collect:**
- Total market cap
- BTC dominance
- 24h trading volume
- Fear & Greed Index (if available)

#### 1.3 Output Format

Store in: `~/.openclaw/workspace/memory/daily_analysis/YYYY-MM-DD.md`

```
# Daily Market Data - 2026-03-01

## BTC/USDT
- Price: $67,000
- EMA50: $66,286
- EMA200: $66,550
- 24h Change: +5.94%
- Volume: $XX B

## Market Context
- Total Cap: $2.40T
- BTC Dominance: 56.1%
- Volume: $117.9B
```

---

### STEP 2: News Monitoring

**Purpose:** Get major news that affects markets

#### 2.1 Search Commands

Execute these searches daily:

```bash
# Major crypto news (past 24h)
web_search --freshness pd --count 10 --query "Bitcoin crypto major news today"

# Geopolitical (very important!)
web_search --freshness pd --count 5 --query "US Iran war conflict"
web_search --freshness pd --count 5 --query "Middle East crisis"

# Macro / Economy
web_search --freshness pw --count 5 --query "Federal Reserve interest rate"
web_search --freshness pw --count 5 --query "US economy news"

# Regulatory
web_search --freshness pw --count 5 --query "crypto regulation SEC"
```

#### 2.2 News Categories

| Priority | Category | Examples |
|----------|----------|----------|
| 🔴 Critical | Geopolitical | War, major political events |
| 🔴 Critical | Macro | Fed rate, GDP, inflation |
| 🟠 High | Crypto | ETF, major adoption |
| 🟡 Medium | Technical | Network upgrades |
| 🟢 Low | Community | Tweets, sentiment |

#### 2.3 News Template

```
## News Summary - 2026-03-01

### Critical (Read First!)
- [ ] Iran war: US/Israel attacked Iran, leader killed
- [ ] Market reaction: BTC +X%

### High Priority
- [ ] BTC facing $70K resistance
- [ ] Fear & Greed Index: 11 (extreme fear)

### Crypto Specific
- [ ] Circle earnings +34%
- [ ] Institutional adoption news
```

---

### STEP 3: Technical Analysis

**Purpose:** Calculate indicators and generate signals

#### 3.1 Required Calculations

```python
# For each pair (BTC, ETH, SOL):

# EMAs
EMA50 = calculate_EMA(closes, 50)
EMA200 = calculate_EMA(closes, 200)

# Trend detection
if EMA50 > EMA200: trend = "UPTREND"
elif EMA50 < EMA200: trend = "DOWNTREND"
else: trend = "SIDEWAYS"

# Support/Resistance
high_200 = max(closes[-200:])
low_200 = min(closes[-200:])
position_in_range = (current - low) / (high - low) * 100

# Momentum
change_24h = (close - close[-24]) / close[-24] * 100
change_7d = (close - close[-168]) / close[-168] * 100

# Volatility
volatility_30d = std(closes[-30:]) / mean(closes[-30:]) * 100
```

#### 3.2 Signal Generation

| Condition | Signal |
|-----------|--------|
| EMA50 crosses above EMA200 | BUY (Golden Cross) |
| EMA50 crosses below EMA200 | SELL (Death Cross) |
| Price > EMA200 + 5% | OVEREXTENDED (caution) |
| Price < EMA200 - 5% | OVERSOLD (potential bounce) |
| RSI < 30 | OVERSOLD |
| RSI > 70 | OVERBOUGHT |

#### 3.3 Output Template

```
## Technical Analysis - 2026-03-01

### BTC/USDT
| Indicator | Value | Signal |
|-----------|-------|--------|
| Price | $67,244 | - |
| EMA50 | $66,286 | - |
| EMA200 | $66,550 | - |
| Trend | DOWNTREND | SELL |
| 200h High | $69,328 | Resistance |
| 200h Low | $62,900 | Support |
| Position | 67.6% | Upper range |
| RSI(14) | TBD | - |
| 24h Change | +5.94% | Momentum up |
| 7d Change | -1.04% | Weak |

### ETH/USDT
[Same structure]
```

---

### STEP 4: Market Context

**Purpose:** Combine data + news to understand the big picture

#### 4.1 Questions to Answer

For each analysis, explicitly answer:

1. **What's the trend?** (up/down/sideways)
2. **What's the sentiment?** (fear/greed/neutral)
3. **Any major news?** (war, regulation, adoption)
4. **What's the risk level?** (high/medium/low)
5. **Any key levels?** (support/resistance)

#### 4.2 Market State Classification

```
HIGH RISK:
- Major geopolitical events (war)
- Regulatory news
- Extreme sentiment (Fear < 20 or Greed > 80)
- Breaking key support/resistance

MODERATE RISK:
- Normal market conditions
- No major news
- Trading in range

LOW RISK:
- Clear trend with good momentum
- No major threats
- Price near support in uptrend
```

---

### STEP 5: Reasoning & Judgment

**Purpose:** Generate analysis and trading recommendations

#### 5.1 Analysis Template

```
## Analysis & Recommendation - 2026-03-01

### Market Overview
[Brief 2-3 sentence summary of current state]

### BTC Analysis
- Trend: [UP/DOWN/SIDEWAYS]
- Key Levels: [resistance, support]
- Momentum: [strong/weak/neutral]

### Major Catalysts
1. [List any major news]
2. [List key technical levels]

### My Judgment
[Explicit statement: e.g., "I think the market will..."]
[Confidence level: High/Medium/Low]

### Reasoning
[Step-by-step explanation of my logic]

### Limitations
[What am I uncertain about?]
[What would change my view?]

### Recommendation
- Entry: [price level or condition]
- Stop: [price level]
- Target: [price level]
- Size: [position size recommendation]
```

#### 5.2 Key Phrases to Use

**Always include:**
- "My judgment is..." (explicit opinion)
- "I'm uncertain about..." (acknowledge limits)
- "This could change if..." (conditional)
- "Please correct me if..." (ask for feedback)

**Never say:**
- "The market will definitely..." (no certainty)
- "This is guaranteed..." (no guarantees)
- "I know..." (overconfidence)

---

### STEP 6: Learn & Remember

**Purpose:** Record feedback and improve

#### 6.1 Feedback Collection

After each analysis, explicitly ask Austin:

```
Please tell me:
1. Was my analysis correct?
2. What did I miss?
3. What was wrong?
4. What should I focus on next time?
```

#### 6.2 Learning Log

Store in: `~/.openclaw/workspace/memory/feedback/YYYY-MM.md`

```
# Feedback Log - March 2026

## 2026-03-01
### My Analysis
- Said: "No major catalysts"
- Reality: Iran war started

### Correction
- Austin pointed out: I need to check geopolitical news daily
- Lesson: Always search for major world news, not just crypto

### What I Learned
1. Need to search geopolitical news separately
2. Major events override technicals
3. Ask Austin what I missed
```

#### 6.3 Pattern Recognition

After 10+ feedbacks, look for patterns:

- What do I consistently miss?
- What type of news affects markets most?
- What technical signals work best?

---

## 3. Daily Schedule

### Option A: Manual Trigger

When Austin says "analyze market" or "check market":

1. Run full workflow (Steps 1-5)
2. Present analysis
3. Ask for feedback
4. Log feedback

### Option B: Scheduled (Heartbeat)

Every [morning/evening], automatically:

1. Collect data (Step 1)
2. Search news (Step 2)
3. Calculate indicators (Step 3)
4. Write to daily file
5. If major news → alert Austin

### Recommended Schedule

| Time | Action |
|------|--------|
| 8:00 AM AEST | Morning scan (data + news) |
| 12:00 PM AEST | Quick check |
| 6:00 PM AEST | Evening summary |
| On demand | Full analysis when asked |

---

## 4. Execution Checklist

### Before Any Analysis - DO THIS FIRST

```
☐ Get fresh price data from Binance
☐ Search for major geopolitical news
☐ Search for crypto news
☐ Search for macro/economic news
```

### During Analysis

```
☐ Calculate EMAs (50, 200)
☐ Identify trend
☐ Find support/resistance
☐ Check momentum (24h, 7d)
☐ Calculate volatility
☐ Answer: What's the trend?
☐ Answer: What's the sentiment?
☐ Answer: Any major news?
☐ Answer: What's the risk level?
```

### After Analysis

```
☐ Present in clean format
☐ State my judgment clearly
☐ Acknowledge limitations
☐ Ask: "Please correct me if wrong"
☐ Ask: "What did I miss?"
```

---

## 5. Tools Reference

### Data Collection

```bash
# Binance OHLCV
curl "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=200"

# CoinGecko global
curl "https://api.coingecko.com/api/v3/global"
```

### News Search

```bash
# Crypto news
web_search --freshness pd --count 10 --query "Bitcoin crypto news"

# Geopolitical
web_search --freshness pd --count 5 --query "US Iran war"

# Macro
web_search --freshness pw --count 5 --query "Federal Reserve"
```

---

## 6. Quality Standards

### Every Analysis Must Have:

1. **Data** - Numbers from API, not assumptions
2. **News** - At least check major headlines
3. **Technical** - EMA, support/resistance, momentum
4. **Judgment** - Explicit opinion
5. **Uncertainty** - What could change my view
6. **Question** - Ask for feedback

### Never Do:

1. Don't assume markets are quiet
2. Don't skip news check
3. Don't give false certainty
4. Don't ignore major events (wars, regulation)
5. Don't forget to ask for correction

---

## 7. Files & Locations

| File | Location |
|------|----------|
| Daily data | `~/.openclaw/workspace/memory/daily_analysis/YYYY-MM-DD.md` |
| Feedback log | `~/.openclaw/workspace/memory/feedback/YYYY-MM.md` |
| This workflow | `~/.openclaw/workspace/TRADING_WORKFLOW.md` |

---

## 8. Immediate Action Items

1. **Test now:** Run workflow on current market
2. **Get feedback:** Ask Austin what I missed
3. **Set schedule:** Decide on timing
4. **Refine:** Update based on lessons learned

---

*This workflow is a living document. Update as we learn.*

---

## 9. Position Sizing & Leverage Rules

> **Important:** These rules were learned from Austin on 2026-03-01. Always use these formulas.

### 9.1 Core Principle: Loss-Based Position Sizing (以损定仓)

**Never decide position size first. Always start with stop loss.**

```
Step 1: Find support level
Step 2: Place stop loss below support
Step 3: Calculate stop loss distance (%)
Step 4: Calculate max leverage based on distance
Step 5: Calculate position size based on max risk per trade
```

### 9.2 Risk Environment: Position Multiplier

> **Updated 2026-03-02**: Risk environment only affects position size, NOT direction.
> Direction is determined by structure/location/invalidation.
> Environment only determines how much you dare to risk.

| Risk Level | Definition | Position Multiplier (pos_mult) |
|------------|------------|-------------------------------|
| **LOW** | No major news + normal volatility + clear structure | 1.0x–1.2x |
| **MODERATE** | General news OR elevated volatility, but structure still tradable | 0.7x–1.0x |
| **HIGH** | CPI/FOMC/war/regulation OR ATR ≥ 1.5x OR structure broken | 0.3x–0.5x |

**HIGH RISK Triggers (any ONE triggers HIGH):**
- [ ] Major macro/CPI/FOMC/NFP/war escalation/SEC regulation
- [ ] 15m ATR ≥ 20-bar average × 1.5
- [ ] 30m structure just broke / key levels repeatedly false-break

### 9.3 New Position Sizing Formula (Final Version)

```
base_risk_$ = equity × 1%
effective_risk_$ = base_risk_$ × pos_mult
max_position_value = effective_risk_$ / stop_distance%

Gate A (Small Stop Penalty):
  if stop_distance% < 0.7% AND risk_env == HIGH:
      pos_mult = min(pos_mult, 0.3)

Gate B (Position Cap):
  max_position_value ≤ equity × 1.0 (training phase)
  max_position_value ≤ equity × 1.5 (advanced phase)
```

**Example: $10,000 account, 0.5% stop, HIGH RISK**
- base_risk = $100
- pos_mult = 0.4 (mid-point)
- effective_risk = $100 × 0.4 = $40
- max_position = $40 ÷ 0.5% = $8,000 (80% of capital)

> **HIGH RISK doesn't mean no trade. It means trade smaller.
> Direction is determined by structure. Position size is determined by environment.**

### 9.2 Leverage Calculation

**Key Insight:** Higher leverage is better, BUT you must stay above liquidation.

| Coin Type | Liquidation Distance | Formula |
|-----------|---------------------|---------|
| **Major** (BTC, ETH) | Distance + 0.5% | Max Leverage = Distance - 0.5% |
| **Small Cap** | Distance + 1-2% | Max Leverage = Distance - 1~2% |

**Examples:**

| Scenario | Stop Distance | Major Coin Max Leverage | Small Coin Max Leverage |
|----------|--------------|------------------------|------------------------|
| Support 2% away | 2% | 2% - 0.5% = 1.5% → ~50x | 2% - 1.5% = 0.5% → ~20x |
| Support 1% away | 1% | 1% - 0.5% = 0.5% → ~100x | 1% - 1.5% = negative → NO TRADE |
| Support 3% away | 3% | 3% - 0.5% = 2.5% → ~35x | 3% - 1.5% = 1.5% → ~50x |

### 9.3 Position Size Calculation

**Rule:** Risk maximum 1% of total capital per trade

```
Max Position Value = Total Capital × Risk % ÷ Stop Distance %
```

**Example:**
- Total capital: $10,000
- Risk per trade: 1% = $100
- Stop distance: 2%
- Max position: $100 ÷ 2% = $5,000 (50% of capital at 2x leverage)

### 9.4 Position Sizing with Leverage

```
Step 1: Determine risk amount (1% of capital)
Step 2: Calculate stop loss distance from support
Step 3: Calculate max leverage (Distance - 0.5% for major, -1.5% for small)
Step 4: Verify leverage doesn't exceed max
Step 5: Open position in tranches (分仓), not all at once

Tranche sizing:
- First entry: 50% of planned position
- Second entry: 30% on confirmation
- Final entry: 20% reserve
```

### 9.5 NEVER Do

- ❌ Use 100x leverage when support is 1% away (liquidation at 0.5%)
- ❌ Put entire capital in one trade
- ❌ Ignore liquidation distance
- ❌ Risk more than 1% per trade
- ❌ Open full position at once (分仓!)

### 9.6 Decision Checklist Before Entry

```
☐ Identified support level
☐ Stop loss placed below support
☐ Calculated stop distance (%)
☐ Calculated max leverage (Distance - 0.5% or -1.5%)
☐ Planned leverage is within limit
☐ Calculated position size (max 1% risk)
☐ Will open in tranches (not all at once)
☐ Have reserve for additional entries
```

### 9.7 Real Example: ETH Trade

```
Analysis:
- Current price: $2,005
- Support (EMA50): $1,980
- Stop loss: $1,960 (below support)
- Distance: ($2,005 - $1,960) / $2,005 = 2.24%

Calculation:
- Max leverage: 2.24% - 0.5% = 1.74% → ~50x
- Risk: 1% of $10,000 = $100
- Position size: $100 ÷ 2.24% = $4,464
- At 50x leverage: $4,464 = 44.6% of capital

Decision:
- Use 40-45x leverage (safe buffer)
- Open in 3 tranches: 50%/30%/20%
- First entry: $2,230 at ~45x
- Stop: $1,960
- Risk: ~$100 (1%)
```

---

## 10. Asset Selection Rules (System Constraints)

> **Critical:** These rules were learned from Austin on 2026-03-02. They are HARD CONSTRAINTS - not suggestions.

### 10.1 The "Not Strongest, But Hardest to Kill" Principle

> **Core Mindset:**
> ```
> NOT: "Which asset looks strongest?"
> BUT: "Which asset is least likely to get crushed?"
> ```

### 10.2 Asset Priority Hierarchy

```
BTC > ETH > ALT
```

**Rule:** If BTC has a strong signal, you MUST trade BTC. You are NOT allowed to "go around" BTC to trade altcoins.

### 10.3 Correlation Gate

```python
if macro == risk_off:
    if BTC shows weakness:
        → FORBID ALT LONG
```

**Why:** In risk-off:
- BTC drops first
- ETH follows
- ALTs (SOL, etc.) get crushed LAST and HARDEST

**Wrong thinking:** "SOL is strongest → go long SOL"
**Right thinking:** "BTC is weakening → no alt longs"

### 10.4 BTC as Market Anchor

**Critical Insight:** BTC is the "anchor" of the entire crypto market.

When BTC shows reversal/distribution signals:
- Do NOT look for "stronger" alts
- Either trade BTC (short) or NO TRADE
- ALT longs are forbidden

**BTC weakness signals (high weight):**
- Liquidity grab (swept highs, rejection)
- Distribution pattern
- OI up + price rejection
- Funding crowded longs

### 10.5 Risk-Off Behavior Pattern

| Phase | Market Behavior |
|-------|-----------------|
| Early | BTC drops |
| Mid | ETH follows |
| Late | ALTs get crushed worst |

**Implication:** Being "strongest" in risk-off is a trap.

### 10.6 Valid Trading Answers

For ANY question with:
- macro = risk_off
- BTC shows weakness

**Valid answers:**
- ✅ NO TRADE (no low-risk opportunity)
- ✅ BTC SHORT

**Forbidden:**
- ❌ ALT LONG (ANY altcoin)

### 10.7 Decision Checklist (Updated)

```
☐ Check macro (risk_on / risk_off)
☐ Check BTC status (strength / weakness)
☐ If risk_off + BTC weakness:
    → Valid answers: NO TRADE or BTC SHORT
    → ALT LONG is FORBIDDEN
☐ Apply asset priority: BTC > ETH > ALT
☐ If trading alt, verify:
    - macro = risk_on OR
    - BTC is neutral/strong
☐ Then apply position sizing rules
```

### 10.8 Example: The Trap

**Scenario:**
- Macro: risk_off (DXY up, Nasdaq down, liquidity warning)
- BTC: just swept highs, rejected, OI up
- SOL: strong uptrend, broke high

**Wrong answer:** SOL LONG (relative strength trap)
**Correct answers:** NO TRADE or BTC SHORT

---

### 10.9 Mindset Update (Memorize)

```
Not "pick the strongest"
But "pick the hardest to kill"

BTC is the anchor.
BTC weakness = no alt longs.
```

---




## 11. Execution Framework (Decision Engine)

> **Critical:** These rules were learned from Austin on 2026-03-02. 
> This transforms analysis into execution.

### 11.1 Core Problem Diagnosis

| Problem | Symptom | Solution |
|---------|---------|----------|
| Risk = No Trade | Skip when should reduce size | Risk → reduces position, not skip |
| T2 Avoidance | Structure confirmed but still NO TRADE | Must execute in T2 |
| Reversal思维 | Wait for perfect setup | Continuation = structure + control → go |
| Excuse-making | RR不够/手续费高 as skip reasons | Edge first, then TP design, then fees |

### 11.2 The Four Decision Variables

```
1. control       → 谁主导? (macro/timeframe alignment)
2. location      → 位置是否容易加速?
3. invalidation  → 我哪里错? (硬定义如下)
4. expansion     → 有没有空间? (在 extension 里找)
```

#### Invalidation 硬定义 (CRITICAL)

```
invalidation_clear = "acceptance below/above key level"

❌ WRONG: "我觉得 71,200 差不多是支撑"
✅ RIGHT: "price closes below 71,400 = invalidation"

两选一 stop 方案:
- 保守: stop = wick_low + buffer (结构失效)
- 更保守: stop = deeper level (允许一次 flush)
```

**核心:** Stop 必须是"可计算的价格点"，不是"我感觉"。

---

### 11.2.1 Standardized Trade Plan (Final Output Template)

```
Decision: [BUY/SELL] [ASSET]
Entry: [price zone] (limit preferred)
Stop: [exact price] (below/above key level)
Stop distance: [%] (must be ≥ 0.5% OR use stricter pos_mult)
TP1: [price] (XX% 减仓)
TP2: [price] (remaining)
RR: [distribution calculation - include fees]
Risk: pos_mult = [value]

⚠️ Check: stop 距离太小会被噪音扫 → 二选一:
- 用更宽 stop
- 用更严 pos_mult (0.3 instead of 0.4)
```

**Key insight:** "No expansion" is often wrong - expansion is in EXTENSION, not current move.

### 11.3 Three-Phase Decision Framework

#### PHASE 1: 未动 (No Movement)
```
if no trigger:
    → NO TRADE
```
**Reason:** No edge = no trade

#### PHASE 2: 冲击 (Shock)
```
if news / fake breakout:
    → DO NOT TRADE
    → DEFINE trigger for PHASE 3
```
**Key:** Waiting for confirmation, not safety

#### PHASE 3: 确认 (Confirmation)
```
if trigger hit:
    → CHECK Execution Gate
    → if pass → TRADE (MUST)
    → if fail → SKIP (must justify why)
```

### 11.4 Execution Gate (The Key)

```python
if (structure_break == True) and 
   (macro_aligned == True) and 
   (invalidation_clear == True) and 
   (RR >= 1.5 including extension):
    → EXECUTE TRADE
else:
    → NO TRADE (must prove why)
```

**Critical:** NOT "T2 must trade" - it's "T2 cannot avoid THE TRADE that meets criteria"

### 11.5 Risk Logic (Final)

```
edge  → 决定是否交易
environment → 决定仓位大小
```

**Formula:**
- base_risk = equity × 1%
- effective_risk = base_risk × pos_mult
- pos_mult: LOW=1.0x, MODERATE=0.7-1.0x, HIGH=0.3-0.5x

**Key insight:** HIGH RISK doesn't mean no trade. It means trade SMALLER.

### 11.6 Anti-Avoidance Mechanism

**Rule:** If outputting NO TRADE in PHASE 3, must explicitly prove ONE of:

```
1. control unclear OR
2. invalidation unclear OR  
3. RR < 1.5

Otherwise: → Mark as "AVOIDANCE BEHAVIOR"
```

**Why:** Prevents finding excuses not to trade.

### 11.7 Continuation vs Reversal

| Type | Mindset | Speed | Best For |
|------|---------|-------|----------|
| **Reversal** | Wait for confirmation | Slow | Ranging markets |
| **Continuation** | Wait for structure + control | Fast | Trending markets |

**Common mistake:** Using Reversal思维 for Continuation trades.

### 11.8 Final Mindset

```
没动 → 不做
刚动 → 不追
动完确认 → 必须做
```

**Translation:**
- No movement → no trade
- Just moved → don't chase
- Movement confirmed → MUST execute

### 11.9 Execution Checklist

```
PHASE 1:
☐ Is there a trigger?

PHASE 2:
☐ Defined trigger?
☐ Waiting for confirmation?

PHASE 3:
☐ Trigger hit?
☐ Check Execution Gate:
    ☕ structure_break?
    ☕ macro_aligned?
    ☕ invalidation_clear?
    ☕ RR >= 1.5 (including extension)?
☕ If all YES → EXECUTE
☕ If NO → Justify (not avoid)
☐ Then apply position sizing (environment multiplier)
```

---

## 12. Updated Decision Template

### Before making any trade decision, answer:

| Question | Answer |
|----------|--------|
| What's the trend? | |
| What's the support level? | |
| Where's the stop loss? | |
| What's the stop distance? | |
| Max leverage (major/small)? | |
| Planned leverage? | |
| Risk amount ($)? | |
| Position size ($)? | |
| Tranche plan? | |

### Trade Recommendation Must Include:

1. **Entry** - Price level or condition
2. **Stop Loss** - Below support
3. **Stop Distance** - Must calculate!
4. **Max Leverage** - Using formula above
5. **Planned Leverage** - Within max
6. **Position Size** - Based on 1% risk rule
7. **Tranches** - How to split entry
8. **Risk/Reward** - Must be positive

---

*Updated: 2026-03-01 - Added leverage and position sizing rules from Austin*

---

## 11. Intraday Trading System (New)

> **Added:** 2026-03-01 - From Austin's guidance

### 11.1 Core Principles

1. **All coins, no lazy filtering** - Monitor all 200+ coins, not just top 50
2. **Multi-timeframe** - Use 5m, 15m, 30m simultaneously
3. **Key levels** - Highs/lows + Volume Profile (LVN/HVN)
4. **Multi-timeframe resonance** - Higher win rate when all 3 timeframes agree

### 11.2 Timeframe Strategy

| Timeframe | Purpose | Signal Type |
|-----------|---------|-------------|
| 5m | Precise entry | Fast signals |
| 15m | Direction confirmation | Medium signals |
| 30m | Trend bias | Reliable signals |

### 11.3 Key Level Identification

| Method | Description | Signal |
|--------|-------------|--------|
| **Highs/Lows** | Period high/low points | Support/Resistance |
| **LVN** (Low Volume Node) | Fast drop area = potential support | BUY zone |
| **HVN** (High Volume Node) | High volume area = potential resistance | SELL zone |

**LVN/HVN Calculation:**
```
1. Divide price range into bins (e.g., 50 bins)
2. Calculate volume at each price level
3. LVN = price levels with LOW volume (gaps)
4. HVN = price levels with HIGH volume (clusters)
```

### 11.4 Trading Strategy

```
Multiple coins (200+)
    ↓
Multi-timeframe: 5m + 15m + 30m
    ↓
Identify key levels: highs/lows + LVN/HVN
    ↓
Check resonance:
  - 3 timeframes at key level = STRONG signal
  - 2 timeframes = MEDIUM signal
  - No alignment = NO TRADE
    ↓
Entry at LVN → Target HVN
    ↓
Calculate stop loss → Position sizing → Open in tranches
```

### 11.5 Multi-Timeframe Resonance Rules

| Condition | Action |
|-----------|--------|
| 5m + 15m + 30m ALL at key level | Strong signal - ENTER |
| 2 of 3 at key level | Medium signal - Consider |
| No alignment | NO TRADE |

### 11.6 LVN → HVN Strategy

**Logic:**
- Buy at LVN (low volume node = support)
- Sell at HVN (high volume node = resistance)

**Example:**
- LVN identified at $1,980 → Enter long
- HVN identified at $2,050 → Take profit
- Stop loss below LVN

### 11.7 Complete Intraday Scan Checklist

```
☐ Scan ALL coins (200+, not just top 50)
☐ For each coin, check 3 timeframes: 5m, 15m, 30m
☐ Identify highs/lows on each timeframe
☐ Calculate LVN and HVN using Volume Profile
☐ Find alignment: Are multiple timeframes at same key level?
☐ If resonance found:
  - Identify entry at LVN
  - Identify target at HVN
  - Calculate stop loss below LVN
  - Calculate position size (1% risk max)
  - Determine leverage (account for liquidation distance)
  - Plan tranches: 50% / 30% / 20%
☐ If no resonance → Skip to next coin
```

---

## 12. Volume Profile Calculation

### 12.1 Basic Formula

```python
def calculate_volume_profile(prices, volumes, bins=50):
    """Calculate volume at each price level"""
    price_range = max(prices) - min(prices)
    bin_size = price_range / bins
    
    profile = {}
    for i in range(bins):
        bin_low = min(prices) + i * bin_size
        bin_high = bin_low + bin_size
        
        # Sum volume where price was in this bin
        volume_in_bin = sum(
            volumes[j] for j in range(len(prices))
            if bin_low <= prices[j] < bin_high
        )
        profile[bin_low] = volume_in_bin
    
    return profile

def find_lvn(profile, threshold_percentile=20):
    """Find Low Volume Nodes"""
    volumes = sorted(profile.values())
    threshold = volumes[int(len(volumes) * threshold_percentile / 100)]
    return [price for price, vol in profile.items() if vol < threshold]

def find_hvn(profile, threshold_percentile=80):
    """Find High Volume Nodes"""
    volumes = sorted(profile.values())
    threshold = volumes[int(len(volumes) * threshold_percentile / 100)]
    return [price for price, vol in profile.items() if vol > threshold]
```

### 12.2 Real-time Calculation

For intraday scanning:
- Use last 100-200 candles
- Recalculate every 5-15 minutes
- Focus on recent LVN/HVN (not old ones)

---

*Updated: 2026-03-01 - Added intraday trading system from Austin*

---

## 13. Liquidity & Stop Hunting (流动性止损狩猎)

> **Added:** 2026-03-01 - From Austin's guidance

### 13.1 Core Concept

**The logic:**
1. Retail traders place stop losses below key levels
2. Institutions push price toward these stop loss levels
3. Retail gets stopped out = institutions get liquidity
4. After taking liquidity, institutions push price the other way

### 13.2 Liquidity Grab Pattern

```
Key Level (Support/Resistance)
    ↓
Price rapidly breaks through
    ↓
Retail stops get triggered
    ↓
Institutions have taken the liquidity
    ↓
Price reverses → Institution starts push
```

### 13.3 How to Identify "Liquidity Taken"

| Signal | Description |
|--------|-------------|
| **Rapid break** | Price quickly passes through key level |
| **Engulfing** | Bullish/bearish engulfing pattern |
| **Hammer/Shooting Star** | Reversal candlestick patterns |
| **V reversal** | Quick dump then quick recover |
| **Wick rejection** | Long wick testing level and rejecting |

### 13.4 Trading Strategy with Liquidity

```
1. Identify key level (support/resistance)
2. Price approaches level
3. WAIT for:
   - Quick break through level
   - Reversal signal (engulfing/hammer/V-reversal)
4. If both occur:
   - Liquidity has been taken
   - Institution is ready to push
   - ENTER in direction of reversal
5. Target: Opposite key level (HVN if long, LVN if short)
```

### 13.5 Liquidity + Multi-Timeframe

```
5m: Rapid break + reversal signal
15m: Confirms direction
30m: Trend alignment
    ↓
All 3 = HIGH PROBABILITY TRADE
```

### 13.6 Liquidity at Stop Loss

**Key insight:** Stop loss should be where liquidity is LIKELY to be taken

```
Support at $1,980
Stop below at $1,960
    ↓
Question: Is there enough buy liquidity at $1,960?
    ↓
If NO: Price will "穿透" (break through) support
If YES: Price might bounce

→ Look for liquidity grab signals before entry
```

### 13.7 Liquidity Grab Checklist

```
☐ Identified key level (high/low/LVN/HVN)
☐ Price approaching level
☐ Rapid break through level?
☐ Reversal signal present? (engulfing/hammer/V)
☐ If YES → Liquidity taken → Enter
☐ If NO → Wait for confirmation
☐ Target: Next key level (HVN/LVN)
☐ Stop: Previous liquidity pool
```

### 13.8 Example: ETH Trade

```
Scenario:
- Support: $1,980
- Stop: $1,960

Observation:
- Price rapidly drops from $1,990 to $1,950 (breaks support)
- 5m shows bullish engulfing at $1,960

Interpretation:
- Liquidity grabbed at $1,960
- Institutions have liquidity
- Prepare for upward push

Action:
- Enter long at $1,960 (after engulfing)
- Target: HVN at $2,050
- Stop: Below $1,940 (new liquidity pool)
```

---

## 14. Summary: Complete Intraday System

```
PHASE 1: SCAN
- All coins (200+)
- Multi-timeframe (5m, 15m, 30m)
- Identify key levels (highs/lows + LVN/HVN)

PHASE 2: ANALYSIS  
- Check multi-timeframe resonance
- Look for liquidity grab signals
- Confirm reversal patterns

PHASE 3: ENTRY
- Wait for confirmation (don't front-run)
- Entry at LVN (long) or HVN (short)
- Or entry after liquidity grab + reversal

PHASE 4: RISK
- Calculate stop loss distance
- Account for liquidation (major +0.5%, small +1.5%)
- Position size: max 1% risk
- Open in tranches: 50% / 30% / 20%

PHASE 5: MANAGEMENT
- Trail stop as price moves
- Target next key level (HVN/LVN)
- Exit on reversal signals
```

---

*Updated: 2026-03-01 - Added liquidity/stop hunting concepts*
