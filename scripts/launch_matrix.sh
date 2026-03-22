#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# launch_matrix.sh — Plutus V4.0 Infrastructure Bootstrapper
#
# Usage:
#   ./scripts/launch_matrix.sh start   Start all services and run health checks
#   ./scripts/launch_matrix.sh stop    Stop and remove all containers
#   ./scripts/launch_matrix.sh status Show running container status
#   ./scripts/launch_matrix.sh logs   Tail logs from all services
#
# Requirements:
#   - docker        (CLI tool)
#   - docker compose plugin  (or docker-compose standalone)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

COMPOSE_FILE="docker/docker-compose.yml"
COMPOSE_PROJECT="plutus"

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m' # No Colour

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERR]${NC}   $*" >&2; }

# ── Prerequisites ─────────────────────────────────────────────────────────────
check_prerequisites() {
    info "Checking prerequisites..."

    if ! command -v docker &>/dev/null; then
        error "docker is not installed or not in PATH. Aborting."
        exit 1
    fi

    # Support both "docker compose" (v2) and "docker-compose" (v1)
    if docker compose version &>/dev/null; then
        COMPOSE_CMD=(docker compose)
    elif command -v docker-compose &>/dev/null; then
        COMPOSE_CMD=(docker-compose)
    else
        error "docker compose plugin (or docker-compose v1) not found. Aborting."
        exit 1
    fi

    info "Using compose command: ${COMPOSE_CMD[*]}"
    success "Prerequisites OK"
}

# ── Docker Compose helper ─────────────────────────────────────────────────────
compose() {
    "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" -p "${COMPOSE_PROJECT}" "$@"
}

# ── Start ─────────────────────────────────────────────────────────────────────
do_start() {
    check_prerequisites

    info "Pulling latest images..."
    compose pull --quiet || warn "Some images may already be up-to-date"

    info "Building and starting all services (--build forced)..."
    compose up -d --build

    info "Waiting for TimescaleDB to become healthy..."
    local attempt=0
    local max_attempts=30
    until compose exec -T timescaledb pg_isready -U plutus &>/dev/null; do
        attempt=$((attempt + 1))
        if (( attempt > max_attempts )); then
            error "TimescaleDB did not become healthy after ${max_attempts} attempts."
            error "Check logs: docker compose -f ${COMPOSE_FILE} logs timescaledb"
            exit 1
        fi
        sleep 2
        printf "."
    done
    echo ""
    success "TimescaleDB is healthy"

    info "Waiting for Redis to respond..."
    attempt=0
    until compose exec -T redis redis-cli ping 2>/dev/null | grep -q "PONG"; do
        attempt=$((attempt + 1))
        if (( attempt > max_attempts )); then
            error "Redis did not respond after ${max_attempts} attempts."
            exit 1
        fi
        sleep 1
        printf "."
    done
    echo ""
    success "Redis is responding"

    info "Initialising TimescaleDB schema..."
    # Run init DDL inside the TimescaleDB container
    compose exec -T timescaledb psql -U plutus -d plutus <<-'EOSQL'
        -- Enable TimescaleDB extension
        CREATE EXTENSION IF NOT EXISTS timescaledb;

        -- OHLCV 1-minute aggregated candle table
        CREATE TABLE IF NOT EXISTS ohlcv_1m (
            time        TIMESTAMPTZ NOT NULL,
            symbol      TEXT NOT NULL,
            open        NUMERIC(18, 8) NOT NULL,
            high        NUMERIC(18, 8) NOT NULL,
            low         NUMERIC(18, 8) NOT NULL,
            close       NUMERIC(18, 8) NOT NULL,
            volume      NUMERIC(18, 8) NOT NULL,
            quote_volume NUMERIC(18, 8) NOT NULL,
            num_trades  BIGINT NOT NULL,
            PRIMARY KEY (time, symbol)
        );

        -- Convert to hypertable (partitioned by time)
        SELECT create_hypertable('ohlcv_1m', 'time',
            chunk_time_interval => INTERVAL '1 day',
            if_not_exists => TRUE
        );

        -- Order book imbalance log
        CREATE TABLE IF NOT EXISTS orderbook_imbalance (
            id          BIGSERIAL PRIMARY KEY,
            time        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            symbol      TEXT NOT NULL,
            bid_volume  NUMERIC(18, 4) NOT NULL,
            ask_volume  NUMERIC(18, 4) NOT NULL,
            imbalance   NUMERIC(10, 6) NOT NULL,
            spread      NUMERIC(18, 8) NOT NULL
        );

        SELECT create_hypertable('orderbook_imbalance', 'time',
            chunk_time_interval => INTERVAL '1 hour',
            if_not_exists => TRUE
        );

        -- Trade fills
        CREATE TABLE IF NOT EXISTS fills (
            id              BIGSERIAL PRIMARY KEY,
            time            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            order_id        TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            side            TEXT NOT NULL,
            price           NUMERIC(18, 8) NOT NULL,
            quantity        NUMERIC(18, 8) NOT NULL,
            quote_quantity  NUMERIC(18, 8) NOT NULL,
            commission      NUMERIC(18, 8) NOT NULL,
            commission_asset TEXT NOT NULL,
            is_maker        BOOLEAN NOT NULL DEFAULT FALSE
        );

        SELECT create_hypertable('fills', 'time',
            chunk_time_interval => INTERVAL '1 day',
            if_not_exists => TRUE
        );

        -- Portfolio snapshots
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id          BIGSERIAL PRIMARY KEY,
            time        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            total_equity_usd  NUMERIC(18, 4) NOT NULL,
            position_count   INTEGER NOT NULL,
            open_pnl_usd     NUMERIC(18, 4) NOT NULL,
            unrealised_pnl   NUMERIC(18, 4) NOT NULL,
            leverage_avg    NUMERIC(6, 3) NOT NULL
        );

        SELECT create_hypertable('portfolio_snapshots', 'time',
            chunk_time_interval => INTERVAL '1 hour',
            if_not_exists => TRUE
        );

        -- Scanner anomaly events
        CREATE TABLE IF NOT EXISTS scanner_events (
            id          BIGSERIAL PRIMARY KEY,
            time        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            symbol      TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            severity    TEXT NOT NULL,
            payload     JSONB NOT NULL
        );

        SELECT create_hypertable('scanner_events', 'time',
            chunk_time_interval => INTERVAL '1 hour',
            if_not_exists => TRUE
        );
EOSQL
    success "TimescaleDB schema initialised"

    # Create Redis stream groups and pub/sub channel health checks
    info "Verifying Redis pub/sub channels..."
    compose exec -T redis redis-cli <<-'EOREDIS'
        SADD plutus:channels scanner.events orders.pending orders.filled portfolio.updates risk.alerts
        HEALTHZ
EOREDIS
    success "Redis channels configured"

    print_status
}

# ── Stop ──────────────────────────────────────────────────────────────────────
do_stop() {
    check_prerequisites
    info "Stopping and removing all Plutus containers..."
    compose down --remove-orphans
    success "All containers stopped"
}

# ── Status ────────────────────────────────────────────────────────────────────
print_status() {
    check_prerequisites
    echo ""
    echo -e "${BOLD}══════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}  PLUTUS V4.0 — SERVICE STATUS${NC}"
    echo -e "${BOLD}══════════════════════════════════════════════════════${NC}"
    echo ""

    local all_ok=true

    # Header
    printf "  %-20s %-15s %-12s %s\n" "SERVICE" "STATUS" "HEALTH" "PORTS"
    echo "  $(printf '%.0s─' {1..70})"

    local services=("timescaledb" "redis" "plutus_engine" "execution_node")
    for svc in "${services[@]}"; do
        local raw_status
        raw_status=$(compose ps --format json "$svc" 2>/dev/null | \
            python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Health','?'),d.get('State','?'))" 2>/dev/null || echo "? ?")
        local health state
        health=$(echo "$raw_status" | awk '{print $1}')
        state=$(echo "$raw_status" | awk '{print $2}')

        local health_display
        case "$health" in
            healthy) health_display="${GREEN}$health${NC}" ;;
            starting) health_display="${YELLOW}$health${NC}" ;;
            unhealthy|"") health_display="${RED}${health:-unhealthy}${NC}" ;;
            *) health_display="$health" ;;
        esac

        local state_display
        case "$state" in
            running) state_display="${GREEN}$state${NC}" ;;
            exited|dead) state_display="${RED}$state${NC}" ;;
            *) state_display="$state" ;;
        esac

        local port
        case "$svc" in
            timescaledb) port="5432" ;;
            redis) port="6379" ;;
            plutus_engine) port="8000" ;;
            execution_node) port="(internal)" ;;
        esac

        printf "  %-20s %-15s %-12s %s\n" "$svc" "$state_display" "$health_display" "$port"

        if [[ "$state" != "running" ]]; then
            all_ok=false
        fi
    done

    echo ""
    if $all_ok; then
        success "All services are running"
    else
        warn "Some services are not running — run './scripts/launch_matrix.sh logs' for details"
    fi
    echo ""
}

# ── Logs ──────────────────────────────────────────────────────────────────────
do_logs() {
    check_prerequisites
    compose logs -f --tail=100
}

# ── Main ───────────────────────────────────────────────────────────────────────
main() {
    local command="${1:-}"

    case "$command" in
        start)
            do_start
            ;;
        stop)
            do_stop
            ;;
        status)
            print_status
            ;;
        logs)
            do_logs
            ;;
        restart)
            do_stop
            sleep 2
            do_start
            ;;
        *)
            echo "Usage: $0 {start|stop|status|logs|restart}"
            echo ""
            echo "  start   — Build, start, and health-check all services"
            echo "  stop    — Stop and remove all containers"
            echo "  status  — Show running service status"
            echo "  logs    — Tail logs from all services"
            echo "  restart — Stop, then start"
            exit 1
            ;;
    esac
}

# ── Trap SIGINT / SIGTERM for graceful stop ───────────────────────────────────
cleanup() {
    echo ""
    warn "Caught interrupt signal — stopping containers..."
    do_stop
    exit 0
}
trap cleanup SIGINT SIGTERM

main "$@"
