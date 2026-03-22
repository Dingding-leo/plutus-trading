#!/usr/bin/env bash
# ============================================================
# PLUTUS COMPLETION GATE — Pre-Commit Validation
# Runs before any git commit is completed.
# Exit 0 = commit proceeds. Exit 1 = commit blocked.
# ============================================================

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"

ERRORS=0

echo "🔒 [Plutus Gate] Running pre-commit validation..."

# ── Phase 1: Python syntax check ──────────────────────────
echo "  [1/3] Checking Python syntax..."
if ! python3 -m py_compile src/**/*.py 2>&1; then
    echo "  ❌ Syntax errors detected in src/**/*.py"
    ERRORS=$((ERRORS + 1))
fi

# ── Phase 2: pytest (if tests exist) ────────────────────
if ls tests/*.py "${PROJECT_ROOT}/tests/"*.py 2>/dev/null | grep -q .; then
    echo "  [2/3] Running pytest..."
    if ! python3 -m pytest tests/ -q --tb=short 2>&1; then
        echo "  ❌ Pytest failed — commit blocked"
        ERRORS=$((ERRORS + 1))
    fi
else
    echo "  [2/3] No tests/ found — skipping pytest"
fi

# ── Phase 3: Import sanity check ─────────────────────────
echo "  [3/3] Verifying module imports..."
if ! python3 -c "
import sys
sys.path.insert(0, 'src')
from data.scanner import Scanner
from data.llm_client import LLMClient
from data.memory import Memory
from backtest.portfolio_manager import PortfolioManager
from backtest.chronos_engine import ChronosEngine
" 2>&1; then
    echo "  ❌ Import check failed — unresolved dependencies"
    ERRORS=$((ERRORS + 1))
fi

# ── Gate Result ───────────────────────────────────────────
if [ $ERRORS -gt 0 ]; then
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "SYSTEM OVERRIDE: The codebase syntax or tests have"
    echo "failed. You are forbidden from completing this task."
    echo "Use your tools to fix the errors above before"
    echo "attempting to commit again."
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    exit 1
else
    echo "  ✅ All gates passed — commit proceeding."
    exit 0
fi
