"""Dry-run support for bulk ingest: a no-write store and a caching API client.

`PlanningStore` implements the `Store` interface around a real store. Reads pass through;
`upsert` writes nothing and instead records, per table, how many of the offered rows are new,
changed or identical to what is stored, and estimates the rows D1 would write. Any other
write raises, so a dry run cannot write by accident. The ingest functions run unchanged on it.

`CachingClient` remembers every API response, so a plan pass followed by the real pass spends
the API calls once.

Estimate: a new row writes 1 row plus one per index (`games` has two); a changed row writes 1,
plus the index rows when an indexed column changed. D1's own `rows_written` meta is the truth;
this is the figure used to guard against a bulk run that would eat the daily budget. What was
verified on real D1 (during the storage task) is a 1,200-row `games` upsert sent in chunks of
500, 500 and 200 rows and a 365-row `ratings` upsert: the insert and unchanged-rerun costs
come from those. A full 6,300-game season and the CHANGED-row cost are a model that has not
been checked against real D1.

Everything the ingest does to stored games (the rows it re-derives, the absent games it
neutralizes, the duplicates it unrates) goes through `upsert`, so the plan counts them too. The
operator overrides run after the ingest and are not simulated: the caller adds
`override_restore_writes(entries)` for them. An overridden game that differs from the source
shows up as "changed" in the plan (the ingest would overwrite it) and the restore is the second
write.
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from database.store import DEFAULT_CHUNK_ROWS, Store, WriteStats, check_identifier

INDEXES_PER_TABLE = {"games": 2}
INDEXED_COLUMNS = {"games": frozenset({"season", "week", "is_rated"})}
# Rows an override entry writes when it restores its game after the ingest: one row, plus the two
# index rows for `exclude` because is_rated is indexed. `score` and `site` touch no indexed column.
OVERRIDE_RESTORE_WRITES = {"score": 1, "site": 1, "exclude": 3}


def override_restore_writes(entries: Sequence[dict]) -> int:
    return sum(OVERRIDE_RESTORE_WRITES[e["kind"]] for e in entries)


class DryRunWriteError(RuntimeError):
    """A write other than `upsert` reached the planning store."""


@dataclass
class TablePlan:
    rows: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    estimated_rows_written: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class PlanningStore(Store):
    def __init__(self, inner: Store):
        self._inner = inner
        self.plans: dict[str, TablePlan] = {}
        self._planned: dict[tuple, dict] = {}  # (table, key values) -> row as it would be stored

    @property
    def estimated_rows_written(self) -> int:
        return sum(p.estimated_rows_written for p in self.plans.values())

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        return self._inner.query(sql, params)

    def query_rows(self, sql: str, params: Sequence[Any] = ()) -> tuple[list[str], list[list]]:
        return self._inner.query_rows(sql, params)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> WriteStats:
        raise DryRunWriteError("a dry run must not write")

    def execute_batch(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> WriteStats:
        raise DryRunWriteError("a dry run must not write")

    def _stored(self, table: str, rows: Sequence[dict], key: Sequence[str]) -> dict[tuple, dict]:
        """Stored rows for the keys of `rows`, read in chunks with the keys as one JSON parameter."""
        check_identifier(table)
        key = [check_identifier(c) for c in key]
        join = " AND ".join(f"t.{c} = j.value->>'$.{c}'" for c in key)
        sql = f"SELECT t.* FROM {table} AS t JOIN json_each(?1) AS j ON {join}"
        found: dict[tuple, dict] = {}
        for start in range(0, len(rows), DEFAULT_CHUNK_ROWS):
            keys = [{c: r[c] for c in key} for r in rows[start:start + DEFAULT_CHUNK_ROWS]]
            for stored in self._inner.query(sql, [json.dumps(keys)]):
                found[tuple(stored[c] for c in key)] = stored
        return found

    def upsert(self, table: str, rows: Sequence[dict], key: Sequence[str], **_chunking: Any) -> WriteStats:
        if not rows:
            return WriteStats()
        columns = set(rows[0])
        if any(set(r) != columns for r in rows):
            raise ValueError("rows must share the same columns")
        stored = self._stored(table, rows, key)
        plan = self.plans.setdefault(table, TablePlan())
        indexes = INDEXES_PER_TABLE.get(table, 0)
        indexed = INDEXED_COLUMNS.get(table, frozenset())
        written = 0
        for row in rows:
            k = tuple(row[c] for c in key)
            before = self._planned.get((table, k)) or stored.get(k)
            plan.rows += 1
            if before is None:
                plan.new += 1
                cost = 1 + indexes
            else:
                diff = {c for c in row if row[c] != before.get(c)}
                if not diff:
                    plan.unchanged += 1
                    continue
                plan.changed += 1
                cost = 1 + (indexes if diff & indexed else 0)
            self._planned[(table, k)] = {**(before or {}), **row}
            written += cost
        plan.estimated_rows_written += written
        return WriteStats(rows_written=written)


class CachingClient:
    """Wraps an API client; a repeated request is answered from memory and costs no call."""

    def __init__(self, inner):
        self._inner = inner
        self._cache: dict[tuple, Any] = {}

    @property
    def calls(self) -> int:
        return self._inner.calls

    @property
    def quota_remaining(self) -> int | None:
        return self._inner.quota_remaining

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        cache_key = (path, json.dumps(params or {}, sort_keys=True))
        if cache_key not in self._cache:
            self._cache[cache_key] = self._inner.get(path, params)
        return copy.deepcopy(self._cache[cache_key])
