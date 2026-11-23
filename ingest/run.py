"""Ingestion entry points: `ingest_season` (full, the scheduled path) and `ingest_week` (windowed).

    CBB_STORE=local uv run python -m ingest.run season 2026
    uv run python -m ingest.run week 2027 3

The weekly job runs `ingest_season` every time: 1 teams call plus about 1 games call per 3,000
games (4 calls for a 6,300-game season). That refreshes the teams, re-derives `is_rated` for
every game and heals anything a missed week left behind. `ingest_week` (1 games call, plus 1
teams call only if the season has no teams stored) is a cheap manual tool.

All API calls happen before the first write, and all writes go through `store.upsert`, so
re-running writes nothing for unchanged rows. `last_ingested_at` is stamped only after
everything succeeded and the gates below passed; a run that did not is reported through
`IngestSummary.ok` / `.failures`, and the CLI exits non-zero printing `INGEST_FAILURE: <reasons>`.

Gates (failures): no teams or no games fetched for a season, or none for a week that should
have games; dropped or unusable rows above a threshold (default: the larger of 5 and 0.5% of
the rows fetched; env `INGEST_MAX_DROPPED` or the `max_dropped` argument overrides it); a
full fetch that is implausibly small for what is stored (absent games are then NOT
neutralized); a week ingest on a season never fully ingested, or after Selection Sunday was
changed in `seasons.json`.

Guards on the stored games of the season (each counted as an anomaly kind and logged):
* a stored game absent from a complete season fetch is neutralized (`is_rated = 0`, status
  `cancelled`, result cleared, row kept): `absent_from_source`.
* two rows with the same unordered team pair and game_date that would both be rated and played
  are a double count: the larger id stays rated, the others get `is_rated = 0`:
  `duplicate_rated_pair`.
* a final with missing or equal scores is stored without a result and with `is_rated = 0`
  (`final_score_missing`, `final_tie`).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable

from database import get_store
from database.store import Store, WriteStats
from ingest.derive import (
    ABSENT_FROM_SOURCE,
    DEFAULT_ALPHA,
    DUPLICATE_RATED_PAIR,
    FINAL_SCORE_MISSING,
    FINAL_TIE,
    MALFORMED_ROW,
    UNKNOWN_SEASON_TYPE,
    UNKNOWN_STATUS,
    AlphaConfig,
    derive_game,
    eastern_date,
)
from ingest.games import LOOKBACK_DAYS, fetch_season_games, fetch_week_games, season_window
from ingest.http import CbbdClient, quota_floor_from_env
from ingest.overrides import OVERRIDE_FAILURE_MARKER, OverrideFileError, OverrideSummary, apply_overrides, load_overrides
from ingest.rules import RATED_PLAYED_SQL, is_rated_played
from ingest.teams import fetch_teams, stored_team_ids, team_rows
from ingest.weeks import (
    AnchorError,
    SeasonRow,
    compute_anchor_monday,
    get_season_row,
    is_provisional,
    last_week,
    load_selection_sunday,
    season_row_dict,
    week_for_date,
)

log = logging.getLogger(__name__)

INGEST_FAILURE_MARKER = "INGEST_FAILURE"
MAX_DROPPED_ENV = "INGEST_MAX_DROPPED"
MIN_DROP_ALLOWANCE = 5
DROP_FRACTION = 0.005
# A complete season fetch holds every stored game of its window, scheduled ones included, so
# one with fewer rows than this share of them is a bad fetch, not a purge.
MIN_PLAUSIBLE_FRACTION = 0.9
DROPPED_KINDS = (MALFORMED_ROW, UNKNOWN_STATUS, UNKNOWN_SEASON_TYPE)
MAX_IDS_PER_KIND = 20


class SeasonNotIngestedError(RuntimeError):
    """A weekly update needs the season row (and its anchor) created by a full `ingest_season`."""


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
    retried_writes: int = 0  # writes re-sent after a timeout; rows_written may be under-counted
    dropped_both_non_d1: int = 0
    anomalies: dict[str, int] = field(default_factory=dict)
    anomaly_ids: dict[str, list[int]] = field(default_factory=dict)  # first MAX_IDS_PER_KIND game ids per kind
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    anchor_monday: str | None = None
    selection_sunday: str | None = None

    @property
    def ok(self) -> bool:
        """False when any gate failed; the season was then NOT stamped as ingested."""
        return not self.failures

    @property
    def total_rows_written(self) -> int:
        return sum(self.rows_written.values())

    def as_dict(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "ok": self.ok, "total_rows_written": self.total_rows_written}


def failure_line(failures: list[str]) -> str:
    """The marker line CI and the orchestration grep for."""
    return f"{INGEST_FAILURE_MARKER}: " + "; ".join(failures)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _upsert(store: Store, summary: IngestSummary, table: str, rows: list[dict], key: tuple[str, ...]) -> None:
    stats: WriteStats = store.upsert(table, rows, key)
    summary.rows_upserted[table] = summary.rows_upserted.get(table, 0) + len(rows)
    summary.rows_written[table] = summary.rows_written.get(table, 0) + stats.rows_written
    summary.retried_writes += stats.retried_writes


def _anomaly(summary: IngestSummary, code: str, game_ids: list[int]) -> None:
    summary.anomalies[code] = summary.anomalies.get(code, 0) + len(game_ids)
    ids = summary.anomaly_ids.setdefault(code, [])
    ids.extend(game_ids[: max(0, MAX_IDS_PER_KIND - len(ids))])


def _selection_sunday(season: int, summary: IngestSummary) -> date:
    selection_sunday = load_selection_sunday(season)
    if is_provisional(season):
        summary.warnings.append(f"Selection Sunday {selection_sunday} for season {season} is provisional")
    summary.selection_sunday = selection_sunday.isoformat()
    return selection_sunday


def max_dropped(games_fetched: int, override: int | None = None) -> int:
    """How many dropped or unusable rows a run tolerates before it fails."""
    if override is not None:
        return override
    raw = os.environ.get(MAX_DROPPED_ENV, "").strip()
    if raw:
        if not raw.isdigit():
            raise ValueError(f"{MAX_DROPPED_ENV} must be a non-negative integer, got {raw!r}")
        return int(raw)
    return max(MIN_DROP_ALLOWANCE, math.ceil(DROP_FRACTION * games_fetched))


def _check_dropped(summary: IngestSummary, limit: int | None) -> None:
    dropped = summary.dropped_both_non_d1 + sum(summary.anomalies.get(k, 0) for k in DROPPED_KINDS)
    allowed = max_dropped(summary.games_fetched, limit)
    if dropped > allowed:
        summary.failures.append(
            f"{dropped} of {summary.games_fetched} fetched games were dropped or malformed "
            f"(both sides non-D-I, {', '.join(DROPPED_KINDS)}), above the allowed {allowed}"
        )
    unplayed = sum(summary.anomalies.get(k, 0) for k in (FINAL_SCORE_MISSING, FINAL_TIE))
    if unplayed > allowed:
        summary.warnings.append(f"{unplayed} finals were stored without a result and unrated (allowed {allowed} before this warning)")


def _derive_rows(
    summary: IngestSummary, raw_games: list[dict], season_row: SeasonRow, d1_ids: set[int], alpha_cfg: AlphaConfig,
) -> list[dict]:
    rows: list[dict] = []
    by_code: dict[str, list[int]] = defaultdict(list)
    for raw in raw_games:
        outcome = derive_game(
            raw, season=season_row.season, d1_ids=d1_ids, selection_sunday=season_row.selection_sunday,
            week_of=lambda d: week_for_date(season_row, d), alpha_cfg=alpha_cfg,
        )
        for message in outcome.messages:
            log.warning(message)
        for code, n in outcome.anomalies.items():
            by_code[code] += [raw.get("id")] * n
        summary.dropped_both_non_d1 += outcome.dropped_both_non_d1
        if outcome.row:
            rows.append(outcome.row)
    if summary.dropped_both_non_d1:
        log.info("dropped %d games with both sides non-D-I", summary.dropped_both_non_d1)
    for code, ids in by_code.items():
        _anomaly(summary, code, ids)
    return rows


def _stored_games(store: Store, season: int, rated_played_only: bool) -> dict[int, dict]:
    where = f" AND {RATED_PLAYED_SQL}" if rated_played_only else ""
    return {r["id"]: r for r in store.query(f"SELECT * FROM games WHERE season = ?{where}", [season])}


def _is_neutralized(row: dict) -> bool:
    return row["status"] == "cancelled" and not row["is_rated"] and row["winner_team_id"] is None


def _neutralize(row: dict) -> dict:
    """Out of every rating and record, row kept: unrated, cancelled, no result."""
    return {**row, "is_rated": 0, "status": "cancelled", "home_score": None, "away_score": None,
            "margin": None, "winner_team_id": None, "alpha": None}


def _in_window(row: dict, season: int) -> bool:
    start, end = season_window(season)
    return start <= datetime.fromisoformat(row["start_date"]) <= end


def _absent_games(summary: IngestSummary, stored: dict[int, dict], raw_games: list[dict], season: int) -> list[dict]:
    """Neutralized copies of stored games a complete season fetch no longer lists. Returns []
    (and records a failure) when the fetch is implausibly small, and never acts on an empty one."""
    fetched = {g["id"] for g in raw_games if g.get("id") is not None}
    in_window = {i: r for i, r in stored.items() if _in_window(r, season)}
    if in_window and len(fetched) < MIN_PLAUSIBLE_FRACTION * len(in_window):
        summary.failures.append(
            f"the season fetch returned {len(fetched)} games but {len(in_window)} are stored for it "
            f"(under {MIN_PLAUSIBLE_FRACTION:.0%}); absent games were NOT neutralized"
        )
        return []
    absent = [r for i, r in sorted(in_window.items()) if i not in fetched and not _is_neutralized(r)]
    if absent:
        ids = [r["id"] for r in absent]
        _anomaly(summary, ABSENT_FROM_SOURCE, ids)
        log.warning("%s: %d stored games are absent from the source and were neutralized: %s",
                    ABSENT_FROM_SOURCE, len(ids), ids)
    return [_neutralize(r) for r in absent]


def _duplicate_losers(games: dict[int, dict]) -> list[int]:
    """Ids to unrate: of rated, played rows with the same unordered team pair and game_date, all
    but the largest id (every candidate is already final with a valid result)."""
    groups: dict[tuple, list[int]] = defaultdict(list)
    for g in games.values():
        if is_rated_played(g):
            groups[(tuple(sorted((g["home_team_id"], g["away_team_id"]))), g["game_date"])].append(g["id"])
    return sorted(i for ids in groups.values() if len(ids) > 1 for i in ids if i != max(ids))


def _store_games(
    store: Store, summary: IngestSummary, raw_games: list[dict], season_row: SeasonRow,
    d1_ids: set[int], alpha_cfg: AlphaConfig, *, full_season: bool, limit: int | None,
) -> None:
    season = season_row.season
    rows = _derive_rows(summary, raw_games, season_row, d1_ids, alpha_cfg)
    stored = _stored_games(store, season, rated_played_only=not full_season)
    neutralized = _absent_games(summary, stored, raw_games, season) if full_season else []

    # The state after this run's writes, so the duplicate check sees what will be stored.
    after = {**stored, **{r["id"]: r for r in neutralized}, **{r["id"]: r for r in rows}}
    losers = _duplicate_losers(after)
    if losers:
        _anomaly(summary, DUPLICATE_RATED_PAIR, losers)
        log.warning("%s: %d rated games repeat another game's teams and date and were unrated: %s",
                    DUPLICATE_RATED_PAIR, len(losers), losers)
    pending = {r["id"]: r for r in neutralized}
    pending.update({r["id"]: r for r in rows})
    for game_id in losers:
        pending[game_id] = {**pending.get(game_id, after[game_id]), "is_rated": 0}

    _upsert(store, summary, "games", [pending[i] for i in sorted(pending)], ("id",))
    _check_dropped(summary, limit)


def _close(summary: IngestSummary, client: Any) -> None:
    summary.api_calls = client.calls
    summary.quota_remaining = client.quota_remaining
    floor = getattr(client, "quota_floor", None)
    floor = quota_floor_from_env() if floor is None else floor
    if summary.quota_remaining is not None and summary.quota_remaining < floor:
        summary.warnings.append(
            f"API quota remaining {summary.quota_remaining} is below the floor {floor}: the next run will abort"
        )
    if summary.retried_writes:
        summary.warnings.append(
            f"{summary.retried_writes} write(s) were retried after a timeout; rows_written may be under-counted"
        )
    if not summary.ok:
        log.error(failure_line(summary.failures))
    log.info("ingest summary: %s", json.dumps(summary.as_dict(), sort_keys=True))


def _stamp(store: Store, summary: IngestSummary, row: SeasonRow, now: datetime) -> None:
    """Only a run that passed every gate marks the season as ingested."""
    if summary.ok:
        stamped = dataclasses.replace(row, last_ingested_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        _upsert(store, summary, "seasons", [season_row_dict(stamped)], ("season",))


def ingest_season(
    store: Store, season: int, client: CbbdClient | None = None,
    *, alpha_cfg: AlphaConfig = DEFAULT_ALPHA, now: Callable[[], datetime] = _now,
    max_dropped_rows: int | None = None,
) -> IngestSummary:
    """Teams, then every game of the season: the scheduled path. Creates the season row (anchor
    from the data) on first run; an existing row keeps its anchor.

    Order: every API call first, then teams, the season row, games (with the absent and
    duplicate guards), and last the `last_ingested_at` stamp, only if `summary.ok`. Nothing is
    written when no teams or no games came back."""
    client = client or CbbdClient()
    summary = IngestSummary(season=season)
    selection_sunday = _selection_sunday(season, summary)
    existing = get_season_row(store, season)
    if existing and existing.selection_sunday != selection_sunday:
        summary.warnings.append(
            f"Selection Sunday for season {season} changed from {existing.selection_sunday} to {selection_sunday}: "
            "this full-season ingest re-derives is_rated for every game, so the season is re-rated"
        )

    raw_teams = fetch_teams(client, season)
    summary.teams_fetched = len(raw_teams)
    raw_games = fetch_season_games(client, season) if raw_teams else []
    summary.games_fetched = len(raw_games)
    if not raw_teams:
        summary.failures.append(f"/teams returned no teams for season {season}")
    elif not raw_games:
        summary.failures.append(f"/games returned no games for season {season}")
    if not summary.ok:
        _close(summary, client)
        return summary

    if existing is None:
        try:
            anchor = compute_anchor_monday([eastern_date(g["startDate"]) for g in raw_games if g.get("startDate")])
        except (AnchorError, ValueError) as exc:
            summary.failures.append(f"cannot create the season row: {exc}")
            _close(summary, client)
            return summary
    else:
        anchor = existing.anchor_monday
    row = SeasonRow(season, anchor, selection_sunday, existing.last_ingested_at if existing else None)
    summary.anchor_monday = anchor.isoformat()

    teams = team_rows(raw_teams, season)
    stored_teams = len(stored_team_ids(store, season))
    if stored_teams and stored_teams != len(teams):
        summary.warnings.append(
            f"season {season} has {len(teams)} D-I teams in the source but {stored_teams} stored; stored teams "
            "are kept (a team vanishing mid-season is suspicious)"
        )
    _upsert(store, summary, "teams", teams, ("id", "season"))
    _upsert(store, summary, "seasons", [season_row_dict(row)], ("season",))  # persist the anchor before games use it
    _store_games(store, summary, raw_games, row, {t["id"] for t in teams}, alpha_cfg, full_season=True, limit=max_dropped_rows)
    _stamp(store, summary, row, now())
    _close(summary, client)
    return summary


def ingest_week(
    store: Store, season: int, week: int, client: CbbdClient | None = None,
    *, lookback_days: int = LOOKBACK_DAYS, alpha_cfg: AlphaConfig = DEFAULT_ALPHA,
    now: Callable[[], datetime] = _now, max_dropped_rows: int | None = None,
) -> IngestSummary:
    """Manual, cheap: games in the window [week Monday - lookback_days, week Sunday], so late
    corrections and the previous week are refreshed. Teams are fetched only if none are stored.
    The scheduled path is `ingest_season`.

    Raises `SeasonNotIngestedError` (no API call) when the season row is missing or was never
    fully ingested. Selection Sunday changed in seasons.json fails the run: a window cannot
    re-rate the rest of the season, run `ingest_season`."""
    if week < 1:
        raise ValueError(f"week must be >= 1, got {week}")
    client = client or CbbdClient()
    summary = IngestSummary(season=season, week=week)
    selection_sunday = _selection_sunday(season, summary)

    row = get_season_row(store, season)
    if row is None:
        raise SeasonNotIngestedError(
            f"season {season} has no seasons row, so its week 1 anchor is unknown: run "
            f"`python -m ingest.run season {season}` once first"
        )
    if row.last_ingested_at is None:
        raise SeasonNotIngestedError(
            f"season {season} was never fully ingested (last_ingested_at is empty): run "
            f"`python -m ingest.run season {season}` first"
        )
    summary.anchor_monday = row.anchor_monday.isoformat()
    if row.selection_sunday != selection_sunday:
        summary.failures.append(
            f"Selection Sunday changed from {row.selection_sunday} to {selection_sunday}: a week ingest cannot re-rate "
            f"the whole season; run `python -m ingest.run season {season}`"
        )
        _close(summary, client)
        return summary

    d1_ids = stored_team_ids(store, season)
    raw_teams: list[dict] = []
    if not d1_ids:
        raw_teams = fetch_teams(client, season)
        summary.teams_fetched = len(raw_teams)
        if not raw_teams:
            summary.failures.append(f"/teams returned no teams for season {season}")
            _close(summary, client)
            return summary
    raw_games = fetch_week_games(client, row, week, lookback_days)
    summary.games_fetched = len(raw_games)
    if not raw_games:
        message = f"/games returned no games for season {season} week {week}"
        if week <= last_week(row):
            summary.failures.append(message)
            _close(summary, client)
            return summary
        summary.warnings.append(message + " (after the last rated week)")

    if raw_teams:
        teams = team_rows(raw_teams, season)
        _upsert(store, summary, "teams", teams, ("id", "season"))
        d1_ids = {t["id"] for t in teams}
    _store_games(store, summary, raw_games, row, d1_ids, alpha_cfg, full_season=False, limit=max_dropped_rows)
    _stamp(store, summary, row, now())
    _close(summary, client)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest.run", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("season", help="teams and all games (the scheduled path)").add_argument("season", type=int)
    week = sub.add_parser("week", help="windowed games for one week (manual)")
    week.add_argument("season", type=int)
    week.add_argument("week", type=int)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        load_overrides()  # a broken file fails before any API call is spent
    except OverrideFileError as exc:
        print(f"{OVERRIDE_FAILURE_MARKER}: {exc}")
        return 1
    store = get_store()
    try:
        summary = ingest_season(store, args.season) if args.mode == "season" else ingest_week(store, args.season, args.week)
    except Exception as exc:
        log.exception("ingest failed")
        print(failure_line([f"{type(exc).__name__}: {exc}"]))
        return 1
    # The ingest rewrote every overridden game from the source: restore the corrections now.
    overrides: OverrideSummary = apply_overrides(store, args.season)
    print(json.dumps({**summary.as_dict(), "overrides": overrides.as_dict()}, indent=2, sort_keys=True))
    if not summary.ok:
        print(failure_line(summary.failures))
    return 0 if summary.ok and overrides.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
