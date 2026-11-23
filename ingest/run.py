"""Ingestion entry points: `ingest_season` (full) and `ingest_week` (windowed).

    CBB_STORE=local uv run python -m ingest.run season 2026
    uv run python -m ingest.run week 2027 3

API budget (the quota is shared with the football pipeline): a full season is 1 teams call plus
about 1 games call per 3,000 games (4 calls for a 6,300-game season); a weekly update is 1 games
call, plus 1 teams call only if the season has no teams stored yet. All writes go through
`store.upsert`, so re-running writes nothing for unchanged rows.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable

from database import get_store
from database.store import Store, WriteStats
from ingest.derive import DEFAULT_ALPHA, AlphaConfig, derive_game, eastern_date
from ingest.games import LOOKBACK_DAYS, fetch_season_games, fetch_week_games
from ingest.http import CbbdClient
from ingest.teams import fetch_teams, stored_team_ids, team_rows
from ingest.weeks import (
    SeasonRow,
    compute_anchor_monday,
    get_season_row,
    is_provisional,
    load_selection_sunday,
    season_row_dict,
    week_for_date,
)

log = logging.getLogger(__name__)


class SeasonNotIngestedError(RuntimeError):
    """A weekly update needs the season row (and its anchor) created by `ingest_season`."""


@dataclass
class IngestSummary:
    season: int
    week: int | None = None  # None for a full-season run
    api_calls: int = 0
    quota_remaining: int | None = None
    teams_fetched: int = 0
    games_fetched: int = 0
    rows_upserted: dict[str, int] = field(default_factory=dict)
    rows_written: dict[str, int] = field(default_factory=dict)  # from the store, per table
    dropped_both_non_d1: int = 0
    anomalies: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    anchor_monday: str | None = None
    selection_sunday: str | None = None

    @property
    def total_rows_written(self) -> int:
        return sum(self.rows_written.values())

    def as_dict(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "total_rows_written": self.total_rows_written}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _upsert(store: Store, summary: IngestSummary, table: str, rows: list[dict], key: tuple[str, ...]) -> None:
    stats: WriteStats = store.upsert(table, rows, key)
    summary.rows_upserted[table] = summary.rows_upserted.get(table, 0) + len(rows)
    summary.rows_written[table] = summary.rows_written.get(table, 0) + stats.rows_written


def _selection_sunday(season: int, summary: IngestSummary) -> date:
    selection_sunday = load_selection_sunday(season)
    if is_provisional(season):
        summary.warnings.append(f"Selection Sunday {selection_sunday} for season {season} is provisional")
    summary.selection_sunday = selection_sunday.isoformat()
    return selection_sunday


def _store_season_row(store: Store, summary: IngestSummary, row: SeasonRow) -> None:
    _upsert(store, summary, "seasons", [season_row_dict(row)], ("season",))


def _store_games(
    store: Store, summary: IngestSummary, raw_games: list[dict], season_row: SeasonRow,
    d1_ids: set[int], alpha_cfg: AlphaConfig,
) -> None:
    rows: list[dict] = []
    anomalies: Counter = Counter()
    for raw in raw_games:
        outcome = derive_game(
            raw, season=season_row.season, d1_ids=d1_ids, selection_sunday=season_row.selection_sunday,
            week_of=lambda d: week_for_date(season_row, d), alpha_cfg=alpha_cfg,
        )
        for message in outcome.messages:
            log.warning(message)
        anomalies.update(outcome.anomalies)
        summary.dropped_both_non_d1 += outcome.dropped_both_non_d1
        if outcome.row:
            rows.append(outcome.row)
    if summary.dropped_both_non_d1:
        log.info("dropped %d games with both sides non-D-I", summary.dropped_both_non_d1)
    for code, n in anomalies.items():
        summary.anomalies[code] = summary.anomalies.get(code, 0) + n
    rows.sort(key=lambda r: r["id"])
    _upsert(store, summary, "games", rows, ("id",))


def _finish(store: Store, summary: IngestSummary, client: CbbdClient, row: SeasonRow, now: datetime) -> None:
    stamped = dataclasses.replace(row, last_ingested_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    _store_season_row(store, summary, stamped)
    summary.api_calls = client.calls
    summary.quota_remaining = client.quota_remaining
    log.info("ingest summary: %s", json.dumps(summary.as_dict(), sort_keys=True))


def ingest_season(
    store: Store, season: int, client: CbbdClient | None = None,
    *, alpha_cfg: AlphaConfig = DEFAULT_ALPHA, now: Callable[[], datetime] = _now,
) -> IngestSummary:
    """Teams, then every game of the season. Creates the season row (anchor from the data) on
    first run; an existing row keeps its anchor."""
    client = client or CbbdClient()
    summary = IngestSummary(season=season)
    selection_sunday = _selection_sunday(season, summary)

    raw_teams = fetch_teams(client, season)
    summary.teams_fetched = len(raw_teams)
    teams = team_rows(raw_teams, season)
    _upsert(store, summary, "teams", teams, ("id", "season"))
    d1_ids = {t["id"] for t in teams}

    raw_games = fetch_season_games(client, season)
    summary.games_fetched = len(raw_games)

    existing = get_season_row(store, season)
    if existing is None:
        dates = [eastern_date(g["startDate"]) for g in raw_games if g.get("startDate")]
        anchor = compute_anchor_monday(dates)
    else:
        anchor = existing.anchor_monday
    row = SeasonRow(season, anchor, selection_sunday, existing.last_ingested_at if existing else None)
    summary.anchor_monday = anchor.isoformat()
    _store_season_row(store, summary, row)  # persist the anchor before any game uses it

    _store_games(store, summary, raw_games, row, d1_ids, alpha_cfg)
    _finish(store, summary, client, row, now())
    return summary


def ingest_week(
    store: Store, season: int, week: int, client: CbbdClient | None = None,
    *, lookback_days: int = LOOKBACK_DAYS, alpha_cfg: AlphaConfig = DEFAULT_ALPHA,
    now: Callable[[], datetime] = _now,
) -> IngestSummary:
    """Games in the window [week Monday - lookback_days, week Sunday], so late corrections and
    the previous week are refreshed. Teams are fetched only if none are stored for the season."""
    if week < 1:
        raise ValueError(f"week must be >= 1, got {week}")
    client = client or CbbdClient()
    summary = IngestSummary(season=season, week=week)
    selection_sunday = _selection_sunday(season, summary)

    existing = get_season_row(store, season)
    if existing is None:
        raise SeasonNotIngestedError(
            f"season {season} has no seasons row, so its week 1 anchor is unknown: run "
            f"`python -m ingest.run season {season}` once first"
        )
    row = dataclasses.replace(existing, selection_sunday=selection_sunday)
    summary.anchor_monday = row.anchor_monday.isoformat()
    _store_season_row(store, summary, row)  # keeps the anchor; refreshes a corrected Selection Sunday

    d1_ids = stored_team_ids(store, season)
    if not d1_ids:
        raw_teams = fetch_teams(client, season)
        summary.teams_fetched = len(raw_teams)
        teams = team_rows(raw_teams, season)
        _upsert(store, summary, "teams", teams, ("id", "season"))
        d1_ids = {t["id"] for t in teams}

    raw_games = fetch_week_games(client, row, week, lookback_days)
    summary.games_fetched = len(raw_games)
    _store_games(store, summary, raw_games, row, d1_ids, alpha_cfg)
    _finish(store, summary, client, row, now())
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest.run", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("season", help="teams and all games").add_argument("season", type=int)
    week = sub.add_parser("week", help="windowed games for one week")
    week.add_argument("season", type=int)
    week.add_argument("week", type=int)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = get_store()
    summary = ingest_season(store, args.season) if args.mode == "season" else ingest_week(store, args.season, args.week)
    print(json.dumps(summary.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
