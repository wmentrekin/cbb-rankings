"""Local sqlite3 implementation of the storage interface.

Applies the migration files and runs the same SQL the D1 client sends, so unit tests and dry
runs need no network. Differences from D1: rows_written is sqlite3's changed-row count (index
writes are not counted) and rows_read is always 0; bound integers stay integers here, whereas
D1's REST API turns plain numeric parameters into REAL, so keep writes on the `upsert` JSON path.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Sequence

from database.store import Store, WriteStats

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIN_SQLITE = (3, 38, 0)  # value->>'$.path' needs 3.38
_MIGRATION_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.sql$")

# Same shape wrangler keeps, so a local database and a remote one look alike.
_TRACKING_DDL = (
    "CREATE TABLE IF NOT EXISTS d1_migrations ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "name TEXT UNIQUE, "
    "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL)"
)


def check_sqlite_version() -> None:
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} is too old: the storage SQL uses value->>'$.path', "
            "which needs SQLite >= 3.38."
        )


_NUMBERED_RE = re.compile(r"\?\d")


def _bind(sql: str, params: Sequence[Any]):
    """sqlite3 wants a dict for numbered placeholders (?1), as the upsert statements use."""
    if _NUMBERED_RE.search(sql):
        return {str(i): v for i, v in enumerate(params, 1)}
    return list(params)


class LocalStore(Store):
    def __init__(self, path: str = ":memory:", migrations_dir: Path = MIGRATIONS_DIR):
        check_sqlite_version()
        self._conn = sqlite3.connect(path, isolation_level=None)  # explicit transactions
        self._conn.row_factory = sqlite3.Row
        self.apply_migrations(migrations_dir)

    def apply_migrations(self, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
        """Apply unapplied *.sql files in name order, one transaction each; return names applied."""
        self._conn.execute(_TRACKING_DDL)
        done = {r[0] for r in self._conn.execute("SELECT name FROM d1_migrations")}
        applied = []
        for path in sorted(Path(migrations_dir).glob("*.sql")):
            if path.name in done:
                continue
            if not _MIGRATION_NAME_RE.fullmatch(path.name):
                raise ValueError(f"unsupported migration file name: {path.name}")
            script = (
                f"BEGIN;\n{path.read_text()}\n;\n"
                f"INSERT INTO d1_migrations (name) VALUES ('{path.name}');\nCOMMIT;"
            )
            try:
                self._conn.executescript(script)
            except sqlite3.Error:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
            applied.append(path.name)
        return applied

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        return [dict(r) for r in self._conn.execute(sql, _bind(sql, params)).fetchall()]

    def query_rows(self, sql: str, params: Sequence[Any] = ()) -> tuple[list[str], list[list]]:
        cur = self._conn.execute(sql, _bind(sql, params))
        columns = [d[0] for d in cur.description or []]
        return columns, [list(r) for r in cur.fetchall()]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> WriteStats:
        before = self._conn.total_changes
        self._conn.execute(sql, _bind(sql, params))
        return WriteStats(rows_written=self._conn.total_changes - before)

    def execute_batch(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> WriteStats:
        if not statements:
            return WriteStats()
        before = self._conn.total_changes
        self._conn.execute("BEGIN")
        try:
            for sql, params in statements:
                self._conn.execute(sql, _bind(sql, params))
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        return WriteStats(rows_written=self._conn.total_changes - before)

    def close(self) -> None:
        self._conn.close()
