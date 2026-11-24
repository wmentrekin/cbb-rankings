"""The weekly run: ingest -> overrides -> model -> write ratings -> publish to R2.

    uv run python main.py                                    # the Monday run (cron): today's week
    uv run python main.py --season 2026 --week 12            # one explicit week
    uv run python main.py --season 2026 --week 3 --staging   # writes ratings_staging, publishes nothing
    uv run python main.py --season 2026 --all-weeks --no-ingest --dry-run   # ratings backfill plan

Default target: season = `season_for_date(today_et())`, week = `resolve_week(season_row, today)`
(the week that ended yesterday). `--season` / `--week` override either. A Monday with nothing to
publish (outside the season, before week 1 has ended, after the week containing Selection Sunday
was published, a season missing from ingest/seasons.json) prints `WEEKLY_NOOP: <code>: <why>`,
exits 0 and spends no API call. The one exception: a brand-new season has no `seasons` row until
its first ingest, so a Monday inside the season window (Nov 1 through the Sunday after Selection
Sunday) with no row ingests first (4 API calls) and then resolves the week.

Steps of one run, each stopping the run with its marker and a non-zero exit (nothing later runs):

    ingest_season (4 API calls)           INGEST_FAILURE     summary.ok false, fetch error, quota floor
    apply_overrides                       OVERRIDE_FAILURE   a known-wrong game must not be rated
    prior season present (weeks 1-6)      PRIOR_FAILURE      back-fill season S-1 first, or --allow-missing-prior
    rate_week                             MODEL_FAILURE      ModelSolveError / NoTeamsError
    write ratings (or ratings_staging)    WRITE_FAILURE
    publish artifacts to R2               PUBLISH_FAILURE    ratings stay written, nothing is swallowed
    anything unexpected                   WEEKLY_FAILURE

then the summary and `WEEKLY_OK season=S week=W`. The deploy hook is the workflow's job, after a
run that printed WEEKLY_OK.

Ratings-only mode (`--weeks A-B` or `--all-weeks`, season required) runs the same steps for each
week in order after ONE ingest, or after none with `--no-ingest` (data already stored by the
backfill-games job). Weeks must have ended. `--dry-run` (needs `--no-ingest`) rates, compares
with what is stored and prints what would be written and published, writing and publishing
nothing. Reruns are idempotent: unchanged ratings rows write nothing; artifacts are re-uploaded
(the week file differs only in `generated_at_utc`).

Staging: `--staging` writes `ratings_staging` and publishes nothing, unless `--publish-staging`,
which publishes under `cbb-staging/` from `ratings_staging`. A staging run never touches the
production prefix. Its ingest still writes `games` and `teams` for real.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from artifacts.r2 import DEFAULT_PREFIX, STAGING_PREFIX, PublishError, RankingsPublisher, r2_from_env
from artifacts.rankings import build_payload, check_table
from database import get_store
from database.store import Store, WriteStats
from ingest.http import CbbdClient
from ingest.overrides import OVERRIDE_FAILURE_MARKER, OverrideFileError, OverrideSummary, apply_overrides
from ingest.planning import PlanningStore
from ingest.rules import RATED_PLAYED_SQL
from ingest.run import IngestSummary, ingest_season
from ingest.weeks import (
    SeasonConfigError,
    SeasonRow,
    get_season_row,
    last_week,
    load_selection_sunday,
    monday_of,
    resolve_week,
    season_for_date,
    today_et,
)
from model.config import BASKETBALL_DEFAULT
from model.pipeline import NoTeamsError, RatingsResult, rate_week
from model.solve import ModelSolveError

log = logging.getLogger("weekly")

NOOP_MARKER = "WEEKLY_NOOP"
OK_MARKER = "WEEKLY_OK"
DRY_RUN_MARKER = "WEEKLY_DRY_RUN"
FAILURE_MARKER = "WEEKLY_FAILURE"  # usage problems and anything unexpected
PRIOR_FAILURE_MARKER = "PRIOR_FAILURE"
MODEL_FAILURE_MARKER = "MODEL_FAILURE"
WRITE_FAILURE_MARKER = "WRITE_FAILURE"
PUBLISH_FAILURE_MARKER = "PUBLISH_FAILURE"
INGEST_FAILURE_MARKER = "INGEST_FAILURE"

RATING_DECIMALS = 6  # stored precision: solver noise below this must not rewrite D1 rows
FLOOR_TOLERANCE = 1e-6
RATINGS_KEY = ("team_id", "season", "week")
# One JSON parameter carries the integers (D1's REST API would turn bound integers into REALs).
DELETE_STALE_SQL = (
    "DELETE FROM {table} WHERE season = json_extract(?1, '$.season') AND week = json_extract(?1, '$.week') "
    "AND team_id NOT IN (SELECT value FROM json_each(?1, '$.keep'))"
)
WINDOW_START = (11, 1)  # a season's Mondays begin on Nov 1 of the year before it ends


class RunFailure(Exception):
    """A step failed: print `marker: message` and exit with `code`."""

    def __init__(self, marker: str, message: str, code: int = 1):
        super().__init__(message)
        self.marker, self.message, self.code = marker, message, code


class RunNoop(Exception):
    """Nothing to do today."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class Options:
    season: int | None = None
    week: int | None = None
    weeks: tuple[int, int] | None = None
    all_weeks: bool = False
    no_ingest: bool = False
    staging: bool = False
    publish_staging: bool = False
    dry_run: bool = False
    allow_missing_prior: bool = False

    @property
    def range_mode(self) -> bool:
        return self.weeks is not None or self.all_weeks

    @property
    def table(self) -> str:
        return "ratings_staging" if self.staging else "ratings"

    @property
    def prefix(self) -> str:
        return STAGING_PREFIX if self.staging else DEFAULT_PREFIX

    @property
    def publishes(self) -> bool:
        return not self.dry_run and (not self.staging or self.publish_staging)


def season_window(selection_sunday: date, season: int) -> tuple[date, date]:
    """Dates on which a Monday run can have something to do for `season`: from Nov 1 of the year
    before it ends through the Sunday after Selection Sunday (the Monday after Selection Sunday
    publishes the last week)."""
    return date(season - 1, *WINDOW_START), selection_sunday + timedelta(days=7)


def weeks_ended(row: SeasonRow, today: date) -> int:
    """How many weeks of the season have ended by `today` (the basis of `resolve_week`)."""
    return max(0, (monday_of(today) - row.anchor_monday).days // 7)


def prior_season_available(store: Store, season: int) -> bool:
    """Whether `season` has teams and rated played games to build priors from."""
    teams = store.query("SELECT COUNT(*) AS n FROM teams WHERE season = ?", [season])[0]["n"]
    games = store.query(f"SELECT COUNT(*) AS n FROM games WHERE season = ? AND {RATED_PLAYED_SQL}", [season])[0]["n"]
    return bool(teams) and bool(games)


def ratings_rows(result: RatingsResult) -> list[dict[str, Any]]:
    rows = []
    for team_id in sorted(result.ratings):
        wins, losses = result.records.get(team_id, (0, 0))
        rows.append({
            "team_id": int(team_id), "season": result.season, "week": result.week,
            "rating": round(float(result.ratings[team_id]), RATING_DECIMALS),
            "wins": int(wins), "losses": int(losses),
        })
    return rows


def write_ratings(store: Store, table: str, season: int, week: int, rows: list[dict[str, Any]]) -> WriteStats:
    """Upsert the week's rows, then delete rows of that (season, week) whose team is not in `rows`."""
    check_table(table)
    stats = store.upsert(table, rows, RATINGS_KEY)
    keep = json.dumps({"season": season, "week": week, "keep": [r["team_id"] for r in rows]}, separators=(",", ":"))
    return stats + store.execute(DELETE_STALE_SQL.format(table=table), [keep])


def plan_ratings_write(store: Store, table: str, season: int, week: int, rows: list[dict[str, Any]]) -> dict[str, int]:
    """What `write_ratings` would do, from the stored rows; nothing is written."""
    check_table(table)
    stored = {
        r["team_id"]: r for r in store.query(
            f"SELECT team_id, rating, wins, losses FROM {table} WHERE season = ? AND week = ?", [season, week]
        )
    }
    new = changed = unchanged = 0
    for row in rows:
        before = stored.get(row["team_id"])
        if before is None:
            new += 1
        elif (before["rating"], before["wins"], before["losses"]) != (row["rating"], row["wins"], row["losses"]):
            changed += 1
        else:
            unchanged += 1
    stale = len(set(stored) - {r["team_id"] for r in rows})
    return {"new": new, "changed": changed, "unchanged": unchanged, "stale": stale, "rows_written": new + changed + stale}


def _lines(items: list[tuple[str, Any]], out: Callable[[str], None]) -> None:
    for key, value in items:
        out(f"  {key}: {value}")


def _ingest(store: Store, season: int, api: Any, out: Callable[[str], None]) -> tuple[IngestSummary, OverrideSummary]:
    try:
        summary = ingest_season(store, season, api() if callable(api) else api)
    except Exception as exc:
        log.exception("ingest failed")
        raise RunFailure(INGEST_FAILURE_MARKER, f"{type(exc).__name__}: {exc}") from exc
    if not summary.ok:
        raise RunFailure(INGEST_FAILURE_MARKER, "; ".join(summary.failures))
    return summary, _overrides(store, season, out)


def _overrides(store: Store, season: int, out: Callable[[str], None]) -> OverrideSummary:
    try:
        summary = apply_overrides(store, season)
    except OverrideFileError as exc:
        raise RunFailure(OVERRIDE_FAILURE_MARKER, str(exc)) from exc
    if not summary.ok:
        detail = "; ".join(f"game_id={f['game_id']} reason={f['reason']}" for f in summary.failed)
        raise RunFailure(
            OVERRIDE_FAILURE_MARKER,
            f"{len(summary.failed)} override(s) failed, so a known-wrong game would be rated; stopping before the model: {detail}",
        )
    return summary


def _target_weeks(opts: Options, row: SeasonRow, today: date) -> list[int]:
    """The weeks to run, validated: from 1 to the last week that has ended (and not past the one
    containing Selection Sunday)."""
    top = min(last_week(row), weeks_ended(row, today))
    if opts.range_mode:
        low, high = (1, top) if opts.all_weeks else opts.weeks  # type: ignore[misc]
        if top < 1:
            raise RunFailure(FAILURE_MARKER, f"season {row.season}: no week has ended by {today}", 2)
    elif opts.week is not None:
        low = high = opts.week
    else:
        week = resolve_week(row, today)
        if week is None:
            raise RunNoop(
                "no_week_to_publish",
                f"season {row.season} has no week to publish on {today} (week 1 has not ended, or the week "
                f"containing Selection Sunday {row.selection_sunday} was already published)",
            )
        return [week]
    if not 1 <= low <= high <= top:
        raise RunFailure(
            FAILURE_MARKER,
            f"weeks {low}-{high} are not publishable for season {row.season} on {today}: "
            f"weeks 1-{top} have ended and are on or before the Selection Sunday week ({last_week(row)})",
            2,
        )
    return list(range(low, high + 1))


def _publish(store: Store, opts: Options, season: int, week: int, r2: Any, now: datetime):
    payload = build_payload(store, season, week, table=opts.table, now=now)
    client, bucket = r2
    return RankingsPublisher(client, bucket, opts.prefix).publish(payload, now), payload


def _run_week(
    store: Store, opts: Options, season: int, week: int, r2: Any, now: Callable[[], datetime],
    out: Callable[[str], None],
) -> int:
    """Steps e-h for one week; returns the ratings rows written."""
    started = time.perf_counter()
    try:
        result = rate_week(store, season, week)
    except (ModelSolveError, NoTeamsError) as exc:
        raise RunFailure(MODEL_FAILURE_MARKER, f"season {season} week {week}: {type(exc).__name__}: {exc}") from exc
    rows = ratings_rows(result)
    solve = result.solve
    floor = sum(1 for r in result.ratings.values() if r <= result.config.r_min + FLOOR_TOLERANCE)

    plan = None
    stats = WriteStats()
    if opts.dry_run:
        plan = plan_ratings_write(store, opts.table, season, week, rows)
    else:
        try:
            stats = write_ratings(store, opts.table, season, week, rows)
        except Exception as exc:
            log.exception("writing ratings failed")
            raise RunFailure(
                WRITE_FAILURE_MARKER,
                f"season {season} week {week}: {type(exc).__name__}: {exc} (a rerun is idempotent; nothing was published)",
            ) from exc

    published = None
    keys: list[str] = []
    if opts.dry_run:
        pub = RankingsPublisher(None, "(dry run)", opts.prefix)
        keys = [pub.week_key(season, week), "(latest.json files and index.json follow if this is the newest week)"]
    elif opts.publishes:
        try:
            published, _ = _publish(store, opts, season, week, r2, now())
        except (PublishError, ValueError) as exc:
            raise RunFailure(
                PUBLISH_FAILURE_MARKER,
                f"season {season} week {week}: {exc}; ratings are written to {opts.table} but the artifacts are not "
                f"(fully) published and the site was not rebuilt; rerun to republish",
            ) from exc
        keys = published.keys

    out(f"{DRY_RUN_MARKER if opts.dry_run else 'WEEKLY_SUMMARY'} season={season} week={week} "
        f"table={opts.table} mode={'staging' if opts.staging else 'production'}")
    items: list[tuple[str, Any]] = [
        ("teams_rated", len(rows)),
        ("model_status", solve.status),
        ("solve_seconds", f"{solve.solve_seconds:.2f} (build {solve.build_seconds:.2f}, total {result.seconds:.2f})"),
        ("margin_median_and_k", f"{solve.median_used:g} and {solve.k_margin:.4f} ({solve.margin_scale_basis})"),
        ("capped_games", f"{solve.n_capped} of {solve.n_games}"),
        ("teams_at_floor", floor),
        ("n_flat_teams", f"{solve.n_flat_teams} (teams with no losses; undetermined when prior_lambda is 0)"),
        ("r_lower", f"{solve.r_lower:.4f}"),
        ("prior_lambda", solve.lam),
        ("model_warnings", result.warnings or "none"),
        ("week_seconds", f"{time.perf_counter() - started:.2f}"),
    ]
    if plan is not None:
        items += [("would_write_ratings", plan), ("would_publish", keys)]
    else:
        items += [("ratings_rows_written", stats.rows_written), ("artifact_keys", keys or "none (staging, not published)")]
        if published is not None:
            items.append(("artifact_bytes", published.bytes_by_key))
    _lines(items, out)
    if not opts.dry_run:
        out(f"{OK_MARKER} season={season} week={week}")
    return stats.rows_written


def run(
    opts: Options, *, store: Store, api: Any = None, r2: Any = None,
    today: date | None = None, now: Callable[[], datetime] | None = None,
    out: Callable[[str], None] | None = None,
) -> int:
    """One run; returns the exit code. `api` is a CBBD client (or a factory), `r2` a
    (boto3 client, bucket) pair; both are built from the environment when needed and not given."""
    out = out or (lambda line: print(line, flush=True))
    now = now or (lambda: datetime.now(timezone.utc))
    try:
        _run(opts, store, api, r2, today or today_et(), now, out)
        return 0
    except RunNoop as noop:
        out(f"{NOOP_MARKER}: {noop.code}: {noop.message}")
        return 0
    except RunFailure as failure:
        out(f"{failure.marker}: {failure.message}")
        return failure.code
    except Exception as exc:
        log.exception("unexpected failure")
        out(f"{FAILURE_MARKER}: {type(exc).__name__}: {exc}")
        return 1


def _run(opts: Options, store: Store, api: Any, r2: Any, today: date, now: Callable[[], datetime], out: Callable[[str], None]) -> None:
    check_table(opts.table)
    season = opts.season if opts.season is not None else season_for_date(today)
    scheduled = opts.season is None and opts.week is None and not opts.range_mode
    try:
        selection_sunday = load_selection_sunday(season)
    except SeasonConfigError as exc:
        if opts.season is not None:
            raise RunFailure(FAILURE_MARKER, str(exc), 2) from exc
        raise RunNoop("season_not_configured", str(exc)) from exc
    if scheduled:
        start, end = season_window(selection_sunday, season)
        if not start <= today <= end:
            raise RunNoop("outside_season", f"{today} is outside the season {season} window {start}..{end}")

    row = get_season_row(store, season)
    if scheduled and row is not None and resolve_week(row, today) is None:
        raise RunNoop(
            "no_week_to_publish",
            f"season {season} has no week to publish on {today} (week 1 has not ended, or the week containing "
            f"Selection Sunday {row.selection_sunday} was already published); no API call was made",
        )
    if opts.publishes and r2 is None:
        try:
            r2 = r2_from_env()  # fail before any API call or write when R2 is not configured
        except PublishError as exc:
            raise RunFailure(PUBLISH_FAILURE_MARKER, str(exc)) from exc

    ingest_lines: list[tuple[str, Any]] = []
    if opts.no_ingest:
        if opts.dry_run:
            overrides = _overrides(PlanningStore(store), season, out)  # counts, writes nothing
            ingest_lines.append(("ingest", f"skipped (--no-ingest); overrides that would change games: {len(overrides.applied)}"))
        else:
            overrides = _overrides(store, season, out)
            ingest_lines.append(("ingest", f"skipped (--no-ingest); overrides applied: {len(overrides.applied)}"))
    else:
        summary, overrides = _ingest(store, season, api, out)
        ingest_lines += [
            ("api_calls", f"{summary.api_calls} (quota remaining {summary.quota_remaining})"),
            ("d1_rows_written_ingest", summary.total_rows_written + overrides.rows_written),
            ("teams_games_fetched", f"{summary.teams_fetched} teams, {summary.games_fetched} games"),
            ("ingest_anomalies", summary.anomalies or "none"),
            ("ingest_warnings", summary.warnings or "none"),
        ]
        out("ingest: " + json.dumps({"api_calls": summary.api_calls, "quota_remaining": summary.quota_remaining,
                                     "rows_written": summary.rows_written, "anomalies": summary.anomalies,
                                     "warnings": summary.warnings}, sort_keys=True))

    row = get_season_row(store, season)
    if row is None:
        raise RunFailure(FAILURE_MARKER, f"season {season} has no seasons row: run once without --no-ingest (or the backfill-games job)", 2)

    weeks = _target_weeks(opts, row, today)
    ingest_lines.append(("weeks", f"{weeks[0]}-{weeks[-1]} of {last_week(row)} (Selection Sunday {row.selection_sunday})"))

    fading = [w for w in weeks if BASKETBALL_DEFAULT.prior_lambda(w) > 0]
    if fading and not prior_season_available(store, season - 1):
        message = (
            f"season {season - 1} (the prior for season {season}) has no stored teams or rated games, but weeks "
            f"{fading[0]}-{fading[-1]} use it (prior weight lambda > 0); publishing rankings without priors would "
            f"be wrong. Backfill season {season - 1} (the Backfill Games workflow), or pass --allow-missing-prior"
        )
        if not opts.allow_missing_prior:
            raise RunFailure(PRIOR_FAILURE_MARKER, message)
        ingest_lines.append(("prior_season", f"MISSING, tolerated by --allow-missing-prior (weeks {fading[0]}-{fading[-1]} rated without priors)"))

    out(f"WEEKLY_RUN season={season} table={opts.table}{' DRY RUN' if opts.dry_run else ''}")
    _lines(ingest_lines, out)
    total = 0
    for week in weeks:
        total += _run_week(store, opts, season, week, r2, now, out)
    if opts.range_mode:
        out(f"RANGE_DONE season={season} weeks={weeks[0]}-{weeks[-1]} ratings_rows_written={total} "
            f"{'(dry run: nothing written or published)' if opts.dry_run else ''}".rstrip())


def parse_range(text: str) -> tuple[int, int]:
    """'3-7' or '5' -> (3, 7) / (5, 5)."""
    low, _, high = text.partition("-")
    try:
        a, b = int(low), int(high or low)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected A-B or W, got {text!r}") from None
    if a < 1 or b < a:
        raise argparse.ArgumentTypeError(f"need 1 <= A <= B, got {text!r}")
    return a, b


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--season", type=int, help="the year the season ends (2025-26 is 2026); default: today's season")
    parser.add_argument("--week", type=int, help="one explicit week; default: the week that ended yesterday")
    parser.add_argument("--weeks", type=parse_range, metavar="A-B", help="ratings-only mode: weeks A through B in order")
    parser.add_argument("--all-weeks", action="store_true", help="ratings-only mode: week 1 through the last week that has ended")
    parser.add_argument("--no-ingest", action="store_true", help="skip the ingest; rate what is stored (0 API calls)")
    parser.add_argument("--staging", action="store_true", help="write ratings_staging; publish nothing unless --publish-staging")
    parser.add_argument("--publish-staging", action="store_true", help="with --staging: publish under the cbb-staging/ prefix")
    parser.add_argument("--dry-run", action="store_true", help="with --no-ingest: rate and report; write and publish nothing")
    parser.add_argument("--allow-missing-prior", action="store_true",
                        help="do not fail when the prior season is not stored (weeks 1-6 then rate without priors)")
    return parser


def parse_options(argv: list[str] | None = None) -> Options:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.week is not None and (args.weeks or args.all_weeks):
        parser.error("--week cannot be combined with --weeks / --all-weeks")
    if args.weeks and args.all_weeks:
        parser.error("--weeks and --all-weeks are exclusive")
    if (args.weeks or args.all_weeks) and args.season is None:
        parser.error("--weeks / --all-weeks need an explicit --season")
    if args.week is not None and args.week < 1:
        parser.error("--week must be >= 1")
    if args.publish_staging and not args.staging:
        parser.error("--publish-staging needs --staging")
    if args.dry_run and not args.no_ingest:
        parser.error("--dry-run needs --no-ingest (an ingest writes games)")
    return Options(
        season=args.season, week=args.week, weeks=args.weeks, all_weeks=args.all_weeks, no_ingest=args.no_ingest,
        staging=args.staging, publish_staging=args.publish_staging, dry_run=args.dry_run,
        allow_missing_prior=args.allow_missing_prior,
    )


def main(
    argv: list[str] | None = None, *, store: Store | None = None, api: Any = None, r2: Any = None,
    today: date | None = None, now: Callable[[], datetime] | None = None,
) -> int:
    opts = parse_options(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    try:
        store = store or get_store()
    except Exception as exc:  # e.g. the D1 variables are missing: still a marker line, not a bare traceback
        print(f"{FAILURE_MARKER}: {type(exc).__name__}: {exc}", flush=True)
        return 1
    return run(opts, store=store, api=api or CbbdClient, r2=r2, today=today, now=now)


if __name__ == "__main__":
    sys.exit(main())
