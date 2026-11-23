"""Ingest-only backfill of one season (full) or one season + week into the store.

    uv run python scripts/backfill_games.py --season 2026                 # dry run (default)
    uv run python scripts/backfill_games.py --season 2026 --week 3        # dry run, one week
    uv run python scripts/backfill_games.py --season 2026 --apply         # writes

Nothing but ingest and the result overrides runs: no model, no ratings, no publishing.

The DRY RUN DOES SPEND API CALLS (a full season is 4, a week 1; the monthly quota is shared
with the football pipeline). It fetches, runs the same derivation and classification as the
real ingest, and reports what would be written (rows per table, estimated D1 rows written, API
calls, quota left, anomalies, warnings) without writing anything. With --apply the responses
fetched for the estimate are reused, so the calls are spent once. After writing, the overrides
are re-applied (a re-ingest rewrites an overridden game from the source) and the marker
`BACKFILL_APPLIED season=S [week=W]` is printed.

--apply is refused when the estimate exceeds --max-rows-written (default 40,000; D1's free
plan allows 100,000 rows written per day); raise the limit explicitly to run a larger job. The
estimate includes the guards (absent games neutralized, duplicate pairs unrated) because they
write through the same upsert; the cost of CHANGED rows is a model not yet verified against
real D1.

A run whose ingest gates fail (`IngestSummary.ok` false: empty fetch, too many dropped rows,
...) prints `INGEST_FAILURE: <reasons>`; with --apply nothing is written in that case.

Exit codes: 0 success, 1 fetch/ingest failure, an INGEST_FAILURE or any OVERRIDE_FAILURE, 2
usage error or apply refused by the guard.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # `database`, `ingest` when run as a file

from database import get_store  # noqa: E402
from database.store import Store  # noqa: E402
from ingest.http import CbbdClient  # noqa: E402
from ingest.overrides import OVERRIDE_FAILURE_MARKER, OverrideFileError, apply_overrides, load_overrides  # noqa: E402
from ingest.planning import CachingClient, PlanningStore, override_restore_writes  # noqa: E402
from ingest.run import IngestSummary, failure_line, ingest_season, ingest_week  # noqa: E402

DEFAULT_MAX_ROWS_WRITTEN = 40_000
MIN_SEASON, MAX_SEASON = 2002, 2100
APPLIED_MARKER = "BACKFILL_APPLIED"
FAILED_MARKER = "BACKFILL_FAILED"

log = logging.getLogger("backfill_games")


def _slice(season: int, week: int | None) -> str:
    return f"season={season}" + (f" week={week}" if week is not None else "")


def _ingest(store: Store, season: int, week: int | None, client: Any) -> IngestSummary:
    return ingest_season(store, season, client) if week is None else ingest_week(store, season, week, client)


def _emit(report: dict[str, Any]) -> None:
    print(json.dumps(report, indent=2, sort_keys=True))


def run(season: int, week: int | None, *, apply: bool, max_rows_written: int, store: Store, client: Any) -> int:
    label = _slice(season, week)
    spent = "full season: 4 calls" if week is None else "one week: 1 call, 2 if the season has no teams yet"
    print(f"{'APPLY' if apply else 'DRY RUN'} {label}: fetching from the CBBD API, which spends API calls "
          f"from the shared monthly quota ({spent}).")

    try:
        entries = [e for e in load_overrides() if e["season"] == season]
    except OverrideFileError as exc:
        print(f"{OVERRIDE_FAILURE_MARKER}: {exc}")
        return 1

    cached = CachingClient(client)
    planner = PlanningStore(store)
    try:
        plan = _ingest(planner, season, week, cached)
    except Exception as exc:
        log.exception("fetch or ingest failed")
        print(f"{FAILED_MARKER} {label}: {type(exc).__name__}: {exc}")
        return 1
    if not plan.ok:
        print(f"{FAILED_MARKER} {label}: the ingest gates failed; nothing written")
        print(failure_line(plan.failures))
        return 1

    estimate = planner.estimated_rows_written + override_restore_writes(entries)
    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "season": season, "week": week,
        "api_calls": plan.api_calls, "quota_remaining": plan.quota_remaining,
        "teams_fetched": plan.teams_fetched, "games_fetched": plan.games_fetched,
        "dropped_both_non_d1": plan.dropped_both_non_d1,
        "anomalies": plan.anomalies, "anomaly_ids": plan.anomaly_ids, "warnings": plan.warnings,
        "plan": {t: p.as_dict() for t, p in sorted(planner.plans.items())},
        "override_entries_for_season": len(entries),
        "estimated_rows_written": estimate, "max_rows_written": max_rows_written,
    }
    if not apply:
        report["note"] = "dry run: nothing was written"
        _emit(report)
        print(f"DRY RUN {label}: nothing written; estimated D1 rows written {estimate}. Pass --apply to write.")
        return 0

    if estimate > max_rows_written:
        _emit(report)
        print(f"{FAILED_MARKER} {label}: estimated {estimate} rows written exceeds --max-rows-written "
              f"{max_rows_written}; nothing written. D1's free plan allows 100,000 rows per day; raise the limit "
              "explicitly if this is intended.")
        return 2

    try:
        summary = _ingest(store, season, week, cached)  # answered from the cache: no new API calls
        overrides = apply_overrides(store, season)
    except Exception as exc:
        log.exception("apply failed")
        print(f"{FAILED_MARKER} {label}: {type(exc).__name__}: {exc}")
        return 1
    report.update(
        api_calls=cached.calls, quota_remaining=cached.quota_remaining,
        rows_written=summary.rows_written, total_rows_written=summary.total_rows_written + overrides.rows_written,
        retried_writes=summary.retried_writes, ok=summary.ok, failures=summary.failures,
        overrides=overrides.as_dict(),
    )
    _emit(report)
    print(f"{APPLIED_MARKER} {label} rows_written={report['total_rows_written']} "
          f"overrides_applied={len(overrides.applied)} overrides_failed={len(overrides.failed)}")
    if not summary.ok:
        print(failure_line(summary.failures))
    return 0 if overrides.ok and summary.ok else 1


def main(argv: list[str] | None = None, *, store: Store | None = None, client: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--season", type=int, required=True, help="the year the season ends (2025-26 is 2026)")
    parser.add_argument("--week", type=int, help="only this week's window; omit for the whole season")
    parser.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    parser.add_argument("--max-rows-written", type=int, default=DEFAULT_MAX_ROWS_WRITTEN,
                        help="refuse --apply above this estimate (default %(default)s)")
    args = parser.parse_args(argv)
    if not MIN_SEASON <= args.season <= MAX_SEASON:
        parser.error(f"--season must be in [{MIN_SEASON}, {MAX_SEASON}]")
    if args.week is not None and args.week < 1:
        parser.error("--week must be >= 1")
    if args.max_rows_written < 1:
        parser.error("--max-rows-written must be positive")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run(
        args.season, args.week, apply=args.apply, max_rows_written=args.max_rows_written,
        store=store or get_store(), client=client or CbbdClient(),
    )


if __name__ == "__main__":
    sys.exit(main())
