#!/usr/bin/env bash
# ============================================================
# PLUTUS SANDBOX SPAWNER — Isolated Worker Container
# Usage: ./scripts/spawn_sandbox.sh [--build] [--kill]
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DOCKERFILE="$PROJECT_ROOT/docker/Dockerfile.quant_worker"
IMAGE_NAME="plutus/quant_worker"
CONTAINER_NAME="plutus_quant_worker"
SESSION_NAME="plutus_worker"

# ── Helpers ───────────────────────────────────────────────────
info()  { echo "ℹ️  $1"; }
warn()  { echo "⚠️  $1"; }
die()   { echo "❌ $1" >&2; exit 1; }

# ── Flags ────────────────────────────────────────────────────
ACTION="spawn"
while [[ $# -gt 0 ]]; do
    case $1 in
        --build)  ACTION="build"  ;;
        --kill)   ACTION="kill"   ;;
        --help)   echo "Usage: $0 [--build|--kill]"
                  echo "  --build  Rebuild Docker image before spawning"
                  echo "  --kill   Stop and remove running container"
                  exit 0 ;;
        *)        die "Unknown flag: $1" ;;
    esac
    shift
done

# ── Kill existing ────────────────────────────────────────────
kill_sandbox() {
    info "Stopping container: $CONTAINER_NAME"
    docker stop "$CONTAINER_NAME" 2>/dev/null || true
    docker rm   "$CONTAINER_NAME" 2>/dev/null || true
    info "Container removed."
}

# ── Build image ───────────────────────────────────────────────
build_image() {
    info "Building Docker image: $IMAGE_NAME"
    docker build \
        --platform linux/amd64 \
        -t "$IMAGE_NAME" \
        -f "$DOCKERFILE" \
        "$PROJECT_ROOT/docker"
    info "Image built successfully."
}

# ── Spawn container ───────────────────────────────────────────
spawn_sandbox() {
    # Stop any existing container first
    kill_sandbox

    info "Spawning isolated worker: $CONTAINER_NAME"
    docker run \
        --platform linux/amd64 \
        --detach \
        --name "$CONTAINER_NAME" \
        --restart unless-stopped \
        -v "$PROJECT_ROOT:/app" \
        "$IMAGE_NAME" \
        2>/dev/null || die "Docker not available or container failed to start."

    # Wait for tmux session to be ready
    sleep 2
    TMUX_PID=$(docker exec "$CONTAINER_NAME" bash -c 'tmux list-sessions -F "#{session_name}" 2>/dev/null | grep -q plutus_worker && echo ok' || echo "")
    if [[ "$TMUX_PID" == "ok" ]]; then
        info "✅ Container running. TMUX session '$SESSION_NAME' active."
        echo ""
        echo "  Attach to worker:"
        echo "    docker exec -it $CONTAINER_NAME tmux attach -t $SESSION_NAME"
        echo ""
        echo "  Send command to worker:"
        echo "    docker exec $CONTAINER_NAME tmux send-keys -t $SESSION_NAME 'python3 /app/run_chronos.py' Enter"
        echo ""
        echo "  View worker output:"
        echo "    docker exec $CONTAINER_NAME tmux capture-pane -t $SESSION_NAME -p"
        echo ""
    else
        die "TMUX session failed to start inside container."
    fi
}

# ── Main ──────────────────────────────────────────────────────
case $ACTION in
    build)
        build_image
        spawn_sandbox
        ;;
    kill)
        kill_sandbox
        ;;
    spawn)
        spawn_sandbox
        ;;
esac
