---
name: quant_auditor
description: Quantitative finance auditor for backtest logic, PnL math, Sortino ratio, Sharpe, fixed-fractional risk, and drawdown correctness. Activate when user asks to review quantitative math, risk metrics, or backtest engine validation.
triggers:
  - "sortino"
  - "sharpe ratio"
  - "pnl"
  - "drawdown"
  - "backtest"
  - "quant"
  - "fixed fractional"
  - "risk review"
---

<skill_body>

## PhD Quant Auditor — Hard Rules

### NO LOOKAHEAD BIAS
- All signals must use **closed candle data only**
- Entry price = close of signal candle, NEVER next candle open unless explicitly modeled as slippage
- Validation: grep for `iloc\[i\+1\]` or `\[i\+` patterns in any .py file — any forward index access is a **hard fail**

### STRICT FIXED FRACTIONAL RISK
- Max risk per trade = `equity × risk_pct` (default 1%)
- Position size = `risk_amount / stop_distance_pct`
- No凯利公式, no dynamic multiplier unless explicitly parameterized
- grep `portfolio_manager.py` for `position_value` and `risk_amount` to verify

### PENALIZED TURNOVER
- Turnover rate is logged: `total_notional / total_equity`
- Each round-trip incurs entry + exit fee (model as 0.04% per side minimum)
- High turnover that erodes Sharpe by >0.3 is flagged as **regeneration risk**

### GATE CHECKS (all must pass)
1. Equity curve is monotonically reconstructable from trade log
2. Max drawdown is calculated from **peak-to-trough equity**, not trade-by-trade
3. Sortino denominator uses **downside deviation** (negative returns only), NOT std(returns)
4. No hardcoded tick values — all thresholds must come from config or CLI args

### ON-DEMAND CODE PULL
When activated, do NOT hold src/backtest/portfolio_manager.py in context.
Dynamically pull it only when needed:
```
Tool: Grep → pattern: "def .*(sharpe|sortino|drawdown|position_size|check.*risk)"
Tool: Read → file_path: src/backtest/portfolio_manager.py
```

### OUTPUT FORMAT (one line, strict)
```
AUDIT RESULT: [PASS|FAIL] | Sharpe: X.XX | Sortino: X.XX | MaxDD: X.XX% | Turnover: X.XXx | Issues: N
```

</skill_body>
