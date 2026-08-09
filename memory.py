"""SQLite persistence for conversation history and portfolio snapshots.

Two things need to survive a restart: what the advisor and I have already
said to each other, and what the book looked like on a given day. The
first stops the chat losing its thread; the second is what makes
"you've held Figma down 78% for a year" a statement of fact rather than
a guess.

    from memory import Memory
    memory = Memory()
    memory.add_message("user", "why is my semis weight so high?")
    history = memory.recent_messages(limit=20)

Single-file database at ``data/investor.db``. No ORM, no migrations —
the schema is created on first use and extended with ALTER TABLE guards.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "investor.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL DEFAULT 'default',
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'chat',
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages (session_id, id);

CREATE TABLE IF NOT EXISTS snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date     TEXT    NOT NULL UNIQUE,
    total_value       REAL,
    investments_value REAL,
    cost_basis        REAL,
    unrealised_pl     REAL,
    unrealised_pct    REAL,
    realised_pl       REAL,
    cash              REAL,
    positions         INTEGER,
    holdings_json     TEXT,
    created_at        TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS briefs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_date   TEXT NOT NULL,
    channel      TEXT NOT NULL DEFAULT 'app',
    body         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_briefs_date ON briefs (brief_date);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Memory:
    """Thin wrapper over the SQLite file. Safe to construct per request."""

    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    # ------------------------------------------------------------ messages

    def add_message(
        self,
        role: str,
        content: str,
        *,
        session_id: str = "default",
        kind: str = "chat",
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO messages (session_id, role, content, kind, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, role, content, kind, _now()),
            )
            return int(cursor.lastrowid)

    def recent_messages(
        self, *, session_id: str = "default", limit: int = 20, kind: str = "chat"
    ) -> list[dict]:
        """Oldest-first, so the list can go straight into an API call."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT role, content, kind, created_at FROM messages "
                "WHERE session_id = ? AND kind = ? "
                "ORDER BY id DESC LIMIT ?",
                (session_id, kind, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def message_count(self, *, session_id: str = "default") -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["n"])

    def clear_messages(self, *, session_id: str = "default") -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )

    # ----------------------------------------------------------- snapshots

    def save_snapshot(self, portfolio: dict, *, when: str | None = None) -> int:
        """Record today's book. Re-running on the same day overwrites it."""
        summary = portfolio.get("summary") or {}
        holdings = portfolio.get("holdings") or []
        stamp = when or date.today().isoformat()

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO snapshots (
                    snapshot_date, total_value, investments_value, cost_basis,
                    unrealised_pl, unrealised_pct, realised_pl, cash, positions,
                    holdings_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_date) DO UPDATE SET
                    total_value       = excluded.total_value,
                    investments_value = excluded.investments_value,
                    cost_basis        = excluded.cost_basis,
                    unrealised_pl     = excluded.unrealised_pl,
                    unrealised_pct    = excluded.unrealised_pct,
                    realised_pl       = excluded.realised_pl,
                    cash              = excluded.cash,
                    positions         = excluded.positions,
                    holdings_json     = excluded.holdings_json,
                    created_at        = excluded.created_at
                """,
                (
                    stamp,
                    summary.get("Total account value"),
                    summary.get("Investments value"),
                    summary.get("Invested (cost basis)"),
                    summary.get("Unrealised P/L"),
                    summary.get("Unrealised P/L %"),
                    summary.get("Realised P/L (all time)"),
                    (summary.get("Cash available to trade") or 0.0)
                    + (summary.get("Cash in pies") or 0.0),
                    len(holdings),
                    json.dumps(holdings, default=str),
                    _now(),
                ),
            )
            return int(cursor.lastrowid)

    def snapshots(self, limit: int = 90) -> list[dict]:
        """Most recent snapshots, oldest-first for charting."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT snapshot_date, total_value, investments_value, cost_basis, "
                "unrealised_pl, unrealised_pct, realised_pl, cash, positions "
                "FROM snapshots ORDER BY snapshot_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def latest_snapshot(self) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots ORDER BY snapshot_date DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def snapshot_on_or_before(self, target: str) -> dict | None:
        """Nearest snapshot at or before a date — for period comparisons."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE snapshot_date <= ? "
                "ORDER BY snapshot_date DESC LIMIT 1",
                (target,),
            ).fetchone()
        return dict(row) if row else None

    # -------------------------------------------------------------- briefs

    def save_brief(self, body: str, *, channel: str = "app") -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO briefs (brief_date, channel, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (date.today().isoformat(), channel, body, _now()),
            )
            return int(cursor.lastrowid)

    def briefs(self, limit: int = 30) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT brief_date, channel, body, created_at FROM briefs "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def brief_sent_today(self, *, channel: str = "telegram") -> bool:
        """Guard against double-sending the morning update."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM briefs WHERE brief_date = ? AND channel = ?",
                (date.today().isoformat(), channel),
            ).fetchone()
        return int(row["n"]) > 0

    # --------------------------------------------------------------- notes

    def add_note(self, body: str, *, ticker: str | None = None) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO notes (ticker, body, created_at) VALUES (?, ?, ?)",
                (ticker, body, _now()),
            )
            return int(cursor.lastrowid)

    def notes(self, *, ticker: str | None = None, limit: int = 50) -> list[dict]:
        query = "SELECT id, ticker, body, created_at FROM notes"
        params: tuple[Any, ...] = ()
        if ticker:
            query += " WHERE ticker = ?"
            params = (ticker,)
        query += " ORDER BY id DESC LIMIT ?"
        params += (limit,)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # --------------------------------------------------------------- stats

    def stats(self) -> dict:
        with self._connect() as connection:
            def scalar(sql: str) -> int:
                return int(connection.execute(sql).fetchone()[0])

            return {
                "messages": scalar("SELECT COUNT(*) FROM messages"),
                "snapshots": scalar("SELECT COUNT(*) FROM snapshots"),
                "briefs": scalar("SELECT COUNT(*) FROM briefs"),
                "notes": scalar("SELECT COUNT(*) FROM notes"),
                "db_path": str(self.path),
                "db_size_kb": round(self.path.stat().st_size / 1024, 1)
                if self.path.exists()
                else 0.0,
            }


if __name__ == "__main__":
    from prices import load_portfolio

    memory = Memory()
    memory.save_snapshot(load_portfolio())
    print(json.dumps(memory.stats(), indent=2))
