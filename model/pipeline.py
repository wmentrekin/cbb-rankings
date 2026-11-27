"""`rate_week`: read a season's data from the store, run the prior-season solve (when it can
matter) and the main solve, and put the result on the published scale. Reads only; the
orchestration writes the ratings.

Two scales, as in football (cfb-rankings @ 40a6b4f, `model/params.py`):
* RAW: what the solve returns. No lower bound, so the bottom runs below zero. Priors are raw: the
  prior season is re-solved from its stored GAMES on every run (never read from the `ratings`
  table) and fed to the main solve unshifted.
* PUBLISHED: the raw ratings plus one constant, so the lowest D-I team reads `display_min`
  (`shift_to_display`, the only place the shift is computed). This is what is stored and published.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from database.store import Store
from ingest.rules import RATED_PLAYED_SQL
from model.assemble import Assembled, assemble
from model.config import BASKETBALL_DEFAULT, ModelConfig, check_week
from model.solve import SolveResult, solve

log = logging.getLogger(__name__)

GAME_COLUMNS = ("id", "home_team_id", "away_team_id", "winner_team_id", "margin", "alpha")
GAMES_SQL = (
    f"SELECT {', '.join(GAME_COLUMNS)} FROM games "
    f"WHERE season = ? AND {RATED_PLAYED_SQL} AND week <= ? ORDER BY id"
)
PRIOR_GAMES_SQL = (
    f"SELECT {', '.join(GAME_COLUMNS)} FROM games "
    f"WHERE season = ? AND {RATED_PLAYED_SQL} ORDER BY id"
)
TEAMS_SQL = "SELECT id FROM teams WHERE season = ? ORDER BY id"


class NoTeamsError(RuntimeError):
    """The season has no D-I teams in the store, so there is nothing to rate."""


def shift_to_display(raw: dict[int, float], config: ModelConfig) -> tuple[dict[int, float], float]:
    """(published ratings, shift): every rating moved by one constant so the lowest team reads
    `config.display_min`; (a copy of `raw`, 0.0) when `display_min` is None or there are no teams.
    A uniform shift changes no gap and no rank. Football's `shift_to_display` also moves its dummy
    "fcs" entry; here the dummy is not a key, and `RatingsResult.display_shift` applies to it."""
    if config.display_min is None or not raw:
        return dict(raw), 0.0
    shift = config.display_min - min(raw.values())
    return {t: value + shift for t, value in raw.items()}, shift


@dataclass(frozen=True)
class RatingsResult:
    season: int
    week: int
    published_ratings: dict[int, float]  # what is stored and published: raw + display_shift
    raw_ratings: dict[int, float]  # the solve (solve.ratings); every D-I team; never the dummy
    display_shift: float  # published - raw, the same for every team
    records: dict[int, tuple[int, int]]  # (wins, losses), including games against non-D-I teams
    solve: SolveResult  # main solve diagnostics (includes the dummy rating, r_lower)
    prior_solve: SolveResult | None  # raw; None when skipped (lambda 0) or no prior-season data
    connectivity: float  # diagnostic only
    n_games_ignored_unplayed: int
    n_games_ignored_no_d1: int
    config: ModelConfig
    seconds: float
    warnings: list[str] = field(default_factory=list)


def _team_ids(store: Store, season: int) -> list[int]:
    _, rows = store.query_rows(TEAMS_SQL, [season])
    return [int(r[0]) for r in rows]


def _game_rows(store: Store, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    columns, rows = store.query_rows(sql, params)
    return [dict(zip(columns, r)) for r in rows]


def rate_week(store: Store, season: int, week: int, config: ModelConfig = BASKETBALL_DEFAULT) -> RatingsResult:
    """Ratings for `season` through `week` (games with `week <= week`)."""
    t0 = time.perf_counter()
    check_week(week)
    warnings: list[str] = []

    team_ids = _team_ids(store, season)
    if not team_ids:
        raise NoTeamsError(f"no teams stored for season {season}")
    data: Assembled = assemble(_game_rows(store, GAMES_SQL, [season, week]), team_ids)
    if not data.games and not data.lower_losses:
        warnings.append(f"season {season} has no rated played games through week {week}")

    lam = config.prior_lambda(week)
    priors: dict[int, float] = {}  # empty = every team gets default_prior; solve() zeroes lambda itself after the fade week
    prior_result: SolveResult | None = None
    if lam > 0:
        prior_teams = _team_ids(store, season - 1)
        prior_data = assemble(_game_rows(store, PRIOR_GAMES_SQL, [season - 1]), prior_teams) if prior_teams else None
        if prior_data is None or not (prior_data.games or prior_data.lower_losses):
            warnings.append(
                f"no prior-season data for {season - 1}: every team gets default_prior {config.default_prior}"
            )
        else:
            prior_result = solve(
                prior_data.games, prior_data.lower_losses, prior_data.records, prior_teams, None, week, config
            )
            priors = prior_result.ratings  # raw, never shifted

    result = solve(data.games, data.lower_losses, data.records, team_ids, priors, week, config)
    allowed = {config.alpha.neutral, config.alpha.home_win, config.alpha.away_win}
    odd = {g.alpha for g in data.games} | {x.alpha for x in data.lower_losses}
    if odd - allowed:
        warnings.append(f"stored alpha values {sorted(odd - allowed)} are not in the configured set {sorted(allowed)}")
    warnings.extend(result.warnings)
    published, shift = shift_to_display(result.ratings, config)
    if prior_result:
        warnings.extend(f"prior solve: {w}" for w in prior_result.warnings)
    return RatingsResult(
        season=season,
        week=week,
        published_ratings=published,
        raw_ratings=result.ratings,
        display_shift=shift,
        records=data.records,
        solve=result,
        prior_solve=prior_result,
        connectivity=data.connectivity,
        n_games_ignored_unplayed=data.n_ignored_unplayed,
        n_games_ignored_no_d1=data.n_ignored_no_d1,
        config=config,
        seconds=time.perf_counter() - t0,
        warnings=warnings,
    )
