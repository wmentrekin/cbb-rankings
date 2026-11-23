# cbb-rankings

A convex quadratic program that ranks NCAA Division I men's basketball teams from game outcomes,
ported from `cfb-rankings`. A weekly GitHub Actions job ingests games from
[CollegeBasketballData](https://collegebasketballdata.com) into Cloudflare D1, solves the model,
and publishes JSON rankings to R2 for the site.

Status: storage, ingestion, result overrides, backfill and the model. Orchestration and publishing
are planned in `docs/cbb-rankings-port/plan.yaml`.

## Storage

Cloudflare D1 (SQLite). The schema lives in `database/migrations/` and is applied with
`wrangler d1 migrations` (`wrangler.jsonc` holds the database binding only). Python reaches D1
through the REST API with `database.d1.D1Client`; `database.local.LocalStore` is the same
interface on `sqlite3` for tests and local runs. `get_store()` picks one from `CBB_STORE`.

Tables: `seasons`, `teams` (D-I teams per season), `games`, `ratings`, `ratings_staging`.

Budgets and rules for contributors: see "Storage rules" in `AGENTS.md` (100,000 rows written per
day on the free plan, an extra write per index, 30 s per request, writes through the JSON-text
upsert with the changed-values guard).

## Ingestion

`ingest/` pulls D-I teams and games from the CollegeBasketballData API (behaviour documented in
`docs/source-probe.md`) into the store through `store.upsert`, so reruns write nothing for
unchanged rows.

```
CBB_STORE=local uv run python -m ingest.run season 2026   # teams + every game (full season)
CBB_STORE=local uv run python -m ingest.run week 2027 3   # games for week 3's window
```

Both print a JSON summary: API calls, rows fetched and upserted, rows written per table, games
dropped, anomalies, warnings. Needs `CBBD_API_KEY` (in agent sessions a placeholder such as
`proxy-injected` is fine; a proxy supplies the real header).

Rules:

- `teams` is `/teams?season=S` (D-I only). A game side whose team id is not in that season's teams
  is non-D-I; games with both sides non-D-I are dropped and counted.
- Played means `status = 'final'` with both scores present and unequal. Anything else stores NULL
  scores, margin, winner and alpha. A final with a missing score or a tie is logged, counted as an
  anomaly and stored as not played (its status stays `final`, so consumers should test
  `winner_team_id IS NOT NULL`, not status alone).
- `winner_team_id`, `margin = abs(home - away)` and `alpha` (1.0 neutral, 0.8 home win, 1.2 away
  win; `ingest.derive.AlphaConfig`) follow the football pipeline.
- `game_date` is the US/Eastern date of `start_date`. `is_rated` = regular season and
  `game_date <= selection_sunday` (conference tournaments and in-season events included; NCAA,
  NIT and Crown are postseason).
- Weeks (`ingest/weeks.py`, used by every later task): Monday-Sunday buckets of Eastern game dates
  from the season's stored `anchor_monday`; earlier games fold into week 1. The anchor is the
  Monday on or before the first date with 20 or more games, computed once when the season row is
  created and never recomputed. `resolve_week(season_row, today)` returns the week that ended on
  the Sunday before the current week (a Monday run publishes yesterday's week), or None before
  week 1 has ended and after the week containing Selection Sunday has been published.
- Selection Sunday is entered by hand in `ingest/seasons.json`. An entry marked `provisional`
  logs a warning on every run until confirmed; a missing season is an error that says what to add.
  After correcting a date, rerun `ingest.run season S` so `is_rated` is recomputed for every game.
- `ingest_week` needs the season row, so run `ingest.run season S` once per season first. It
  fetches `[Monday - 10 days, Sunday]` of the week (previous week and late corrections included)
  and teams only if none are stored.

API budget (about 1,000 calls a month, shared with the football pipeline): a full season is 4
calls (1 teams + 1 per 3,000 games; `/games` is capped at 3,000 rows); a weekly update is 1
call (2 if teams are missing). Every HTTP request sent, retries included, is counted in
`CbbdClient.calls` and reported in the summary together with `X-CallLimit-Remaining`.

## Overrides

`ingest/game_result_overrides.json` (shipped empty: `{"version": 1, "overrides": []}`) holds
operator corrections, reviewed in pull requests and re-applied on every run. Two kinds:

```json
{"kind": "score", "game_id": 401, "season": 2026, "home_team_id": 52, "away_team_id": 99,
 "home_score": 70, "away_score": 72, "periods": 2, "reason": "Official scorer corrected the final"}
{"kind": "exclude", "game_id": 402, "season": 2026, "home_team_id": 52, "away_team_id": 721,
 "reason": "Exhibition against a non-D-I school that the source lists as official"}
```

`score` re-derives winner, margin and alpha; `exclude` sets `is_rated = 0` (periods is optional).
Season and team ids must match the stored game or that entry fails with `OVERRIDE_FAILURE` in the
log (the other entries still apply, and CI should fail on the marker). A re-ingest overwrites an
overridden game, so `ingest.overrides.apply_overrides(store, season)` must run after every ingest
and before the model. Details and write budget: `AGENTS.md`.

## Backfill

```
uv run python scripts/backfill_games.py --season 2026                  # dry run (default)
uv run python scripts/backfill_games.py --season 2026 --week 3 --apply # write one week
```

Ingest only, no model. The dry run fetches from the API (it spends calls: 4 for a full season, 1
for a week, from the quota shared with football) and prints what would be written: rows per table,
estimated D1 rows written (about 19,000 for a fresh full season), anomalies, warnings, calls and
quota left. `--apply` writes, re-applies the overrides and prints `BACKFILL_APPLIED season=S
[week=W]`; it refuses an estimate above `--max-rows-written` (default 40,000; D1's free plan allows
100,000 per day). Exit is non-zero on a fetch failure or an `OVERRIDE_FAILURE`. The same job is the
manual "Backfill Games" workflow (inputs `season`, `week`, `apply`, `max_rows_written`).

## Model

`model/` holds the optimization (`model/README.md`: formulation, configuration, differences from
football). `model.pipeline.rate_week(store, season, week)` reads a season from the store and
returns ratings, records and diagnostics without writing anything. Proven against golden outputs of
the real football code (`tests/test_model_parity.py`). The margin-scale basis is a pending owner
decision, set in one line in `model/config.py`.

## Setup

```
uv sync
uv run pytest -q
```

Copy `.env.example` to `.env` for local runs. Secrets (set in the repository settings for
Actions): `CBBD_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_D1_DATABASE_ID`, `CLOUDFLARE_API_TOKEN`
(D1 edit permission). Use `CBB_STORE=local` to run against a local sqlite file instead.

## Migrations

Add `database/migrations/NNNN_description.sql`, then run the "Apply D1 Migrations" workflow
(manual), or locally `npx wrangler d1 migrations apply cbb-rankings --remote` with
`CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` set. Applied migrations are tracked in the
`d1_migrations` table and are never edited.

## Workflows

- `tests.yml` - pytest on every push and pull request
- `apply-migrations.yml` - manual D1 migration apply
- `backfill-games.yml` - manual ingest-only backfill, dry run unless `apply` is ticked
