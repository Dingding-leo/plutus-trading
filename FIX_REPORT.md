## Fix 1: Backtest CLI `--help` crash

**Problem**
- `python -m src.backtest --help` crashed with `ValueError: badly formed help string`.

**Root Cause**
- `argparse` formats help strings using `%` substitution; the literal `1%` in the help text was interpreted as a format token.

**Change**
- Escaped the literal percent sign in the help string (`1%%`).
- File: [__main__.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/src/backtest/__main__.py)

**Test Coverage**
- Added subprocess smoke test to ensure `--help` exits successfully.
- File: [test_regressions.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/tests/test_regressions.py)

---

## Fix 2: Binance client JSON decode handler raised `NameError`

**Problem**
- If Binance returned invalid JSON, the code attempted to catch `json.JSONDecodeError` but could itself raise `NameError: name 'json' is not defined`.

**Root Cause**
- `json` was referenced but never imported in the module.

**Change**
- Added `import json` in the Binance client module so the exception handler is valid.
- File: [binance_client.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/src/data/binance_client.py)

**Test Coverage**
- Added unit test that mocks `requests.get(...).json()` raising `json.JSONDecodeError` and asserts a clear error is raised.
- File: [test_regressions.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/tests/test_regressions.py)

---

## Fix 3: `PROJECT_ROOT` resolved outside the repository

**Problem**
- `PROJECT_ROOT` was computed as three parents above `src/config.py`, which can place `memory/` output outside the repo.

**Root Cause**
- Incorrect parent traversal when computing the repo root path.

**Change**
- Updated `PROJECT_ROOT` to `Path(__file__).resolve().parent.parent` (repo root containing `src/`).
- File: [config.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/src/config.py)

**Test Coverage**
- Added unit test asserting `config.PROJECT_ROOT` matches the test repo root.
- File: [test_regressions.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/tests/test_regressions.py)

---

## Fix 4: Negative recommended leverage in position sizing

**Problem**
- `recommended_leverage` could become negative when `max_leverage < 5`, producing nonsensical outputs.

**Root Cause**
- The formula `min(max_leverage * 0.8, max_leverage - 5)` can be negative for low max leverage values.

**Change**
- Clamped `recommended_leverage` to be within `[0, max_leverage]`, and ensured it is at least `1.0` when leverage is viable.
- File: [position_sizer.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/src/execution/position_sizer.py)

**Test Coverage**
- Added unit test verifying recommended leverage is never negative and never exceeds max leverage.
- File: [test_regressions.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/tests/test_regressions.py)

---

## Fix 5: Bare `except:` blocks catching `SystemExit`/`KeyboardInterrupt`

**Problem**
- Multiple modules used `except:` which can swallow `KeyboardInterrupt`/`SystemExit`, making CLI/backtests harder to stop and masking failures.

**Root Cause**
- Overly broad exception handlers.

**Change**
- Replaced `except:` with `except Exception:` across backtest strategies and utilities.
- Files:
  - [llm_strategy.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/src/backtest/llm_strategy.py)
  - [strategy.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/src/backtest/strategy.py)
  - [data_client.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/src/backtest/data_client.py)
  - [improved_strategy.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/src/backtest/improved_strategy.py)
  - [optimized_strategy.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/src/backtest/optimized_strategy.py)
  - [simple_fetch.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/src/backtest/simple_fetch.py)
  - [production_strategy.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/src/backtest/production_strategy.py)
  - [complete_trading_system.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/src/backtest/complete_trading_system.py)
  - [complete_strategy.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/src/backtest/complete_strategy.py)
  - [aggressive_strategy.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/src/backtest/aggressive_strategy.py)
  - [workflow_strategy.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/src/backtest/workflow_strategy.py)
  - [ema_strategy.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/src/backtest/ema_strategy.py)
  - [simple_strategy.py](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/src/backtest/simple_strategy.py)

**Test Coverage**
- Covered indirectly by CLI smoke tests and unit tests added above.

---

## Repo Hygiene: Add root `.gitignore`

**Issue**
- Generated/stateful folders (e.g., `.venv/`, caches, logs) are present in-repo and are easy to accidentally commit.

**Change**
- Added a root [.gitignore](file:///Users/austinliu/Documents/Personal%20Projects/Plutus/.gitignore) to ignore common generated artifacts.
