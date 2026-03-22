#!/usr/bin/env bash
# ============================================================
# PLUTUS SWARM BACKTESTER — Headless Fan-Out
# Launches parallel headless Claude instances per symbol.
# Results are appended to swarm_results.txt.
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RESULTS_FILE="$PROJECT_ROOT/swarm_results.txt"
CLAUDE_CMD="${CLAUDE_CODE_CMD:-claude}"

# Symbols to fan out
SYMBOLS=(BTCUSDT ETHUSDT SOLUSDT)

# Prompt template (escaped for shell heredoc)
build_prompt() {
    local sym="$1"
    cat <<PROMPT
Run the chronos backtester for $sym using --v3-mode live.
Execute: python3 /app/run_chronos.py --symbol $sym --v3-mode live
Extract the final Net PnL and Max Drawdown from the output.
Append a single result line to /app/swarm_results.txt in this exact format:
[$sym] NetPnL: \$XXXX.XX | MaxDD: X.XX% | Sharpe: X.XX | WinRate: XX.X%
Exit immediately after appending.
PROMPT
}

# ── Init ──────────────────────────────────────────────────────
info()  { echo "ℹ️  $1"; }
warn()  { echo "⚠️  $1"; }

# ── Main ──────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════"
echo "  PLUTUS SWARM — Headless Fan-Out Backtest"
echo "══════════════════════════════════════════════"
echo ""

# Clear previous results
> "$RESULTS_FILE"
info "Results file cleared: $RESULTS_FILE"

# Track background PIDs
PIDS=()

# Launch one headless Claude instance per symbol
for sym in "${SYMBOLS[@]}"; do
    info "Launching worker for $sym..."

    PROMPT="$(build_prompt "$sym")"

    # Launch in background — each worker is independent
    $CLAUDE_CMD -p "$PROMPT" \
        --output-format stream-text \
        --allowedTools bash,read_file,write_file \
        > "/tmp/swarm_${sym}.log" 2>&1 \
        &
    PIDS+=($!)
    info "  PID $! spawned for $sym"
done

echo ""
info "All ${#SYMBOLS[@]} workers launched. Fan-out complete."
echo ""

# ── Wait & aggregate ──────────────────────────────────────────
info "Waiting for all workers to complete..."
FAILED=0
for i in "${!SYMBOLS[@]}"; do
    sym="${SYMBOLS[$i]}"
    pid="${PIDS[$i]}"
    if wait "$pid"; then
        info "  ✅ $sym completed (PID $pid)"
    else
        warn "  ❌ $sym failed (PID $pid) — see /tmp/swarm_${sym}.log"
        FAILED=$((FAILED + 1))
    fi
done

echo ""
echo "══════════════════════════════════════════════"
echo "  SWARM RESULTS — $RESULTS_FILE"
echo "══════════════════════════════════════════════"
if [[ -s "$RESULTS_FILE" ]]; then
    cat "$RESULTS_FILE"
else
    warn "Results file empty — workers may have failed."
fi
echo ""

if [[ $FAILED -gt 0 ]]; then
    warn "$FAILED worker(s) failed. Check /tmp/swarm_*.log for details."
else
    info "All workers completed successfully."
fi

exit $FAILED
