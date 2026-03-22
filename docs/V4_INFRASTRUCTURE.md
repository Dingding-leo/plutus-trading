# Plutus V4.0 — Distributed Microservices Architecture

> **Version:** 4.0.0
> **Date:** 2026-03-22
> **Status:** Active Development

---

## 1. Architecture Overview

Plutus V4.0 is built as a set of loosely-coupled Docker services that communicate over
Redis pub/sub and a shared TimescaleDB time-series database.  Each service owns its
data store and exposes a minimal, well-defined interface.

### 1.1 System Diagram

```
                                    ┌─────────────────────────────────────────────┐
                                    │              plutus_engine                 │
                                    │   (LLM Brain — FastAPI + Uvicorn)         │
                                    │                                             │
                                    │  ┌─────────────┐    ┌──────────────────┐  │
                                    │  │   Scanner   │    │   LLM Reasoning   │  │
                                    │  │   Worker    │───▶│   & Judgment     │  │
                                    │  └──────▲──────┘    └────────▲─────────┘  │
                                    │         │                      │            │
                                    └─────────┼──────────────────────┼────────────┘
                                              │                      │
                                    ┌─────────▼──────────────────────▼────────────┐
                                    │                    Redis                     │
                                    │                                                  │
                                    │  Pub/Sub Channels:                               │
                                    │  • scanner.events   ← Scanner Worker           │
                                    │  • orders.pending   ← PlutusEngine           │
                                    │  • orders.filled    ← Execution Node          │
                                    │  • portfolio.updates← Portfolio Manager      │
                                    │  • risk.alerts      ← Risk Engine            │
                                    │                                                  │
                                    │  Streams:                                       │
                                    │  • orderbook:snapshots ← Binance WS feed      │
                                    └──────────┬───────────────────────────────────┘
                                               │
                     ┌─────────────────────────┼─────────────────────────┐
                     │                         │                         │
          ┌──────────▼──────────┐    ┌─────────▼───────────┐
          │   TimescaleDB       │    │   plutus_engine     │
          │   (OHLCV, Fills,    │    │   (FastAPI HTTP)    │
          │    Portfolio,       │    │   Port 8000         │
          │    Scanner Events)  │    └─────────────────────┘
          └─────────────────────┘

  ┌────────────────────────────────────────────────────────────────────────────┐
  │                           Binance Exchange                                  │
  │                                                                          │
  │  Websocket Stream: wss://stream.binance.com:9443/ws/<symbol>@depth20@100ms │
  │       │                                                                   │
  │       ▼                                                                   │
  │  ┌─────────────────┐                                                     │
  │  │ execution_node  │  (Listens on orders.pending, posts to orders.filled)│
  │  │                 │  Submits market/limit orders via python-binance SDK  │
  │  └────────┬────────┘                                                     │
  │           │                                                               │
  │           ▼                                                               │
  │  REST API / Websocket ← Binance execution reports + fills                │
  └────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 Data Flow Summary

```
1. Binance WebSocket ──▶ Redis Stream (orderbook:snapshots)
2. Scanner Worker ──▶ reads stream ──▶ compute imbalances
3. Scanner Worker ──▶ anomalies ──▶ Redis pub/sub (scanner.events)
4. PlutusEngine ──▶ subscribes to scanner.events ──▶ LLM analysis ──▶ orders.pending
5. Execution Node ──▶ subscribes to orders.pending ──▶ Binance API ──▶ orders.filled
6. Execution Node ──▶ fills ──▶ TimescaleDB (fills table)
7. Portfolio Manager ──▶ portfolio.updates ──▶ TimescaleDB (portfolio_snapshots)
8. PlutusEngine ──▶ queries TimescaleDB for historical context ──▶ richer LLM prompts
```

---

## 2. Service Responsibilities

| Service | Language | Role | Data Stores |
|---|---|---|---|
| **timescaledb** | SQL (PostgreSQL 15 + TimescaleDB) | Persistent time-series storage for OHLCV, fills, portfolio snapshots, scanner events | `ohlcv_1m`, `fills`, `portfolio_snapshots`, `scanner_events`, `orderbook_imbalance` |
| **redis** | C | Pub/sub messaging, orderbook snapshot streams, hot cache | `scanner.events`, `orders.pending`, `orders.filled`, `portfolio.updates`, `risk.alerts` channels; `orderbook:snapshots` stream |
| **plutus_engine** | Python 3.11 | LLM brain; scanner worker; FastAPI HTTP server (port 8000) | Reads/Writes Redis; reads/writes TimescaleDB |
| **execution_node** | Python 3.11 | Consumes `orders.pending`; executes on Binance; publishes `orders.filled` | Reads/Writes Redis; reads/writes TimescaleDB |

---

## 3. Redis Pub/Sub Channels

All channels use JSON-encoded messages.  The payload schema is documented per channel below.

| Channel | Publisher | Consumers | Description |
|---|---|---|---|
| `scanner.events` | `ScannerWorker` | `PlutusEngine` | Orderbook imbalance anomalies and spread-widening alerts |
| `orders.pending` | `PlutusEngine` | `ExecutionNode` | New order signals generated by the LLM brain |
| `orders.filled` | `ExecutionNode` | `PlutusEngine`, `PortfolioManager` | Execution reports (fills) from Binance |
| `portfolio.updates` | `ExecutionNode`, `PortfolioManager` | `PlutusEngine`, `RiskEngine` | Periodic portfolio snapshots and PnL updates |
| `risk.alerts` | `ExecutionNode`, `RiskEngine` | `PlutusEngine` | Margin warnings, liquidation proximity, drawdown limits |

### 3.1 Channel Payload Schemas

#### `scanner.events`
```json
{
  "event_type": "bid_imbalance | ask_imbalance | spread_widening",
  "severity": "low | medium | high | critical",
  "symbol": "BTCUSDT",
  "timestamp_ms": 1742659200000,
  "imbalance": 0.38,
  "spread_pct": 0.0003,
  "metadata": {}
}
```

#### `orders.pending`
```json
{
  "order_id": "uuid-v4",
  "symbol": "BTCUSDT",
  "side": "BUY | SELL",
  "order_type": "MARKET | LIMIT",
  "quantity": 0.01,
  "price": null,
  "stop_price": null,
  "created_at_ms": 1742659200000,
  "signal_source": "llm_brain",
  "metadata": {}
}
```

#### `orders.filled`
```json
{
  "order_id": "uuid-v4",
  "exchange_order_id": "12345678",
  "symbol": "BTCUSDT",
  "side": "BUY",
  "filled_qty": 0.01,
  "filled_price": 67420.50,
  "commission": 0.00001,
  "commission_asset": "BTC",
  "is_maker": false,
  "filled_at_ms": 1742659250000
}
```

---

## 4. TimescaleDB Schema

See [doc/V4_DATABASE.md](./V4_DATABASE.md) for the complete schema reference, including
hypertable configuration, retention policies, and continuous aggregates.

### 4.1 Core Tables

| Table | Type | Partition Key | Description |
|---|---|---|---|
| `ohlcv_1m` | hypertable | `time` (1-day chunks) | 1-minute aggregated OHLCV candles |
| `orderbook_imbalance` | hypertable | `time` (1-hour chunks) | Bid/ask volume imbalance per symbol per scan cycle |
| `fills` | hypertable | `time` (1-day chunks) | All execution fills from the exchange |
| `portfolio_snapshots` | hypertable | `time` (1-hour chunks) | Periodic equity and position snapshots |
| `scanner_events` | hypertable | `time` (1-hour chunks) | All scanner anomaly events for backtesting |

---

## 5. Failure Modes & Recovery

### 5.1 Execution Node Loses Connection to Binance

**Symptom:** `orders.filled` messages stop arriving; `orders.pending` queue in Redis grows.

**Detection:**
- `execution_node` publishes a heartbeat to `risk.alerts` every 30 s.
- Missing two consecutive heartbeats triggers a `plutus_engine` alert.

**Recovery:**
1. `ExecutionNode` implements exponential back-off reconnection to Binance Websocket.
2. Stale `orders.pending` messages older than 5 minutes are re-dispatched by `PlutusEngine`.
3. Binance provides a trade history REST endpoint for gap-filling on reconnect.
4. On reconnect, `ExecutionNode` calls `POST /portfolio/sync` to reconcile open positions.

**User impact:** Short delay in order execution; no data loss.

---

### 5.2 Redis Failure

**Symptom:** All pub/sub channels go silent; `plutus_engine` and `execution_node` both stall.

**Recovery:**
- Redis is configured with `appendonly yes` (AOF) for durability.
- `plutus_engine` implements a local in-memory fallback queue (max 1 000 events).
- On Redis reconnect, the fallback queue is flushed to the recovered Redis.
- `ScannerWorker` stores snapshots in TimescaleDB as a secondary path when Redis is unavailable.

**Liveness check:** `launch_matrix.sh` polls `redis-cli ping` every 2 s and marks the service unhealthy after 5 failures.

---

### 5.3 TimescaleDB Lag / Behind

**Symptom:** Historical queries via `PlutusEngine /query` endpoint return stale data or timeout.

**Recovery:**
1. Each hypertable is configured with a `chunk_time_interval` that prevents over-partitioning.
2. TimescaleDB continuous aggregates pre-compute 1-hour and 1-day OHLCV views to speed up LLM prompts.
3. `plutus_engine` uses an asyncpg connection pool with a 5-second query timeout.
4. If TSDB lag exceeds 30 seconds, `plutus_engine` switches to a read-from-Redis-cache mode for recent data.

---

### 5.4 PlutusEngine Crash / OOM

**Symptom:** Port 8000 becomes unreachable; scanner anomalies stop being processed.

**Recovery:**
- `restart: unless-stopped` in `docker-compose.yml` ensures automatic restart with exponential back-off.
- The ScannerWorker runs inside the `plutus_engine` container; if the container restarts, the worker restarts cleanly.
- Orders in `orders.pending` are idempotent (order IDs are UUIDs); the execution node can safely re-process them.
- A `plutus_engine_health` sentinel key in Redis is set to `1` every 10 s and cleared on shutdown; if the key is missing for > 30 s, a Kubernetes/Compose watcher can trigger a restart.

---

## 6. Deployment

### 6.1 Docker Compose Workflow

```bash
# 1. Bootstrap — builds images, starts all services, runs health checks
./scripts/launch_matrix.sh start

# 2. Verify all services are healthy
./scripts/launch_matrix.sh status

# 3. Tail logs from all services
./scripts/launch_matrix.sh logs

# 4. Graceful shutdown
./scripts/launch_matrix.sh stop
```

### 6.2 Required Environment Variables

Create a `.env` file in the project root (never commit it):

```env
# ── TimescaleDB ──────────────────────────────────────────────────────────────
POSTGRES_PASSWORD=your_secure_password
POSTGRES_USER=plutus
POSTGRES_DB=plutus

# ── LLM Provider ────────────────────────────────────────────────────────────
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1          # or your custom endpoint
LLM_MODEL=gpt-4o

# ── Binance ──────────────────────────────────────────────────────────────────
BINANCE_API_KEY=your_api_key
BINANCE_SECRET=your_secret

# ── Execution Mode ───────────────────────────────────────────────────────────
EXECUTION_MODE=test        # 'test' or 'live'
LOG_LEVEL=INFO
```

### 6.3 Health Checks

| Service | Health Check | Interval | Timeout | Retries |
|---|---|---|---|---|
| `timescaledb` | `pg_isready -U plutus` | 10 s | 5 s | 5 |
| `redis` | `redis-cli ping` | 10 s | 5 s | 5 |
| `plutus_engine` | HTTP `GET /health` | — | — | Container restart on OOM |
| `execution_node` | Redis sentinel key TTL | 30 s | — | Auto-restart via Compose |

### 6.4 Rolling Restarts

Because all services are stateless (state lives in TimescaleDB and Redis):

```bash
# Restart one service at a time without downtime
docker compose -f docker/docker-compose.yml restart plutus_engine
```

The `depends_on: condition: service_healthy` in `docker-compose.yml` guarantees that
downstream services wait for their dependencies to be fully healthy before starting.

### 6.5 Log Aggregation

All services write to stdout; Docker's built-in logging driver captures them.
To ship logs to a central aggregator (e.g., Loki, Elasticsearch):

```yaml
# In docker/docker-compose.override.yml
services:
  plutus_engine:
    logging:
      driver: "json-file"
      options:
        max-size: "50m"
        max-file: "5"
```

---

## 7. Security Considerations

- **No secrets in Dockerfiles** — all secrets are injected via environment variables at runtime.
- **Non-root containers** — both Python images run as `appuser` (UID 1000).
- **Read-only source mounts** — `plutus_engine` and `execution_node` mount `./src` as `:ro`.
- **Network isolation** — all services communicate exclusively over the `plutus_net` bridge network;
  only `timescaledb:5432`, `redis:6379`, and `plutus_engine:8000` are exposed to the host.
- **TimescaleDB authentication** — enforced via `POSTGRES_PASSWORD`; the `plutus` user has
  only the privileges needed for the application's schema.

---

## 8. Scaling Path

| Dimension | Current | V4.1 Target |
|---|---|---|
| Simultaneous trading pairs | 1 | 10–50 |
| Scanner cycle latency | 5 s | < 1 s (async) |
| Execution node instances | 1 | 1 per exchange account |
| TimescaleDB retention | 7 days | 30 days + continuous aggregates |
| LLM inference latency | ~2–5 s | Cached + speculative prefill |

---

*Last updated: 2026-03-22*
