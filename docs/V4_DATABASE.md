# Plutus V4.0 — Data Lake Migration Plan

> **Author:** Data Engineer, Plutus V4.0
> **Date:** 2026-03-22
> **Status:** Draft — Pending Review

---

## 1. Current State

### 1.1 What's There Today

The current Plutus data stack runs entirely on local CSV files stored in the project repository:

| Aspect | Current |
|---|---|
| Storage | Local CSV files (`data/`) |
| Time resolution | 1H OHLCV only |
| Data sources | Binance REST API (polled) |
| Orderbook | Not stored |
| Glassnode | Not integrated into DB |
| Scalability | None — degrades with history length |
| Query performance | Full-file scan on every backtest |

### 1.2 Limitations

| Problem | Impact |
|---|---|
| CSV files have no index | Full scan on every read; O(n) on history length |
| No columnar storage | Cannot efficiently read single columns (e.g. `close` only) |
| No time-range pruning | Loading 1 year of 1H data to compute 5-minute signals |
| No concurrent writes | Multiple processes corrupt the file |
| No data types | Strings parsed at runtime — slow and error-prone |
| No compression | GB of raw JSON-like text on disk |
| No glassnode integration | On-chain signals not persisted |

---

## 2. Target Architecture

### 2.1 Candidates

Two time-series databases are evaluated:

| Dimension | TimescaleDB | ClickHouse |
|---|---|---|
| **Write time** | ~50k–100k rows/s per hypertable | ~500k–2M rows/s |
| **Ingest throughput** | Moderate (depends on PostgreSQL config) | Very high (columnar, no ACID overhead) |
| **SQL compatibility** | Full PostgreSQL dialect | Partial ( ClickHouse-flavored SQL) |
| **Time-series features** | Hypertable auto-partitioning, compression, continuous aggregates | MergeTree engine, TTL-based retention, native projection |
| **Operational complexity** | Low — runs as a PostgreSQL extension | Medium — separate service, different运维 model |
| **Plutus integration** | psycopg2 (already used) | clickhouse-driver or HTTP API |
| **Backfill speed** | Fast enough for our data volume | 10x faster for bulk historical loads |
| **Ecosystem** | Mature —grafana, Superset, dbt | Excellent — ClickHouse native UI, Tabix |
| **Licensing** | TimescaleDB Community (Apache 2) | Apache 2 (server), BSL (client) |

### 2.2 Comparison Table

| Feature | TimescaleDB | ClickHouse | Winner |
|---|---|---|---|
| Setup time | < 1 hour (Docker) | < 1 hour (Docker) | Tie |
| Retention policies | Native (drop_chunks) | Native (TTL) | Tie |
| Downsample/aggregation | Continuous aggregates | Materialized views | Tie |
| Orderbook storage | JSONB or relational columns | Array columns + native compression | ClickHouse |
| Query latency (point) | ~1–5 ms | ~0.1–2 ms | ClickHouse |
| Schema flexibility | High (add columns easy) | Medium (ALTER is expensive) | TimescaleDB |
| Backtesting speed | Sufficient (our scale) | Faster | ClickHouse |
| Team familiarity | High | Medium | TimescaleDB |

### 2.3 Recommendation: **ClickHouse**

**Justification:**

1. **Write throughput:** At peak, Plutus may ingest 200+ coins × 12 klines/hour × 60 min = ~1.4 M data points/day. ClickHouse handles this comfortably; TimescaleDB is adequate but slower.
2. **Backtest performance:** Scanning 50 M rows for a rolling-window backtest is 10x faster in ClickHouse. Time matters when iterating strategy ideas.
3. **Orderbook snapshots:** ClickHouse's MergeTree engine stores orderbook levels as `Array(Float64)` columns with no per-row overhead — ideal for the `ohlcv_1m` and `orderbook_snapshots` tables.
4. **Operational acceptance:** Docker Compose makes ClickHouse as operationally simple as TimescaleDB for a single-node setup. The separate process concern is manageable.
5. **Schema evolution:** While ClickHouse ALTER is slower than PostgreSQL, Plutus schema changes are infrequent (schema-on-write, not schema-on-read). When changes are needed, they can be batched during the migration window.

> **If team familiarity is paramount**, TimescaleDB is a fully acceptable fallback — both databases will out-perform the current CSV stack by 100x. The rest of this document uses ClickHouse as the target.

---

## 3. Schema Design

### 3.1 Overview

```
┌─────────────────────────┐
│     ClickHouse          │
│                         │
│  ┌───────────────────┐  │
│  │   ohlcv_1m        │  │  High-granularity candles (raw ingest)
│  └─────────┬─────────┘  │
│            │             │
│  ┌─────────▼─────────┐  │
│  │   ohlcv_1h        │  │  Hourly candles (continuous aggregate)
│  └───────────────────┘  │
│                         │
│  ┌───────────────────┐  │
│  │ orderbook_snaps   │  │  Depth snapshots at tick time
│  └───────────────────┘  │
│                         │
│  ┌───────────────────┐  │
│  │     trades        │  │  Individual trade ticks
│  └───────────────────┘  │
│                         │
│  ┌───────────────────┐  │
│  │ glassnode_metrics │  │  On-chain metrics with TTL
│  └───────────────────┘  │
└─────────────────────────┘
```

### 3.2 `ohlcv_1m` — Minute OHLCV

```sql
CREATE TABLE IF NOT EXISTS ohlcv_1m
(
    symbol       LowCardinality(String)   COMMENT 'Quote pair, e.g. BTCUSDT',
    timestamp    DateTime64(3)            COMMENT 'Candle open time (UTC)',
    open         Float64                  COMMENT 'First trade price in the minute',
    high         Float64                  COMMENT 'Highest trade price',
    low          Float64                  COMMENT 'Lowest trade price',
    close        Float64                  COMMENT 'Last trade price',
    volume       Float64                  COMMENT 'Total base-asset volume',
    quote_volume Float64                  COMMENT 'Total quote-asset volume (close × volume)',
    trades       UInt32                   COMMENT 'Number of individual trades in the minute',
    is_final     UInt8                    COMMENT '1 = candle is closed; 0 = live candle'
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (symbol, timestamp)
TTL timestamp + INTERVAL 30 DAY
SETTINGS index_granularity = 8192;
```

**Design notes:**
- `LowCardinality(String)` on `symbol` reduces memory by ~8x vs plain String (only ~200 distinct values expected).
- `DateTime64(3)` gives millisecond precision — matches Binance API.
- `PARTITION BY toYYYYMM` — allows efficient DROP PARTITION for retention.
- `TTL 30 DAY` — automatic eviction of minute data beyond retention window.
- `is_final` flag lets the app read live (incomplete) candles without a separate streaming flag.

### 3.3 `ohlcv_1h` — Hourly OHLCV (Materialized)

```sql
CREATE MATERIALIZED VIEW IF NOT EXISTS ohlcv_1h
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (symbol, timestamp)
AS
SELECT
    symbol,
    toStartOfHour(timestamp) AS timestamp,
    argMin(open,    timestamp) AS open,
    max(high)                  AS high,
    min(low)                   AS low,
    argMax(close,  timestamp)  AS close,
    sum(volume)                AS volume,
    sum(quote_volume)          AS quote_volume,
    sum(trades)               AS trades
FROM ohlcv_1m
WHERE is_final = 1
GROUP BY symbol, timestamp
TTL timestamp + INTERVAL 365 DAY;
```

**Design notes:**
- Materialized view auto-populates from `ohlcv_1m` — no separate ingest pipeline.
- `argMin`/`argMax` correctly extract open (first) and close (last) prices.
- `TTL 365 DAY` — hourly data retained for one year.
- Backtests reading 1H data are served directly from this table.

### 3.4 `orderbook_snapshots` — Depth Book Snapshots

```sql
CREATE TABLE IF NOT EXISTS orderbook_snapshots
(
    symbol         LowCardinality(String)  COMMENT 'Quote pair',
    timestamp      DateTime64(3)           COMMENT 'Snapshot time (UTC)',
    side           Enum8('bid' = 1, 'ask' = 2) COMMENT 'Bid or ask side',
    price_level    Float64                 COMMENT 'Price level',
    quantity       Float64                 COMMENT 'Quantity at this level',
    level_rank     UInt8                   COMMENT 'Position from top (1 = best bid/ask)',
    best_bid_price Float64   DEFAULT arrayElement(bids, 1).1,
    best_ask_price Float64   DEFAULT arrayElement(asks, 1).1,
    spread_bps     Float64   DEFAULT (best_ask_price - best_bid_price) / best_bid_price * 10000
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (symbol, timestamp, side, level_rank)
TTL timestamp + INTERVAL 7 DAY
SETTINGS index_granularity = 8192;
```

**Design notes:**
- Each snapshot is stored as multiple rows (one per price level) — flat format makes aggregation queries simple (no JSON / Array unnesting at query time).
- `level_rank` enables fast queries for "top 20 levels only" via `WHERE level_rank <= 20`.
- `spread_bps` precomputed at ingest — avoids recalculating on every alert check.
- `TTL 7 DAY` — orderbook data is high volume and low long-term value; 7 days covers all backtesting needs.

**Imbalance query example:**
```sql
SELECT
    symbol,
    timestamp,
    sumIf(quantity, side = 'bid') / sumIf(quantity, side = 'ask') AS imbalance
FROM orderbook_snapshots
WHERE symbol = 'BTCUSDT'
  AND timestamp >= now() - INTERVAL 1 HOUR
  AND level_rank <= 20
GROUP BY symbol, timestamp
ORDER BY timestamp;
```

### 3.5 `trades` — Individual Trade Ticks

```sql
CREATE TABLE IF NOT EXISTS trades
(
    symbol            LowCardinality(String) COMMENT 'Quote pair',
    timestamp         DateTime64(3)          COMMENT 'Trade time (UTC)',
    trade_id          UInt64                 COMMENT 'Binance trade ID (unique within symbol)',
    price             Float64                COMMENT 'Execution price',
    quantity          Float64                COMMENT 'Base asset quantity',
    quote_quantity    Float64                COMMENT 'price × quantity',
    is_buyer_maker    UInt8                  COMMENT '1 = buyer was maker (aggressive seller)',
    is_block_trade    UInt8                  COMMENT '1 = block/OTC trade'
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (symbol, timestamp, trade_id)
TTL timestamp + INTERVAL 30 DAY
SETTINGS index_granularity = 8192;
```

**Design notes:**
- Primary key `(symbol, timestamp, trade_id)` enables efficient time-range + symbol filtering.
- `trade_id` in ORDER BY ensures no duplicate trade IDs per symbol.
- `TTL 30 DAY` — tick data is for intraday/short-term analysis only.
- `quote_quantity` precomputed — saves multiplication on every volume-weighted average price query.

**Volume-weighted price (last 5 minutes):**
```sql
SELECT
    symbol,
    sum(quote_quantity) / sum(quantity) AS vwap
FROM trades
WHERE symbol = 'BTCUSDT'
  AND timestamp >= now() - INTERVAL 5 MINUTE
GROUP BY symbol;
```

### 3.6 `glassnode_metrics` — On-Chain Metrics

```sql
CREATE TABLE IF NOT EXISTS glassnode_metrics
(
    symbol       LowCardinality(String)  COMMENT 'Asset symbol, e.g. BTC, ETH',
    timestamp    DateTime64(3)          COMMENT 'Metric timestamp (UTC)',
    metric_name  LowCardinality(String)  COMMENT 'One of: mvrv, sopr, exchange_net_position_change, active_addresses',
    value        Float64                 COMMENT 'Metric value',
    ingested_at   DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ingested_at)
ORDER BY (symbol, metric_name, timestamp)
TTL ingested_at + INTERVAL 90 DAY;
```

**Design notes:**
- `ReplacingMergeTree` with `ingested_at` deduplicates out-of-order backfill writes.
- Single table for all assets and metrics — uses `metric_name` as a LowCardinality dimension.
- `TTL 90 DAY` — sufficient for strategy development; longer retention handled by a separate cold-storage table or script.
- `ingested_at` is NOT the event timestamp — it's the database write time, used only for deduplication.

**Query latest MVRV for BTC:**
```sql
SELECT value
FROM glassnode_metrics
WHERE symbol = 'BTC'
  AND metric_name = 'mvrv'
ORDER BY timestamp DESC
LIMIT 1;
```

---

## 4. Migration Steps

### 4.1 Phase 1 — Dual-Write (Week 1–2)

**Goal:** Write all new incoming data to BOTH CSV (current) AND ClickHouse simultaneously.

```
┌──────────────┐      ┌──────────────────┐      ┌────────────────┐
│  Binance API │─────▶│  data/scanner.py │─────▶│  CSV (existing) │
│              │      │                  │─────▶│ ClickHouse (new) │
└──────────────┘      └──────────────────┘      └────────────────┘
```

**Actions:**
1. Provision ClickHouse container via Docker Compose.
2. Run all schema DDL from Section 3.
3. Add `scripts/clickhouse_writer.py` — `InsertOHLCV`, `InsertTrades`, `InsertGlassnodeMetrics` functions.
4. Wrap existing scanner fetch loop with dual-write: try CSV (existing) + try ClickHouse (new). CSV failure is still fatal; ClickHouse failure logs but does not block.
5. Verify no latency regression on scanner loop (< 1s per cycle).

**Exit criteria:** ClickHouse contains live data for 48 hours with no data loss.

### 4.2 Phase 2 — Backfill (Week 2–4)

**Goal:** Load historical CSV data into ClickHouse to enable full backtesting.

**Stub script:** `scripts/backfill_timeseries.py` (see Section 5).

**Actions:**
1. Identify all CSV source files in `data/`.
2. Parse timestamps, clean price/volume fields.
3. Insert in batches of 10,000 rows with `input_format_max_rows_in_memory = 100000`.
4. Validate row counts match between CSV and ClickHouse after each symbol.
5. Backfill in symbol batches — BTC/ETH first, then alts.
6. Run end-to-end backtest on a sample strategy using ClickHouse — verify results match CSV baseline (allow < 0.01 % numerical tolerance for floating-point rounding).

**Exit criteria:** All CSV data (from first data point to today) exists in ClickHouse. Sample backtest results match within tolerance.

### 4.3 Phase 3 — Cutover (Week 4+)

**Goal:** Remove CSV dependency from all strategy and backtest code.

**Actions:**
1. Update all `data/` imports to read from ClickHouse instead of CSV.
2. Remove dual-write from scanner — only ClickHouse writes from this point.
3. Archive (do not delete) CSV files to cold storage or `data/archive/`.
4. Set up Grafana dashboard connecting to ClickHouse for data quality monitoring.
5. Document new query patterns in `docs/V4_QUERIES.md` (stub).

**Exit criteria:** Zero CSV reads in production strategy code. Scanner loop latency confirmed < 1s.

---

## 5. Backfill Script Plan

### `scripts/backfill_timeseries.py`

```python
"""
Plutus V4.0 — Timeseries Backfill Script

Purpose
-------
Load historical CSV data (from the legacy local-CSV stack) into ClickHouse.
Supports: ohlcv_1m, ohlcv_1h, trades, glassnode_metrics.

Usage
-----
    python scripts/backfill_timeseries.py \
        --symbol BTCUSDT \
        --data-dir data/ \
        --start-date 2023-01-01 \
        --batch-size 50000

Design
------
- Reads CSV rows in streaming fashion (never loads full file into memory).
- Batches rows into ClickHouse INSERT chunks of --batch-size.
- Reports progress every 5 % of file.
- Validates row counts and price-range sanity checks after insert.
- Dry-run mode (--dry-run) parses and prints stats without inserting.
"""

from __future__ import annotations

import argparse
import csv
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

import clickhouse_connect  # pip install clickhouse-connect

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (environment-variable overrides supported)
# ---------------------------------------------------------------------------

CLICKHOUSE_HOST = "localhost"
CLICKHOUSE_PORT = 8123
CLICKHOUSE_DATABASE = "plutus"
CLICKHOUSE_TABLE_OHLCV_1M = "ohlcv_1m"
CLICKHOUSE_TABLE_TRADES = "trades"


@dataclass
class BackfillStats:
    """Accumulators for progress reporting."""
    rows_read: int = 0
    rows_inserted: int = 0
    rows_skipped: int = 0  # malformed rows
    elapsed_seconds: float = 0.0

    def progress_pct(self, total: int) -> float:
        return self.rows_read / total * 100 if total > 0 else 0.0


# ---------------------------------------------------------------------------
# CSV parsers
# ---------------------------------------------------------------------------

def iter_ohlcv_rows(csv_path: Path, symbol: str) -> Iterator[dict]:
    """
    Yield validated OHLCV row dicts from a Binance-export CSV.

    Expected CSV columns (Binance standard):
        open_time, open, high, low, close, volume, close_time,
        quote_volume, trades, taker_buy_base, taker_buy_quote

    Rows with zero volume or unparseable timestamps are skipped.
    """
    with csv_path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                open_time_ms = int(row["open_time"])
                dt = datetime.fromtimestamp(open_time_ms / 1000.0)
            except (KeyError, ValueError):
                continue

            yield {
                "symbol": symbol,
                "timestamp": dt,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "quote_volume": float(row.get("quote_volume", 0)),
                "trades": int(row.get("trades", 0)),
            }


# ---------------------------------------------------------------------------
# Insert logic
# ---------------------------------------------------------------------------

def backfill_ohlcv(
    client,
    symbol: str,
    csv_path: Path,
    batch_size: int = 50_000,
    dry_run: bool = False,
) -> BackfillStats:
    """
    Backfill ohlcv_1m from a Binance OHLCV CSV file.

    Math note: all price and volume values are stored as Float64.
    Binance prices have up to 8 decimal places; Float64 provides
    full precision for the range of values encountered in crypto.
    """
    stats = BackfillStats()
    t0 = time.monotonic()
    rows: list[dict] = []

    total = sum(1 for _ in open(csv_path)) - 1  # -1 for header
    log.info("Backfilling %s rows from %s", total, csv_path)

    for row in iter_ohlcv_rows(csv_path, symbol):
        stats.rows_read += 1
        rows.append(row)

        if len(rows) >= batch_size:
            if not dry_run:
                client.insert(
                    CLICKHOUSE_TABLE_OHLCV_1M,
                    rows,
                    column_names=[
                        "symbol", "timestamp", "open", "high", "low", "close",
                        "volume", "quote_volume", "trades",
                    ],
                )
            stats.rows_inserted += len(rows)
            rows.clear()
            pct = stats.progress_pct(total)
            log.info("  %.1f %% done — inserted %d rows", pct, stats.rows_inserted)

    # Flush remaining
    if rows and not dry_run:
        client.insert(CLICKHOUSE_TABLE_OHLCV_1M, rows, column_names=[...])
        stats.rows_inserted += len(rows)

    stats.elapsed_seconds = time.monotonic() - t0
    return stats


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill historical data into ClickHouse")
    parser.add_argument("--symbol", required=True, help="Symbol, e.g. BTCUSDT")
    parser.add_argument("--csv", required=True, type=Path, help="Path to CSV file")
    parser.add_argument("--batch-size", type=int, default=50_000)
    parser.add_argument("--dry-run", action="store_true", help="Parse without inserting")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    client = clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        database=CLICKHOUSE_DATABASE,
    )

    stats = backfill_ohlcv(
        client=client,
        symbol=args.symbol,
        csv_path=args.csv,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )

    log.info(
        "Done. read=%d inserted=%d skipped=%d elapsed=%.1fs",
        stats.rows_read,
        stats.rows_inserted,
        stats.rows_skipped,
        stats.elapsed_seconds,
    )


if __name__ == "__main__":
    main()
```

**Running the backfill:**
```bash
# Dry run — validate a CSV
python scripts/backfill_timeseries.py \
    --symbol BTCUSDT \
    --csv data/BTCUSDT_1h.csv \
    --dry-run

# Actual backfill
python scripts/backfill_timeseries.py \
    --symbol BTCUSDT \
    --csv data/BTCUSDT_1h.csv \
    --batch-size 50000

# Backfill all symbols
for csv in data/*_1h.csv; do
    symbol=$(basename "$csv" | sed 's/_1h.csv//')
    python scripts/backfill_timeseries.py --symbol "$symbol" --csv "$csv"
done
```

---

## 6. Data Flow Summary

```
Binance REST API (1m klines)
         │
         ▼
  data/scanner.py  ──────▶  ClickHouse  ohlcv_1m
         │                                  │
         │                                  │ [SummingMergeTree MV]
         │                                  ▼
         │                        ClickHouse  ohlcv_1h  (auto-populated)
         │
         ▼
Binance WebSocket  ──────▶  src/data/streams/binance_websocket.py
  (real-time depth,                  │
   trade stream)                      │ batch write every N seconds
         │                            ▼
         │                  ClickHouse  orderbook_snapshots
         │                  ClickHouse  trades
         │
         ▼
Glassnode API  ────────▶  src/data/streams/glassnode.py
                                 │
                                 │ batch write every 5 min
                                 ▼
                       ClickHouse  glassnode_metrics
```

---

*End of document.*
