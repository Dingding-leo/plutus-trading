"""
Plutus V3.1 — The Memory Bank
The Archivist: Persistent SQLite RAG store for LLM persona reflexion.

Architecture role:
  The MemoryBank sits at the core of the Reflexion Engine. After every
  losing trade, the responsible LLM persona writes a 1-sentence lesson.
  Before evaluating a new anomaly, all personas query the MemoryBank for
  prior lessons on that anomaly type — those lessons are injected into
  the LLM prompt at inference time (RAG retrieval).

Schema:
  lessons
    id          INTEGER PRIMARY KEY AUTOINCREMENT
    timestamp   TEXT    NOT NULL   -- ISO 8601
    persona     TEXT    NOT NULL   -- e.g. "SMC_ICT", "ORDER_FLOW"
    anomaly_type TEXT  NOT NULL   -- e.g. "LIQUIDITY_SWEEP"
    pnl         REAL    NOT NULL   -- signed % return (negative = loss)
    thesis      TEXT    NOT NULL   -- what the persona believed
    lesson      TEXT    NOT NULL   -- 1-sentence rule the LLM produced

Indexes:
  - idx_persona_anomaly  ON (persona, anomaly_type)  -- fast RAG lookup

Usage:
  bank = MemoryBank()                        # uses ~/.plutus/memory.db
  bank.save_lesson("SMC_ICT", "LIQUIDITY_SWEEP", -2.3,
                   "Bullish FVG with MSS confirmed",
                   "Never enter a long if the prior swing low was tested more than 3 times.")
  lessons = bank.retrieve_lessons("SMC_ICT", "LIQUIDITY_SWEEP")
  # → ["Never enter a long if the prior swing low was tested more than 3 times.", ...]
"""

from __future__ import annotations

import os
import signal
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict


# ─── Path helpers ──────────────────────────────────────────────────────────────

def _default_db_path() -> Path:
    base = Path.home() / ".plutus"
    base.mkdir(exist_ok=True)
    return base / "memory.db"


# ─── MemoryBank ───────────────────────────────────────────────────────────────

class MemoryBank:
    """
    Thread-safe SQLite-backed lesson store.

    All write operations are serialized via an instance-level lock so that
    concurrent backtest threads (e.g. multi-symbol) never corrupt the DB.
    Reads require no lock (SQLite handles concurrent readers).
    """

    def __init__(self, db_path: Optional[Path | str] = None):
        self._db_path: Path = Path(db_path) if db_path else _default_db_path()
        self._lock     = threading.Lock()
        self._init_db()

    # ── Public API ────────────────────────────────────────────────────────────

    def save_lesson(
        self,
        persona: str,
        anomaly_type: str,
        pnl: float,
        thesis: str,
        lesson: str,
        source_mode: str = "DRY_RUN",
    ) -> int:
        """
        Persist a single lesson to the Memory Bank.

        Args:
            persona:      Persona name, e.g. "SMC_ICT"
            anomaly_type: Anomaly that triggered the trade, e.g. "LIQUIDITY_SWEEP"
            pnl:          Signed % return (negative = loss, positive = gain)
            thesis:       The LLM persona's stated thesis when entering
            lesson:       1-sentence rule the LLM produced from reflexion
            source_mode:  "DRY_RUN" or "LIVE". Only LIVE lessons are used for
                          live trading decisions. Default "DRY_RUN".

        Returns:
            The rowid of the newly inserted lesson.
        """
        if not lesson or not lesson.strip():
            return -1  # No-op for empty lessons

        valid_modes = {"DRY_RUN", "LIVE"}
        if source_mode not in valid_modes:
            source_mode = "DRY_RUN"

        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO lessons (timestamp, persona, anomaly_type, pnl, thesis, lesson, source_mode)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.utcnow().isoformat(timespec="seconds"),
                    persona.strip(),
                    anomaly_type.strip(),
                    float(pnl),
                    thesis.strip(),
                    lesson.strip(),
                    source_mode,
                ),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def retrieve_lessons(
        self,
        persona: str,
        anomaly_type: str,
        limit: int = 3,
        source_mode: str = "LIVE",
    ) -> List[str]:
        """
        RAG retrieval: fetch the most recent lessons for a persona + anomaly pair.

        Args:
            persona:      Persona name to filter by
            anomaly_type: Anomaly type to filter by
            limit:        Maximum number of lessons to return (default 3)
            source_mode:  Only return lessons from this source. Set to "LIVE" to
                          exclude dry-run lessons (default "LIVE").

        Returns:
            List of lesson strings, most recent first.
            Empty list if no lessons found.
        """
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT lesson
              FROM lessons
             WHERE persona     = ?
               AND anomaly_type = ?
               AND source_mode  = ?
             ORDER BY id DESC
             LIMIT ?
            """,
            (persona.strip(), anomaly_type.strip(), source_mode, max(1, limit)),
        )
        return [row[0] for row in cur.fetchall()]

    def retrieve_lessons_batch(
        self,
        personas: List[str],
        anomaly_type: str,
        limit_per: int = 3,
        source_mode: str = "LIVE",
    ) -> Dict[str, List[str]]:
        """
        Fetch lessons for multiple personas in a single query.
        Returns dict mapping persona -> list of lesson strings.

        Args:
            personas:     List of persona names to fetch lessons for
            anomaly_type: Anomaly type to filter by
            limit_per:    Maximum lessons per persona (default 3)
            source_mode:  Only return lessons from this source (default "LIVE")
        """
        if not personas:
            return {}
        placeholders = ','.join('?' * len(personas))
        cur = self._conn.cursor()
        cur.execute(
            f"""
            SELECT persona, lesson
              FROM lessons
             WHERE persona IN ({placeholders})
               AND anomaly_type = ?
               AND source_mode  = ?
             ORDER BY id DESC
            """,
            (*[p.strip() for p in personas], anomaly_type.strip(), source_mode),
        )
        results: Dict[str, List[str]] = {p: [] for p in personas}
        for persona, lesson in cur.fetchall():
            p = persona.strip()
            if len(results[p]) < limit_per:
                results[p].append(lesson)
        return results

    def retrieve_all_lessons(
        self,
        persona: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        """
        Debug / UI helper: return full lesson rows as dicts.

        Args:
            persona: If provided, filter by persona name
            limit:   Maximum rows to return (default 20)

        Returns:
            List of dicts with keys: id, timestamp, persona, anomaly_type,
            pnl, thesis, lesson
        """
        cur = self._conn.cursor()
        if persona:
            cur.execute(
                """
                SELECT id, timestamp, persona, anomaly_type, pnl, thesis, lesson
                  FROM lessons
                 WHERE persona = ?
                 ORDER BY id DESC
                 LIMIT ?
                """,
                (persona.strip(), max(1, limit)),
            )
        else:
            cur.execute(
                """
                SELECT id, timestamp, persona, anomaly_type, pnl, thesis, lesson
                  FROM lessons
                 ORDER BY id DESC
                 LIMIT ?
                """,
                (max(1, limit),),
            )
        cols = ["id", "timestamp", "persona", "anomaly_type", "pnl", "thesis", "lesson"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def lesson_count(self, persona: Optional[str] = None) -> int:
        """Return total number of lessons stored (optionally filtered by persona)."""
        cur = self._conn.cursor()
        if persona:
            cur.execute("SELECT COUNT(*) FROM lessons WHERE persona = ?", (persona.strip(),))
        else:
            cur.execute("SELECT COUNT(*) FROM lessons")
        return cur.fetchone()[0]  # type: ignore[return-value]

    # ── L3: MoEWeighter persistence ─────────────────────────────────────────

    def save_moe_weights(
        self,
        weights: Dict[str, float],
        symbol: str = "BTCUSDT",
        sharpe_scores: Optional[Dict[str, float]] = None,
    ) -> None:
        """
        Persist the current MoEWeighter weights to the DB.

        This enables the weights to survive process restarts and prevents
        the per-session reset that was discarding learned weights (L3).

        Args:
            weights:       {persona: weight} dict from MoEWeighter.get_weights()
            symbol:        Trading pair (default BTCUSDT)
            sharpe_scores: {persona: sharpe} optional Sharpe/Sortino scores
        """
        import json as _json

        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS moe_weights (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp    TEXT    NOT NULL,
                    symbol       TEXT    NOT NULL,
                    weights      TEXT    NOT NULL,
                    sharpe_scores TEXT
                )
                """
            )
            cur.execute(
                """
                INSERT INTO moe_weights (timestamp, symbol, weights, sharpe_scores)
                VALUES (?, ?, ?, ?)
                """,
                (
                    datetime.utcnow().isoformat(timespec="seconds"),
                    symbol.strip(),
                    _json.dumps(weights),
                    _json.dumps(sharpe_scores) if sharpe_scores else None,
                ),
            )
            self._conn.commit()

    # ── L1: Evolved config persistence ──────────────────────────────────────

    def save_evolved_config(
        self,
        config_fingerprint: str,
        config_params: Dict[str, float],
        generation: int,
        validation_sharpe: Optional[float] = None,
        train_sharpe: Optional[float] = None,
        max_drawdown: Optional[float] = None,
    ) -> int:
        """
        Persist a GA-evolved config so it survives restarts.

        Args:
            config_fingerprint: Unique string key from GeneticOptimizer._fingerprint()
            config_params:      Dict of {field_name: value}
            generation:         GA generation number
            validation_sharpe:  Sharpe on held-out validation set (walk-forward)
            train_sharpe:      Sharpe on training set
            max_drawdown:       Maximum drawdown % on training set

        Returns:
            The rowid of the inserted config.
        """
        import json as _json

        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS evolved_configs (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp            TEXT    NOT NULL,
                    config_fingerprint   TEXT    NOT NULL UNIQUE,
                    config_params        TEXT    NOT NULL,
                    generation           INTEGER NOT NULL,
                    validation_sharpe    REAL,
                    train_sharpe         REAL,
                    max_drawdown         REAL,
                    is_active            INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Deactivate all previous configs (only one active at a time)
            cur.execute("UPDATE evolved_configs SET is_active = 0")
            cur.execute(
                """
                INSERT INTO evolved_configs
                    (timestamp, config_fingerprint, config_params, generation,
                     validation_sharpe, train_sharpe, max_drawdown, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    datetime.utcnow().isoformat(timespec="seconds"),
                    config_fingerprint,
                    _json.dumps(config_params),
                    generation,
                    validation_sharpe,
                    train_sharpe,
                    max_drawdown,
                ),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    # ── Internal ─────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create the DB file and schema if they don't exist."""
        with self._lock:
            # Ensure parent directory exists
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                isolation_level=None,  # autocommit; we manage transactions explicitly
                timeout=30.0,          # prevent "database is locked" during concurrent backtests
            )

            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode = WAL")      # write-ahead log for concurrency
            cur.execute("PRAGMA foreign_keys = ON")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS lessons (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp    TEXT    NOT NULL,
                    persona      TEXT    NOT NULL,
                    anomaly_type TEXT    NOT NULL,
                    pnl          REAL    NOT NULL,
                    thesis       TEXT    NOT NULL,
                    lesson       TEXT    NOT NULL,
                    source_mode  TEXT    NOT NULL DEFAULT 'DRY_RUN'
                )
                """
            )

            # L6 migration: Add source_mode column to existing tables that lack it.
            # Only run if the lessons table already existed (pre-L6 schema).
            # We detect this by checking if the table exists AND lacks source_mode.
            try:
                cur.execute("PRAGMA table_info(lessons)")
                cols = [row[1] for row in cur.fetchall()]
                if cols and "source_mode" not in cols:
                    cur.execute(
                        "ALTER TABLE lessons ADD COLUMN source_mode TEXT NOT NULL DEFAULT 'DRY_RUN'"
                    )
            except sqlite3.OperationalError:
                pass  # Table doesn't exist yet — no migration needed

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_persona_anomaly
                  ON lessons (persona, anomaly_type)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_timestamp
                  ON lessons (timestamp)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_source_mode
                  ON lessons (source_mode)
                """
            )

    def checkpoint(self) -> dict:
        """
        S6: Explicitly checkpoint the WAL before graceful shutdown.

        PRAGMA wal_checkpoint(TRUNCATE) writes all WAL frames back to the main
        DB file and truncates the WAL to zero bytes, ensuring all in-memory
        writes are durable without requiring a full database close.

        Call this method before:
          - SIGTERM / SIGINT shutdown handlers
          - Container stop (Docker stop, Kubernetes SIGTERM)
          - Any controlled process exit

        Returns
        -------
        dict with keys:
            - success  : bool
            - message  : str
            - frames   : int (WAL pages written back)
            - pages    : int (pages in WAL before checkpoint)
        """
        with self._lock:
            if getattr(self, "_conn", None) is None:
                return {"success": False, "message": "Not connected", "frames": 0, "pages": 0}

            try:
                cur = self._conn.cursor()
                # PASSIVE checkpoint — does not block writers
                cur.execute("PRAGMA wal_checkpoint(PASSIVE)")
                result = cur.fetchone()
                # result: (busy, log_pages, marked_bytes, new_readonly)
                log_pages = result[1] if result else 0

                if log_pages == 0:
                    msg = "WAL is clean; no checkpoint needed"
                    success = True
                else:
                    # If WAL is still busy, truncate it (blocking but fast)
                    cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    msg = f"Checkpoint complete; {log_pages} WAL pages written back"
                    success = True

                logger.debug("[MemoryBank] checkpoint: %s", msg)
                return {"success": success, "message": msg, "frames": log_pages, "pages": log_pages}
            except Exception as exc:
                logger.error("[MemoryBank] checkpoint failed: %s", exc)
                return {"success": False, "message": str(exc), "frames": 0, "pages": 0}

    def close(self):
        """Safely close the SQLite database connection."""
        self.checkpoint()   # S6: ensure WAL is flushed before closing
        with self._lock:
            if getattr(self, '_conn', None):
                self._conn.close()
                self._conn = None

    def register_signal_handler(self) -> None:
        """
        S6: Register this MemoryBank instance for graceful checkpoint on shutdown.

        Wires SIGTERM and SIGINT (Ctrl+C) to:
            1. Call self.checkpoint() to flush the WAL
            2. Call self.close() to close the DB connection
        Idempotent: multiple calls are safe (only one handler is registered per
        signal per process).

        Call this once at application startup when the MemoryBank is the primary
        persistence layer, e.g.:

            bank = MemoryBank()
            bank.register_signal_handler()

        The handler logs the checkpoint result before exiting.
        """
        def _on_signal(signum: int, _frame) -> None:
            sig_name = signal.Signals(signum).name
            logger.info(f"[MemoryBank] Caught {sig_name}; checkpointing before exit...")
            result = self.checkpoint()
            logger.info(
                f"[MemoryBank] Checkpoint result: success={result['success']}  "
                f"frames={result['frames']}  msg={result['message']}"
            )
            self.close()
            # Re-raise the signal so the default Python handler can exit
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        for sig in (signal.SIGTERM, signal.SIGINT):
            # Only register if no handler is already set (or if it's default/ignore)
            old = signal.signal(sig, signal.SIG_DFL)
            if old not in (signal.SIG_DFL, signal.SIG_IGN):
                # A custom handler already exists; don't override
                signal.signal(sig, old)
                logger.warning(
                    "[MemoryBank] SIG%s already has a custom handler; "
                    "skipping MemoryBank registration. "
                    "Call bank.checkpoint() manually in your existing handler.",
                    signal.Signals(sig).name,
                )
            else:
                signal.signal(sig, _on_signal)
                logger.info(f"[MemoryBank] Registered {sig.name} checkpoint handler")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

