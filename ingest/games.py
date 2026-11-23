"""Games from `/games`, fetched by date range.

The endpoint returns at most 3,000 rows per call, oldest first, and filters on inclusive
`startDateRange` / `endDateRange` (probe, sections 7 and 9). A season is therefore fetched in
pages: after a full page the next request starts at the latest start time seen (inclusive, so
rows sharing that timestamp are not lost) and rows are de-duplicated by game id.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ingest.weeks import SeasonRow, week_bounds

log = logging.getLogger(__name__)

GAMES_CAP = 3000
LOOKBACK_DAYS = 10  # weekly window starts this many days before the week's Monday


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc(d: date, hour: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc)


def fetch_games_range(client, season: int, start: datetime, end: datetime, cap: int = GAMES_CAP) -> list[dict[str, Any]]:
    """All games with start time in [start, end], de-duplicated by id, oldest first.

    One call per page; a range holding fewer than `cap` games takes one call.
    """
    games: dict[int, dict[str, Any]] = {}
    cursor = start
    while True:
        page = client.get(
            "/games",
            {"season": season, "startDateRange": _iso(cursor), "endDateRange": _iso(end)},
        )
        for game in page:
            games.setdefault(game["id"], game)
        if len(page) < cap:
            break
        latest = max(
            (datetime.fromisoformat(g["startDate"]) for g in page if g.get("startDate")), default=None
        )
        if latest is None or latest.replace(microsecond=0) <= cursor:
            raise RuntimeError(
                f"/games returned {len(page)} rows (the cap) without advancing past {_iso(cursor)}; "
                "narrow the date range"
            )
        cursor = latest.replace(microsecond=0)
        log.info("/games page full (%d rows); continuing from %s", len(page), _iso(cursor))
    return list(games.values())


def fetch_season_games(client, season: int, cap: int = GAMES_CAP) -> list[dict[str, Any]]:
    """A whole season: October 1 of the previous year through May 31 (the NCAA final is in early
    April). 4 calls in total with the teams call for a 6,300-game season."""
    return fetch_games_range(client, season, _utc(date(season - 1, 10, 1)), _utc(date(season, 5, 31), 23), cap)


def week_window(season_row: SeasonRow, week: int, lookback_days: int = LOOKBACK_DAYS) -> tuple[datetime, datetime]:
    """UTC range covering `lookback_days` before the week's Monday through its Sunday (ET).

    The end is 06:00 UTC the next day, after the end of Sunday in US/Eastern (04:00-05:00 UTC).
    Extra rows at the edges are harmless: the upsert is idempotent.
    """
    monday, sunday = week_bounds(season_row, week)
    return _utc(monday - timedelta(days=lookback_days)), _utc(sunday + timedelta(days=1), 6)


def fetch_week_games(client, season_row: SeasonRow, week: int, lookback_days: int = LOOKBACK_DAYS, cap: int = GAMES_CAP):
    start, end = week_window(season_row, week, lookback_days)
    return fetch_games_range(client, season_row.season, start, end, cap)
