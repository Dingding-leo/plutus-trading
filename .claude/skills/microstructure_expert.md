---
name: microstructure_expert
description: ICT Killzones, timezone-aligned liquidity sweeps, OBD (Order Block Deficits), FVGs, and exchange liquidity dynamics. Activate when user asks about intraday liquidity, killzones, ICT concepts, or microstructure trading logic.
triggers:
  - "killzone"
  - "ICT"
  - "liquidity sweep"
  - "order block"
  - "FVG"
  - "microstructure"
  - "liquidity grab"
  - "exchange volume"
  - "Binance volume"
  - "spot dip"
  - "OBD"
  - "Fair Value Gap"
---

<skill_body>

## Microstructure Expert — Hard Rules

### ICT KILLZONE TIMES (UTC)
| Killzone | Session | UTC Window | High-Probability Move |
|----------|---------|------------|------------------------|
| London Open | EU Session | 07:00–08:00 UTC | Directional bias |
| NY Open | US Session | 12:30–13:30 UTC | Largest volatility |
| NY Close (4pm) | London Kill | 15:00–16:00 UTC | Liquidity sweep |

- Only trade killzone edges between 12:30–13:30 UTC (NY Open) unless Austin specifies otherwise
- Avoid trading 30 min before and after major macro events (CPI, NFP, FOMC)

### TIMEZONE ALIGNMENT RULE
- All candle timestamps must be UTC-normalized before analysis
- Local timezone offsets are NOT acceptable — confirm via `df['timestamp'].dt.tz_convert('UTC')`
- Timeframe alignment: 5m, 15m, 30m candles must all be anchored to the same UTC reference

### LIQUIDITY SWEEP LOGIC
A liquidity sweep is VALID when:
1. Price wicked **beyond** a structural high/low (HH/HL or LH/LL)
2. The break was **rapid** (≤5 candles) — slow breaks = not liquidity, is direction
3. **Immediate reversal** follows (engulfing, hammer, or 3-candle reversal pattern)
4. Volume on the sweep candle was in the **top 20%** of the lookback window

### ORDER BLOCK (OB) RULES
- Bullish OB = last **bearish candle before a sweep up** (institution absorbed selling)
- Bearish OB = last **bullish candle before a sweep down** (institution absorbed buying)
- OB is valid only if it has **not yet been swept** — swept OBs are invalidated
- Entry: pullback to the OB high/low zone + confirmation candle
- Stop: beyond the OB extreme by 0.2% buffer

### FVG (FAIR VALUE GAP) RULES
- FVG = gap between 3 consecutive candles (candle 1 and 3 don't overlap in price)
- Imbalance zones are magnets — price typically fills FVGs within 3–5 candles
- FVG fill + rejection = HIGH probability entry
- If FVG persists >10 candles unfilled, treat as structural zone (OB rules apply)

### LIQUIDITY POOL IDENTIFICATION (Binance-specific)
- Use `src/data/binance_client.py` — call `get_taker_long_short_ratio()` for OI bias
- High taker long/short ratio (>1.2) = retail long crowding = potential sweep up
- Low ratio (<0.8) = retail short crowding = potential sweep down
- Combine with volume profile: LVN = support, HVN = resistance (see LVN/HVN rules)

### EXECUTION GATE (all must pass)
1. Killzone time window confirmed (UTC)
2. Liquidity sweep identified (wick + rapid + reversal + volume)
3. OB or FVG confirmed and not yet swept
4. Timeframe resonance: at least 2 of 3 (5m, 15m, 30m) agree on zone
5. Risk/reward ≥ 1.5 including extension target

### OUTPUT FORMAT (strict)
```
MS RESULT: [TRADE|SKIP] | Zone: [OB/FVG/SWEEP] | Direction: [LONG/SHORT] | Entry: $XXXX | Stop: $XXXX | RR: X.X | Killzone: [LONDON|NY|CLOSE|NONE]
```

</skill_body>
