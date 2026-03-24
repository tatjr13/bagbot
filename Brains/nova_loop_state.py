"""SQLite state database for the Nova mining loop.

Single WAL-mode database holds: proposals, outcomes, directives, metrics,
reward snapshots.  This is the source of truth — markdown views are
rendered from here.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ── schema ──────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS proposals (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    timer           TEXT NOT NULL,          -- 'strategy' | 'research'
    action          TEXT NOT NULL,
    params_json     TEXT NOT NULL DEFAULT '{}',
    expected_delay_min  INTEGER DEFAULT 5,
    risk            TEXT DEFAULT 'low',
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending|committed|executed|succeeded|failed|rolled_back
    attribution_window_min  INTEGER DEFAULT 30,
    rollback_json   TEXT DEFAULT NULL,
    committed_at    TEXT,
    executed_at     TEXT,
    resolved_at     TEXT,
    outcome_json    TEXT,
    score_before    REAL,
    score_after     REAL,
    reward          REAL,
    notes           TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS directives (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at     TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'inbox',     -- inbox|telegram|manual
    raw_text        TEXT NOT NULL,
    parsed_action   TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending|applied|rejected|expired
    applied_at      TEXT,
    notes           TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at     TEXT NOT NULL,
    metric          TEXT NOT NULL,
    value           REAL NOT NULL,
    metadata_json   TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    category        TEXT NOT NULL,          -- health|strategy|research|safety|system
    level           TEXT NOT NULL DEFAULT 'info',  -- debug|info|warn|error|urgent
    message         TEXT NOT NULL,
    metadata_json   TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS reward_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at     TEXT NOT NULL,
    our_score       REAL,
    leader_score    REAL,
    score_gap       REAL,
    rank            INTEGER,
    field_size      INTEGER,
    heavy_norm      REAL,
    metadata_json   TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_directives_status ON directives(status);
CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_metrics_metric ON metrics(metric);
"""


class NovaStateDB:
    """Thread-safe SQLite state store with WAL mode."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path) if not isinstance(db_path, Path) else db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ── proposals ───────────────────────────────────────────────────────

    def insert_proposal(
        self,
        *,
        proposal_id: str,
        timer: str,
        action: str,
        params: dict[str, Any] | None = None,
        expected_delay_min: int = 5,
        risk: str = "low",
        attribution_window_min: int = 30,
        rollback: dict[str, Any] | None = None,
        score_before: float | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO proposals
                   (id, created_at, timer, action, params_json,
                    expected_delay_min, risk, attribution_window_min,
                    rollback_json, score_before)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    proposal_id,
                    iso_now(),
                    timer,
                    action,
                    json.dumps(params or {}),
                    expected_delay_min,
                    risk,
                    attribution_window_min,
                    json.dumps(rollback) if rollback else None,
                    score_before,
                ),
            )

    def commit_proposal(self, proposal_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE proposals SET status='committed', committed_at=? WHERE id=?",
                (iso_now(), proposal_id),
            )

    def execute_proposal(self, proposal_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE proposals SET status='executed', executed_at=? WHERE id=?",
                (iso_now(), proposal_id),
            )

    def resolve_proposal(
        self,
        proposal_id: str,
        *,
        success: bool,
        score_after: float | None = None,
        reward: float | None = None,
        outcome: dict[str, Any] | None = None,
        notes: str = "",
    ) -> None:
        status = "succeeded" if success else "failed"
        with self._tx() as conn:
            conn.execute(
                """UPDATE proposals
                   SET status=?, resolved_at=?, score_after=?, reward=?,
                       outcome_json=?, notes=?
                   WHERE id=?""",
                (
                    status,
                    iso_now(),
                    score_after,
                    reward,
                    json.dumps(outcome or {}),
                    notes,
                    proposal_id,
                ),
            )

    def rollback_proposal(self, proposal_id: str, notes: str = "") -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE proposals SET status='rolled_back', resolved_at=?, notes=? WHERE id=?",
                (iso_now(), notes, proposal_id),
            )

    def pending_proposals(self) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM proposals WHERE status IN ('pending', 'committed', 'executed') ORDER BY created_at")

    def matured_proposals(self) -> list[dict[str, Any]]:
        """Proposals whose attribution window has passed but aren't resolved yet."""
        rows = self._query(
            """SELECT * FROM proposals
               WHERE status = 'executed'
               AND datetime(executed_at, '+' || attribution_window_min || ' minutes') <= datetime('now')
               ORDER BY executed_at"""
        )
        return rows

    def recent_proposals(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._query(f"SELECT * FROM proposals ORDER BY created_at DESC LIMIT {limit}")

    def proposal_stats(self) -> dict[str, int]:
        """Count proposals by status."""
        rows = self._conn.execute("SELECT status, COUNT(*) as cnt FROM proposals GROUP BY status").fetchall()
        return {row["status"]: row["cnt"] for row in rows}

    # ── directives ──────────────────────────────────────────────────────

    def insert_directive(self, *, raw_text: str, source: str = "inbox", parsed_action: str | None = None) -> int:
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO directives (received_at, source, raw_text, parsed_action) VALUES (?, ?, ?, ?)",
                (iso_now(), source, raw_text, parsed_action),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def pending_directives(self) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM directives WHERE status='pending' ORDER BY received_at")

    def apply_directive(self, directive_id: int, notes: str = "") -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE directives SET status='applied', applied_at=?, notes=? WHERE id=?",
                (iso_now(), notes, directive_id),
            )

    def reject_directive(self, directive_id: int, notes: str = "") -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE directives SET status='rejected', applied_at=?, notes=? WHERE id=?",
                (iso_now(), notes, directive_id),
            )

    # ── metrics ─────────────────────────────────────────────────────────

    def record_metric(self, metric: str, value: float, metadata: dict[str, Any] | None = None) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO metrics (recorded_at, metric, value, metadata_json) VALUES (?, ?, ?, ?)",
                (iso_now(), metric, value, json.dumps(metadata or {})),
            )

    def latest_metric(self, metric: str) -> float | None:
        row = self._conn.execute(
            "SELECT value FROM metrics WHERE metric=? ORDER BY recorded_at DESC LIMIT 1",
            (metric,),
        ).fetchone()
        return row["value"] if row else None

    def metric_history(self, metric: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._query(f"SELECT * FROM metrics WHERE metric=? ORDER BY recorded_at DESC LIMIT {limit}", (metric,))

    # ── events ──────────────────────────────────────────────────────────

    def log_event(
        self,
        category: str,
        message: str,
        *,
        level: str = "info",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO events (timestamp, category, level, message, metadata_json) VALUES (?, ?, ?, ?, ?)",
                (iso_now(), category, level, message, json.dumps(metadata or {})),
            )

    def recent_events(self, limit: int = 30, category: str | None = None) -> list[dict[str, Any]]:
        if category:
            return self._query(
                f"SELECT * FROM events WHERE category=? ORDER BY timestamp DESC LIMIT {limit}",
                (category,),
            )
        return self._query(f"SELECT * FROM events ORDER BY timestamp DESC LIMIT {limit}")

    def urgent_events(self, since: str | None = None) -> list[dict[str, Any]]:
        if since:
            return self._query(
                "SELECT * FROM events WHERE level='urgent' AND timestamp>=? ORDER BY timestamp",
                (since,),
            )
        return self._query("SELECT * FROM events WHERE level='urgent' ORDER BY timestamp DESC LIMIT 10")

    # ── reward snapshots ────────────────────────────────────────────────

    def record_reward_snapshot(
        self,
        *,
        our_score: float | None = None,
        leader_score: float | None = None,
        score_gap: float | None = None,
        rank: int | None = None,
        field_size: int | None = None,
        heavy_norm: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO reward_snapshots
                   (recorded_at, our_score, leader_score, score_gap, rank, field_size, heavy_norm, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (iso_now(), our_score, leader_score, score_gap, rank, field_size, heavy_norm, json.dumps(metadata or {})),
            )

    def latest_reward_snapshot(self) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM reward_snapshots ORDER BY recorded_at DESC LIMIT 1")
        return rows[0] if rows else None

    def reward_history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._query(f"SELECT * FROM reward_snapshots ORDER BY recorded_at DESC LIMIT {limit}")

    # ── status rendering ────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        """Build a summary dict for STATUS.md rendering."""
        proposal_stats = self.proposal_stats()
        latest_reward = self.latest_reward_snapshot()
        recent = self.recent_events(limit=5)
        pending_dirs = self.pending_directives()
        return {
            "proposals": proposal_stats,
            "latest_reward": latest_reward,
            "recent_events": recent,
            "pending_directives": len(pending_dirs),
        }

    # ── internals ───────────────────────────────────────────────────────

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
