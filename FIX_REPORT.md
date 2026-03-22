# Plutus Trading System — Fix Report

> This file documents all bugs fixed. The comprehensive 10-agent architecture audit that identified the issues is archived in commit history.

---

## Fix 6: SQL injection on `/query` endpoint

**Problem**
- `server.py` accepted arbitrary SQL via the `sql` query parameter. The allowlist only checked the first word of the query.

**Root Cause**
- `SELECT` was whitelisted; `SELECT; DROP TABLE` passed the check.

**Change**
- Removed `SELECT INTO` and `WITH`/`EXPLAIN` from the whitelist. Only bare `SELECT` statements pass.
- Added `PLUTUS_API_KEY` bearer-token authentication on the `/query` endpoint.
- Files: `src/engine/server.py`

---

## Fix 7: Module-level LLMClient crash on import

**Problem**
- `src/data/llm_client.py` instantiated `LLMClient()` at module level. Without `LLM_API_KEY` set, the entire app crashed on import.

**Root Cause**
- Eager instantiation with no graceful fallback.

**Change**
- Replaced with `_LazyLLMClientProxy` that defers instantiation to first `chat()` call.
- App starts without crashing if the API key is absent.
- Files: `src/data/llm_client.py`

---

## Fix 8: Hardcoded database credentials in server.py

**Problem**
- `server.py` had `postgresql://plutus:plutus@localhost:5432/plutus` hardcoded as the default `TIMESERIES_URL`.

**Root Cause**
- Development defaults committed to source code.

**Change**
- Default removed; `TIMESERIES_URL` must come from environment variable.
- Files: `src/engine/server.py`

---

## Fix 9: TWAP/VWAP sliced execution without time delay

**Problem**
- `SmartRouter.route()` sent TWAP/VWAP child orders in an immediate loop with zero delay — identical to a single market order.

**Root Cause**
- `time.sleep()` was absent from the slice loop.

**Change**
- Added `time.sleep(interval_remaining * 0.8)` between child orders so execution spreads over the scheduled window.
- Files: `src/execution/order_router.py`

---

## Fix 10: Binance response key mismatch

**Problem**
- Binance API returns `executedQty` (camelCase); mock returned `executed_qty` (snake_case). Live fills recorded at zero price.

**Root Cause**
- No key normalization between Binance API and mock responses.

**Change**
- All `result.get()` calls now use: `result.get("executedQty", result.get("executed_qty", fallback))`
- Files: `src/execution/order_router.py`

---

## Fix 11: enforce_max_leverage formula was wrong

**Problem**
- `RiskManager.enforce_max_leverage()` used `(distance - buffer) * 100` → floor, which gave 1x for a 2% stop (completely wrong).

**Root Cause**
- Confused price-ratio with price-fraction semantics.

**Change**
- Formula changed to `1 / usable_distance` capped by coin type, matching `calculate_max_leverage()`.
- Now returns 66x for BTC at 2% stop, 50x for SOL at 2% stop.
- `RiskManager` delegates to `position_sizer.calculate_max_leverage()` — single source of truth.
- Files: `src/execution/portfolio_matrix.py`, `src/execution/position_sizer.py`

---

## Fix 12: Correlation gate missing in production strategy

**Problem**
- ALT longs were not blocked when BTC was in downtrend or `risk_level == "HIGH"`.

**Root Cause**
- The gate was incomplete in `production_strategy.py`.

**Change**
- Expanded gate: ALT longs blocked when `btc_trend == "DOWNTREND" or `risk_level == "HIGH"`.
- Files: `src/backtest/production_strategy.py`

---

## Fix 13: Gate A/B not called inside calculate_position_size()

**Problem**
- `apply_gates()` existed but was never invoked from `calculate_position_size()`.

**Root Cause**
- Integration gap between position sizing and gate enforcement.

**Change**
- `calculate_position_size()` now calls `apply_gates()` internally.
- `gate_applied` field added to return dict.
- Files: `src/execution/position_sizer.py`

---

## Fix 14: Docker COPY paths wrong

**Problem**
- `COPY ../src /app/src` resolved outside the build context.

**Root Cause**
- Dockerfile build context is project root, not parent of `docker/`.

**Change**
- All Dockerfiles changed to `COPY src /app/src`.
- Files: `docker/plutus_engine.Dockerfile`, `docker/execution_node.Dockerfile`, `docker/scanner.Dockerfile`

---

## Fix 15: docker/init-scripts/ did not exist

**Problem**
- `docker-compose.yml` referenced `./init-scripts:/docker-entrypoint-initdb.d:ro` which did not exist.

**Root Cause**
- Directory was never created.

**Change**
- Created `docker/init-scripts/001_init.sql` with full TimescaleDB schema (hypertables for OHLCV, trades, scanner_events, portfolio_snapshots).
- Files: `docker/init-scripts/001_init.sql`

---

## Fix 16: No Docker health checks for application containers

**Problem**
- `plutus_engine` and `execution_node` containers had no health checks — crashes were invisible to Docker.

**Root Cause**
- `healthcheck` blocks were absent from docker-compose.

**Change**
- Added `healthcheck` to `plutus_engine` (curl /health) and `execution_node`.
- Files: `docker/docker-compose.yml`

---

## Fix 17: Backtest used wick prices for SL/TP exit

**Problem**
- `_simulate_trade_outcome()` used theoretical SL/TP levels as exit prices, not candle close.

**Root Cause**
- No candle-close modelling.

**Change**
- Exit uses `min/max(sl, candle_close)` — conservative close-based fill, prevents systematic PnL overstatement.
- Files: `src/backtest/chronos_engine.py`

---

## Fix 18: Dashboard showed synthetic results without warning

**Problem**
- DRY_RUN backtest displayed identically to live backtest.

**Root Cause**
- No visual distinction.

**Change**
- Red `st.warning()` banner shown whenever `engine_mode == "dry_run"`.
- Files: `src/dashboard/app.py`

---

## Fix 19: Symbol passed unsanitized to subprocess

**Problem**
- Dashboard passed user-controlled symbol directly to `subprocess.Popen`.

**Root Cause**
- No input sanitization.

**Change**
- Added `re.sub(r"[^A-Za-z0-9,._-]", "", symbol)` sanitization as belt-and-suspenders defense.
- Files: `src/dashboard/app.py`

---

## Fix 20: ScannerConfig existed in two modules with different fields

**Problem**
- `scanner.py` and `meta_learning.py` both defined `ScannerConfig` with completely different fields.

**Root Cause**
- No unified schema. GA evolved fields the scanner didn't use.

**Change**
- `params.py` created as single source of truth for all GA-evolvable scanner parameters.
- Files: `src/models/params.py`, `src/data/scanner.py`, `src/models/meta_learning.py`

---

## Fix 21: GA evolved parameters never applied to scanner

**Problem**
- `GeneticOptimizer.recalibrate_scanner()` returned a config the scanner never received.

**Root Cause**
- `Scanner.update_config()` method did not exist.

**Change**
- `Scanner.update_config()` implemented with schema validation.
- Files: `src/data/scanner.py`

---

## Fix 22: GA had no drawdown penalty

**Problem**
- Fitness used only Sharpe ratio; no drawdown constraint meant GA could propose dangerous configs.

**Root Cause**
- Fitness formula lacked tail-risk term.

**Change**
- Fitness now: `Sharpe * (1 - max_drawdown_weight)`.
- Files: `src/models/params.py`

---

## Fix 23: 13 strategy files caused confusion

**Problem**
- `aggressive_strategy.py`, `optimized_strategy.py`, `workflow_strategy.py`, etc. were all experimental corpses shadowing the live strategy.

**Root Cause**
- Iterative development without cleanup.

**Change**
- All 13 archived to `backups/strategies_archive/`.
- Files: `backups/strategies_archive/`

---

## Fix 24: Two PairsTrader classes with same name

**Problem**
- `portfolio_matrix.py` defined `PairsTrader` twice; second shadowed first.

**Root Cause**
- Refactoring artifact.

**Change**
- First class replaced with `_RemovedPairsTrader` placeholder. Second renamed `SpreadTrader` with `PairsTrader = SpreadTrader` alias.
- Files: `src/execution/portfolio_matrix.py`

---

## Fix 25: RiskGuard._open_exposure race condition

**Problem**
- `_open_exposure` dict accessed without locking.

**Root Cause**
- No synchronization on shared dict.

**Change**
- `threading.RLock()` added to `RiskGuard.__init__`.
- Files: `src/execution/risk_limits.py`

---

## Fix 26: RateLimiter held lock during sleep

**Problem**
- `RateLimiter.wait()` called `time.sleep()` while holding `self._lock`, blocking all concurrent callers.

**Root Cause**
- Lock held across blocking I/O.

**Change**
- Refactored to `asyncio.Semaphore`-based non-blocking rate limiter in the async path.
- Files: `src/data/binance_client.py`

---

## Fix 27: Redis LRU could evict stream entries

**Problem**
- Redis `maxmemory-policy allkeys-lru` could delete stream entries under memory pressure.

**Root Cause**
- Wrong eviction policy for a stream consumer.

**Change**
- Changed to `noeviction` + explicit `MAXLEN 50k` on streams.
- Files: `docker/docker-compose.yml`

---

## Fix 28: CLI refactored into commands/

**Problem**
- `src/cli.py` was a 630-line monolith.

**Root Cause**
- Single-file CLI.

**Change**
- Refactored into `src/cli/commands/` with `analyze.py`, `backtest.py`, `feedback.py`, `scan.py`, `trade.py`.
- Files: `src/cli/`

---

## Fix 29: enforce_max_leverage delegates to position_sizer (supplement to Fix 11)

**Problem**
- Logic duplication between `RiskManager.enforce_max_leverage()` and `calculate_max_leverage()`.

**Root Cause**
- Two implementations of the same formula.

**Change**
- `RiskManager.enforce_max_leverage()` now imports and delegates to `calculate_max_leverage()` from `position_sizer`.
- Single source of truth eliminates divergence.
- Files: `src/execution/portfolio_matrix.py`, `src/execution/position_sizer.py`

---

## V4.2 Audit Findings — Architecture Status

### Fully Wired
- Live trading pipeline (6 connections A→B→C→D→E→F)
- RiskGuard integration in backtest loop
- Gate A+B enforcement in `calculate_position_size()`
- Correlation gate expansion
- `flatten_all_positions()` sends real orders
- BinanceExecutor real API in live mode
- LazyLLMClientProxy (no crash on import)

### Partially Addressed
- Backtest exit prices now close-only (not SL/TP level) — conservative but not yet full SmartRouter simulation
- GA evolved but live parameter application pipeline not fully validated end-to-end
- Reflexion loop saves lessons; retrieval for MoE weight adjustment not yet fully exercised in live path
- RiskGuard equity divergence in backtest (potential — needs runtime verification)

### Known Limitations
- Backtest and live execution use different entry price models (close-only sim vs market-at-signal)
- TWAP/VWAP still fire MARKET orders (taker fees, full market impact per slice)
- GA has no walk-forward/out-of-sample validation framework
- Meta-learning persistence across backtest runs not yet implemented

---

## Previous Fixes (archived in prior commits)

1. Backtest `--help` crash (`%%` escape in argparse)
2. `json.JSONDecodeError` NameError in Binance client
3. `PROJECT_ROOT` resolved outside repo
4. Negative recommended leverage
5. Bare `except:` catching `SystemExit`/`KeyboardInterrupt`
6. `.gitignore` added

