"""The seasons table and week logic, implemented once for every task (ingest, model, run).

Weeks are Monday-Sunday buckets of US/Eastern game dates counted from a stored per-season
anchor Monday. Week 1 starts on the anchor; games dated before it are folded into week 1.

Anchor rule (`compute_anchor_monday`): the opening day is the earliest US/Eastern game date
with at least `OPENING_MIN_GAMES` (20) games; the anchor is the Monday on or before it. A few
stray early games (2026-27 opened with two games on Sunday 2026-11-01 while 182 were played on
Monday 2026-11-02) therefore do not move it: 2025-26 gives 2025-11-03, 2026-27 gives 2026-11-02.
The anchor is computed once, when the season row is first created, and stored; an existing
row's anchor is never recomputed, so reruns cannot renumber weeks.

Selection Sunday is not in the API. It is entered by hand in `ingest/seasons.json`; a season
marked provisional there produces a warning on every use until confirmed.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from database.store import Store
from ingest.derive import EASTERN

log = logging.getLogger(__name__)

SEASONS_FILE = Path(__file__).resolve().parent / "seasons.json"
OPENING_MIN_GAMES = 20


class SeasonConfigError(RuntimeError):
    """The season is missing from ingest/seasons.json, or the file is malformed."""


class AnchorError(RuntimeError):
    """No day of the data has enough games to be the season's opening day yet."""


@dataclass(frozen=True)
class SeasonRow:
    season: int  # the year the season ends: 2025-26 is 2026
    anchor_monday: date
    selection_sunday: date
    last_ingested_at: str | None = None

    def __post_init__(self):
        if self.anchor_monday.weekday() != 0:
            raise ValueError(f"anchor_monday {self.anchor_monday} is not a Monday")


def today_et(now: datetime | None = None) -> date:
    """Today's US/Eastern calendar date."""
    return (now or datetime.now(EASTERN)).astimezone(EASTERN).date()


def season_for_date(d: date) -> int:
    """The season a date belongs to: August through December count toward the season ending
    next year, January through July toward the one ending this year."""
    return d.year + 1 if d.month >= 8 else d.year


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def compute_anchor_monday(game_dates: Iterable[date], min_games: int = OPENING_MIN_GAMES) -> date:
    counts = Counter(game_dates)
    busy = sorted(d for d, n in counts.items() if n >= min_games)
    if not busy:
        raise AnchorError(
            f"no date has {min_games}+ games, so the opening day is not known yet "
            f"({sum(counts.values())} games over {len(counts)} dates)"
        )
    return monday_of(busy[0])


def load_selection_sunday(season: int, path: Path = SEASONS_FILE) -> date:
    """Selection Sunday from the hand-entered file; warns while it is provisional."""
    entries = json.loads(path.read_text())
    entry = entries.get(str(season))
    if entry is None:
        raise SeasonConfigError(
            f"season {season} is not in {path.name}: add "
            f'"{season}": {{"selection_sunday": "YYYY-MM-DD", "provisional": false}} '
            "(Selection Sunday is not available from the API)"
        )
    try:
        selection_sunday = date.fromisoformat(entry["selection_sunday"])
    except (KeyError, ValueError) as exc:
        raise SeasonConfigError(f"season {season} in {path.name} has no valid selection_sunday") from exc
    if entry.get("provisional"):
        log.warning(
            "season %s Selection Sunday %s is PROVISIONAL (unconfirmed); is_rated and the last "
            "published week depend on it. Confirm it and set provisional=false in %s",
            season, selection_sunday, path.name,
        )
    return selection_sunday


def is_provisional(season: int, path: Path = SEASONS_FILE) -> bool:
    return bool(json.loads(path.read_text()).get(str(season), {}).get("provisional"))


def get_season_row(store: Store, season: int) -> SeasonRow | None:
    rows = store.query(
        "SELECT season, anchor_monday, selection_sunday, last_ingested_at FROM seasons WHERE season = ?",
        [season],
    )
    if not rows:
        return None
    r = rows[0]
    return SeasonRow(
        season=int(r["season"]),
        anchor_monday=date.fromisoformat(r["anchor_monday"]),
        selection_sunday=date.fromisoformat(r["selection_sunday"]),
        last_ingested_at=r["last_ingested_at"],
    )


def season_row_dict(row: SeasonRow) -> dict:
    return {
        "season": row.season,
        "anchor_monday": row.anchor_monday.isoformat(),
        "selection_sunday": row.selection_sunday.isoformat(),
        "last_ingested_at": row.last_ingested_at,
    }


def week_for_date(season_row: SeasonRow, game_date: date) -> int:
    """Week number of a US/Eastern date; dates before the anchor fold into week 1."""
    return max(1, (game_date - season_row.anchor_monday).days // 7 + 1)


def week_bounds(season_row: SeasonRow, week: int) -> tuple[date, date]:
    """(Monday, Sunday) of a week."""
    if week < 1:
        raise ValueError(f"week must be >= 1, got {week}")
    monday = season_row.anchor_monday + timedelta(days=7 * (week - 1))
    return monday, monday + timedelta(days=6)


def last_week(season_row: SeasonRow) -> int:
    """The week containing Selection Sunday: the last one that gets a snapshot."""
    return week_for_date(season_row, season_row.selection_sunday)


def resolve_week(season_row: SeasonRow, today: date) -> int | None:
    """The week to publish on `today` (a US/Eastern date): the one that ended on the Sunday
    before the current Monday-Sunday week, so a Monday run publishes the week that ended
    yesterday. None before week 1 has ended and after the last week has been published."""
    ended = (monday_of(today) - season_row.anchor_monday).days // 7
    if ended < 1 or ended > last_week(season_row):
        return None
    return ended
