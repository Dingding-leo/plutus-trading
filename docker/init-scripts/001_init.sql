-- Plutus V4.0 — TimescaleDB schema initialisation
-- Runs automatically when the timescaledb container first starts.

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- ── Core market-data hypertables ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ohlcv_1m (
    symbol      TEXT    NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL,
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (symbol, timestamp)
);

SELECT create_hypertable('ohlcv_1m', 'timestamp',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE TABLE IF NOT EXISTS ohlcv_1h (
    symbol      TEXT    NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL,
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (symbol, timestamp)
);

SELECT create_hypertable('ohlcv_1h', 'timestamp',
    chunk_time_interval => INTERVAL '1 week',
    if_not_exists => TRUE
);

-- ── Trade ledger ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS trades (
    id          BIGSERIAL PRIMARY KEY,
    symbol      TEXT    NOT NULL,
    direction   TEXT    NOT NULL,  -- 'LONG' | 'SHORT'
    entry_price DOUBLE PRECISION NOT NULL,
    exit_price  DOUBLE PRECISION,
    size        DOUBLE PRECISION NOT NULL,
    leverage    DOUBLE PRECISION,
    entry_time  TIMESTAMPTZ NOT NULL,
    exit_time   TIMESTAMPTZ,
    pnl         DOUBLE PRECISION DEFAULT 0,
    result      TEXT    DEFAULT 'OPEN',  -- 'OPEN' | 'WIN' | 'LOSS' | 'HOLD'
    notes       JSONB
);

SELECT create_hypertable('trades', 'entry_time',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE
);

-- ── Scanner events ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS scanner_events (
    id          BIGSERIAL PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL,
    symbol      TEXT    NOT NULL,
    anomaly_type TEXT   NOT NULL,
    severity    INTEGER NOT NULL,  -- 1-3
    metadata    JSONB
);

SELECT create_hypertable('scanner_events', 'timestamp',
    chunk_time_interval => INTERVAL '1 week',
    if_not_exists => TRUE
);

-- ── Portfolio snapshots ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL,
    equity      DOUBLE PRECISION NOT NULL,
    open_pnl    DOUBLE PRECISION DEFAULT 0,
    positions   JSONB,
    risk_level  TEXT
);

SELECT create_hypertable('portfolio_snapshots', 'timestamp',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- ── Indexes ───────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_trades_symbol     ON trades (symbol);
CREATE INDEX IF NOT EXISTS idx_trades_result     ON trades (result);
CREATE INDEX IF NOT EXISTS idx_events_symbol     ON scanner_events (symbol);
CREATE INDEX IF NOT EXISTS idx_events_anomaly    ON scanner_events (anomaly_type);

COMMENT ON TABLE ohlcv_1m        IS '1-minute OHLCV bars — TimescaleDB hypertable';
COMMENT ON TABLE ohlcv_1h        IS '1-hour OHLCV bars — TimescaleDB hypertable';
COMMENT ON TABLE trades           IS 'Trade ledger — open and closed positions';
COMMENT ON TABLE scanner_events   IS 'Scanner anomaly events';
COMMENT ON TABLE portfolio_snapshots IS 'Portfolio equity/position snapshots';
