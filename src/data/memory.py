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
    ) -> int:
        """
        Persist a single lesson to the Memory Bank.

        Args:
            persona:      Persona name, e.g. "SMC_ICT"
            anomaly_type: Anomaly that triggered the trade, e.g. "LIQUIDITY_SWEEP"
            pnl:          Signed % return (negative = loss, positive = gain)
            thesis:       The LLM persona's stated thesis when entering
            lesson:       1-sentence rule the LLM produced from reflexion

        Returns:
            The rowid of the newly inserted lesson.
        """
        if not lesson or not lesson.strip():
            return -1  # No-op for empty lessons

        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO lessons (timestamp, persona, anomaly_type, pnl, thesis, lesson)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.utcnow().isoformat(timespec="seconds"),
                    persona.strip(),
                    anomaly_type.strip(),
                    float(pnl),
                    thesis.strip(),
                    lesson.strip(),
                ),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def retrieve_lessons(
        self,
        persona: str,
        anomaly_type: str,
        limit: int = 3,
    ) -> List[str]:
        """
        RAG retrieval: fetch the most recent lessons for a persona + anomaly pair.

        Args:
            persona:      Persona name to filter by
            anomaly_type: Anomaly type to filter by
            limit:        Maximum number of lessons to return (default 3)

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
             ORDER BY id DESC
             LIMIT ?
            """,
            (persona.strip(), anomaly_type.strip(), max(1, limit)),
        )
        return [row[0] for row in cur.fetchall()]

    def retrieve_lessons_batch(
        self,
        personas: List[str],
        anomaly_type: str,
        limit_per: int = 3,
    ) -> Dict[str, List[str]]:
        """
        Fetch lessons for multiple personas in a single query.
        Returns dict mapping persona -> list of lesson strings.
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
             ORDER BY id DESC
            """,
            (*[p.strip() for p in personas], anomaly_type.strip()),
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
                    lesson       TEXT    NOT NULL
                )
                """
            )

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

    def close(self):
        """Safely close the SQLite database connection."""
        with self._lock:
            if getattr(self, '_conn', None):
                self._conn.close()
                self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

