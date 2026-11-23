"""The storage interface shared by the D1 client and the local sqlite3 store.

Both implementations run the SAME SQL, built here, so unit tests against the local store
exercise exactly the statements that reach D1.

Rules every writer must respect (see AGENTS.md):

* Integers must reach SQLite through the JSON path (`upsert`). D1's REST API delivers plain
  numeric bound parameters as REAL, so binding an int directly to a write statement stores a
  float (and STRICT INTEGER columns reject non-whole values).
* Upserts carry a changed-values guard so re-running an unchanged ingest writes zero rows.
  D1's free plan allows 100,000 rows written per day, and each index adds a row per insert.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Target size of the single JSON parameter of one upsert statement. The REST API rejects
# requests of 2 MB or more (and the plan's budget is <= 1 MB), so stay well below.
DEFAULT_CHUNK_BYTES = 1_000_000
DEFAULT_CHUNK_ROWS = 500


def check_identifier(name: str) -> str:
    """Return `name` if it is a plain SQL identifier; identifiers are interpolated, never bound."""
    if not isinstance(name, str) or not IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return name


@dataclass(frozen=True)
class WriteStats:
    """Totals from the `meta` of one or more responses.

    The local store reports the sqlite3 changed-row count as rows_written (indexes not
    counted) and 0 for rows_read.

    `retried_writes` counts write requests that were re-sent after a timeout or connection
    error. The first attempt may have committed before the connection dropped, in which case
    the retry changed nothing and reported 0 rows, so `rows_written` may be UNDER-COUNTED
    whenever `retried_writes` is non-zero.
    """

    rows_written: int = 0
    rows_read: int = 0
    retried_writes: int = 0

    def __add__(self, other: "WriteStats") -> "WriteStats":
        return WriteStats(
            self.rows_written + other.rows_written,
            self.rows_read + other.rows_read,
            self.retried_writes + other.retried_writes,
        )


def build_upsert_sql(table: str, columns: Sequence[str], key: Sequence[str]) -> str:
    """The upsert statement for one chunk; the chunk is bound as ?1, a JSON array of objects.

    `value->>'$.c'` yields native SQLite types: JSON integers stay INTEGER, JSON null becomes
    NULL, strings stay TEXT. The WHERE on DO UPDATE skips rows whose values are unchanged, so a
    no-op upsert writes nothing.
    """
    check_identifier(table)
    columns = [check_identifier(c) for c in columns]
    key = [check_identifier(c) for c in key]
    if not key:
        raise ValueError("upsert requires at least one key column")
    if len(set(columns)) != len(columns):
        raise ValueError("duplicate column names")
    missing = [k for k in key if k not in columns]
    if missing:
        raise ValueError(f"key columns not in row columns: {missing}")

    selects = ", ".join(f"value->>'$.{c}'" for c in columns)
    sql = (
        f"INSERT INTO {table}({', '.join(columns)}) "
        f"SELECT {selects} FROM json_each(?1) WHERE true "
        f"ON CONFLICT({', '.join(key)}) "
    )
    non_key = [c for c in columns if c not in key]
    if not non_key:
        return sql + "DO NOTHING"
    sets = ", ".join(f"{c} = excluded.{c}" for c in non_key)
    guard = " OR ".join(f"{table}.{c} IS NOT excluded.{c}" for c in non_key)
    return sql + f"DO UPDATE SET {sets} WHERE {guard}"


def chunk_json(
    rows: Sequence[dict],
    *,
    max_rows: int = DEFAULT_CHUNK_ROWS,
    max_bytes: int = DEFAULT_CHUNK_BYTES,
) -> Iterator[tuple[str, int]]:
    """Yield (json_array_text, row_count) chunks bounded by row count and serialized size."""
    if max_rows < 1 or max_bytes < 1:
        raise ValueError("max_rows and max_bytes must be positive")
    pieces: list[str] = []
    size = 2  # the enclosing brackets
    for row in rows:
        piece = json.dumps(row, separators=(",", ":"), allow_nan=False, ensure_ascii=False)
        piece_bytes = len(piece.encode("utf-8"))
        if piece_bytes + 2 > max_bytes:
            raise ValueError(f"a single row serializes to {piece_bytes} bytes, over {max_bytes}")
        if pieces and (len(pieces) >= max_rows or size + piece_bytes + 1 > max_bytes):
            yield "[" + ",".join(pieces) + "]", len(pieces)
            pieces, size = [], 2
        pieces.append(piece)
        size += piece_bytes + (1 if len(pieces) > 1 else 0)
    if pieces:
        yield "[" + ",".join(pieces) + "]", len(pieces)


class Store(ABC):
    """Interface implemented by `D1Client` and `LocalStore`."""

    @abstractmethod
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        """Run a read; one dict per row."""

    @abstractmethod
    def query_rows(self, sql: str, params: Sequence[Any] = ()) -> tuple[list[str], list[list]]:
        """Run a read; (column names, row lists). Preferred for bulk reads."""

    @abstractmethod
    def execute(self, sql: str, params: Sequence[Any] = ()) -> WriteStats:
        """Run one write statement."""

    @abstractmethod
    def execute_batch(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> WriteStats:
        """Run statements atomically: if one fails, none are applied."""

    def upsert(
        self,
        table: str,
        rows: Sequence[dict],
        key: Sequence[str],
        *,
        chunk_rows: int = DEFAULT_CHUNK_ROWS,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    ) -> WriteStats:
        """Insert or update rows keyed by `key`, one statement per chunk.

        Every row must have the same keys. Values must be JSON-serializable (int, float, str,
        None). Unchanged rows write nothing. Chunks are separate statements and are not atomic
        together; re-running is safe because the upsert is idempotent.
        """
        if not rows:
            return WriteStats()
        columns = list(rows[0])
        for row in rows:
            if set(row) != set(columns):
                raise ValueError(f"rows must share the same columns; got {sorted(row)} vs {sorted(columns)}")
        sql = build_upsert_sql(table, columns, tuple(key))
        total = WriteStats()
        for payload, _count in chunk_json(rows, max_rows=chunk_rows, max_bytes=chunk_bytes):
            total += self.execute(sql, [payload])
        return total
