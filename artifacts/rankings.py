"""The weekly rankings payload, built from what is STORED (`ratings` joined to `teams`), never
from the in-memory model result, so republishing a week reproduces the original artifact.

Shape (the contract the site already consumes; football's `build_payload` is the model):

    {"season": 2026, "week": 19, "generated_at_utc": "...",
     "rankings": [{"rank": 1, "team": "Duke", "conference": "ACC", "logo": "https://...|null",
                   "record": "30-3", "rating": 87.12, "delta": 2}, ...]}

Rank is rating descending (the stored value, not the rounded one), ties broken by team name
ascending (case-insensitive), then team id, so the order never depends on row order. A week whose
ratings rows do not all join to a `teams` row is refused (`PublishError`) rather than published
short. `delta` is the previous rank minus the current rank, where "previous" is the greatest
earlier week of the same season that has ratings; 0 when there is none or the team was not
ranked then. `conference` is `teams.conference` as the source gives it (already a short display
name); `logo` is `teams.logo_dark` or null.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from artifacts.r2 import PublishError
from database.store import Store

RATINGS_TABLES = ("ratings", "ratings_staging")  # the only tables a payload may be built from
RATING_DECIMALS = 2


def check_table(table: str) -> str:
    if table not in RATINGS_TABLES:
        raise ValueError(f"ratings table must be one of {RATINGS_TABLES}, got {table!r}")
    return table


def stored_week(store: Store, season: int, week: int, table: str = "ratings", *, require_complete: bool = True) -> list[dict]:
    """Stored ratings of one week joined to that season's teams. Raises `PublishError` naming the
    team ids when some ratings rows have no `teams` row for the season: the list would be short.
    The previous week, read only for ranks, is not held to that (`require_complete=False`)."""
    check_table(table)
    rows = store.query(
        f"SELECT r.team_id, t.school AS team, t.conference, t.logo_dark AS logo, "
        f"r.rating, r.wins, r.losses "
        f"FROM {table} r JOIN teams t ON t.id = r.team_id AND t.season = r.season "
        f"WHERE r.season = ? AND r.week = ?",
        [season, week],
    )
    stored_ids = {r["team_id"] for r in store.query(
        f"SELECT team_id FROM {table} WHERE season = ? AND week = ?", [season, week]
    )}
    if require_complete and len(rows) != len(stored_ids):
        missing = sorted(stored_ids - {r["team_id"] for r in rows})
        raise PublishError(
            f"{table} has {len(stored_ids)} rows for season {season} week {week} but only {len(rows)} join to a team of "
            f"that season: missing team ids {missing[:20]}{' ...' if len(missing) > 20 else ''}; not publishing a short "
            f"ranking (re-ingest the season's teams, then rerun)"
        )
    return rows


def previous_week_with_data(store: Store, season: int, week: int, table: str = "ratings") -> int | None:
    """The greatest week before `week` in the season that has ratings, or None."""
    check_table(table)
    rows = store.query(
        f"SELECT MAX(week) AS week FROM {table} WHERE season = ? AND week < ?", [season, week]
    )
    value = rows[0]["week"] if rows else None
    return None if value is None else int(value)


def rank_rows(rows: Sequence[dict]) -> list[dict]:
    """Rows ordered best first, each with `rank` (1 = best). Equal ratings are common (many teams
    sit at the rating floor), so the tie-break is explicit and total: team name, case-insensitively,
    then team id."""
    ordered = sorted(rows, key=lambda r: (-float(r["rating"]), r["team"].casefold(), r["team_id"]))
    return [{**r, "rank": i} for i, r in enumerate(ordered, 1)]


def build_payload(
    store: Store, season: int, week: int, *, table: str = "ratings", now: datetime | None = None,
) -> dict[str, Any]:
    """The week payload. Raises ValueError when the week has no stored ratings."""
    current = rank_rows(stored_week(store, season, week, table))
    if not current:
        raise ValueError(f"no stored ratings in {table} for season {season} week {week}")
    previous_week = previous_week_with_data(store, season, week, table)
    previous_rank = (
        {r["team_id"]: r["rank"] for r in rank_rows(stored_week(store, season, previous_week, table, require_complete=False))}
        if previous_week is not None else {}
    )
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "season": season,
        "week": week,
        "generated_at_utc": stamp.isoformat(),
        "rankings": [
            {
                "rank": r["rank"],
                "team": r["team"],
                "conference": r["conference"],
                "logo": r["logo"] or None,
                "record": f"{int(r['wins'])}-{int(r['losses'])}",
                "rating": round(float(r["rating"]), RATING_DECIMALS),
                "delta": previous_rank[r["team_id"]] - r["rank"] if r["team_id"] in previous_rank else 0,
            }
            for r in current
        ],
    }
