# cbb-rankings

A convex quadratic program that ranks NCAA Division I men's basketball teams from game outcomes,
ported from `cfb-rankings`. A weekly GitHub Actions job ingests games from
[CollegeBasketballData](https://collegebasketballdata.com) into Cloudflare D1, solves the model,
and publishes JSON rankings to R2 for the site.

Status: storage, ingestion, result overrides, backfill, the model, the weekly run and publishing to
R2 (plan: `docs/cbb-rankings-port/plan.yaml`). Not live until the launch checklist below is done.

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
CBB_STORE=local uv run python -m ingest.run week 2027 3   # games for week 3's window (manual, cheap)
```

The scheduled weekly path is the full-season ingest (`ingest.run.ingest_season`, 4 API calls): it
refreshes the teams, re-derives `is_rated` for every game and heals a missed week. `week` is a
cheap manual shortcut. Both print a JSON summary (API calls, rows fetched and upserted, rows
written per table, games dropped, anomalies, warnings, `ok`, `failures`) and then re-apply the
operator overrides. They exit non-zero, printing `INGEST_FAILURE: <reasons>`, when `ok` is false
and print/log `OVERRIDE_FAILURE` for a failed override entry. Needs `CBBD_API_KEY` (in agent
sessions a placeholder such as `proxy-injected` is fine; a proxy supplies the real header).

Rules:

- `teams` is `/teams?season=S` (D-I only). A game side whose team id is not in that season's teams
  is non-D-I; games with both sides non-D-I are dropped and counted.
- Played means `winner_team_id IS NOT NULL`: a `final` with both scores present and unequal.
  Anything else stores NULL scores, margin, winner and alpha. A rated, played game is
  `is_rated = 1 AND winner_team_id IS NOT NULL`, and W-L records count rated, played games only (an
  `exclude` override removes a game from both the rating and the record). These are defined once in
  `ingest/rules.py` (`PLAYED_SQL`, `RATED_PLAYED_SQL`, `is_played`, `is_rated_played`). A final with
  a missing score or a tie (the source has 0-0 finals against non-D-I schools) is logged, counted
  as the anomaly `final_score_missing` / `final_tie`, and stored with its status `final` but no
  result and `is_rated = 0`.
- `winner_team_id`, `margin = abs(home - away)` and `alpha` (1.0 neutral, 0.8 home win, 1.2 away
  win; `ingest.derive.AlphaConfig`) follow the football pipeline.
- `game_date` is the US/Eastern date of `start_date`; a timestamp without an offset, or a bare date,
  is rejected (that row is a `malformed_row`). `is_rated` = regular season and
  `game_date <= selection_sunday` and a usable result for finals (conference tournaments and
  in-season events included; NCAA, NIT and Crown are postseason).
- Guards on the stored season, each counted as an anomaly kind: a stored game absent from a complete
  season fetch is neutralized (`is_rated = 0`, status `cancelled`, row kept: `absent_from_source`),
  and rows with the same team pair and `game_date` that would all be rated and played keep only the
  largest id rated (`duplicate_rated_pair`). A changed D-I team count is a warning; teams are never
  deleted.
- Gates (`IngestSummary.ok`, `.failures`): no teams or games fetched, dropped or malformed rows above
  a threshold (the larger of 5 and 0.5% of rows; env `INGEST_MAX_DROPPED`), or a season fetch under
  90% of the stored games. `last_ingested_at` is stamped only by a run that passed them, after all
  writes. The API calls precede the first write.
- Weeks (`ingest/weeks.py`, used by every later task): Monday-Sunday buckets of Eastern game dates
  from the season's stored `anchor_monday`; earlier games fold into week 1. The anchor is the
  Monday on or before the first date with 20 or more games, computed once when the season row is
  created and never recomputed. `resolve_week(season_row, today)` returns the week that ended on
  the Sunday before the current week (a Monday run publishes yesterday's week), or None before
  week 1 has ended and after the week containing Selection Sunday has been published.
- Selection Sunday is entered by hand in `ingest/seasons.json` and must be a Sunday. An entry marked
  `provisional` logs a warning on every run until confirmed; a missing season is an error that says
  what to add. After correcting a date, run `ingest.run season S`: it warns that the season is
  re-rated and recomputes `is_rated` for every game (`ingest_week` refuses, it cannot re-rate the
  season).
- `ingest_week` needs a season that was fully ingested (`last_ingested_at` set), so run
  `ingest.run season S` once per season first. It fetches `[Monday - 10 days, Sunday]` of the week
  (previous week and late corrections included) and teams only if none are stored. It never
  neutralizes absent games.

API budget (about 1,000 calls a month, shared with a live football pipeline): a full season is 4
calls (1 teams + 1 per 3,000 games; `/games` is capped at 3,000 rows); a week is 1 call (2 if teams
are missing). Every HTTP request sent, retries included, is counted in `CbbdClient.calls` and
reported in the summary together with `X-CallLimit-Remaining`. Once that header is below
`CBBD_QUOTA_FLOOR` (default 50) the client raises `QuotaFloorError` instead of making another call,
and a 429 is retried only once unless it carries `Retry-After`, so a spent quota fails fast.

## Overrides

`ingest/game_result_overrides.json` (shipped empty: `{"version": 1, "overrides": []}`) holds
operator corrections, reviewed in pull requests and re-applied on every run. Three kinds:

```json
{"kind": "score", "game_id": 401, "season": 2026, "home_team_id": 52, "away_team_id": 99,
 "home_score": 70, "away_score": 72, "periods": 2, "reason": "Official scorer corrected the final"}
{"kind": "exclude", "game_id": 402, "season": 2026, "home_team_id": 52, "away_team_id": 721,
 "reason": "Exhibition against a non-D-I school that the source lists as official"}
{"kind": "site", "game_id": 213466, "season": 2026, "home_team_id": 902, "away_team_id": 344,
 "neutral_site": false, "swap_home_away": true, "reason": "Played at Washington State's arena"}
```

`score` re-derives winner, margin and alpha; `exclude` sets `is_rated = 0` (periods is optional);
`site` fixes the source's inverted-site quirks by setting `neutral_site` and optionally swapping
home and away (`swap_home_away`), then re-derives winner, margin and alpha. A game takes at most one
`score` or `site` entry plus optionally one `exclude`. Season and team ids must match the stored
game or that entry fails with `OVERRIDE_FAILURE` in the log (the other entries still apply, and CI
should fail on the marker). A re-ingest overwrites an overridden game, so
`ingest.overrides.apply_overrides(store, season)` must run after every ingest and before the model
(`ingest.run` and the backfill do it themselves). Details and write budget: `AGENTS.md`.

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
100,000 per day). Exit is non-zero on a fetch failure, an `INGEST_FAILURE` (nothing is written then)
or an `OVERRIDE_FAILURE`. The estimate for CHANGED rows is a model that has not been verified
against real D1. The same job is the
manual "Backfill Games" workflow (inputs `season`, `week`, `apply`, `max_rows_written`).

## Weekly run and publishing

`main.py` is the entry point of the Monday job (workflow `weekly-update.yml`, cron `0 10 * * 1`
plus a manual dispatch with optional `season` and `week`):

```
uv run python main.py                                   # today's season, the week that ended yesterday
uv run python main.py --season 2027 --week 4            # one explicit week
uv run python main.py --season 2027 --week 4 --staging  # writes ratings_staging, publishes nothing
uv run python main.py --season 2026 --all-weeks --no-ingest --dry-run   # ratings backfill plan
```

One run: `ingest_season` (4 API calls) -> `apply_overrides` -> prior-season check -> `rate_week` ->
upsert `ratings` (rounded to 6 decimals, stale rows of that season and week deleted) -> publish the
week artifacts to R2 -> summary and `WEEKLY_OK season=S week=W`. The workflow then calls the
`PERSONAL_SITE_DEPLOY_HOOK_URL` deploy hook, only after a run that printed `WEEKLY_OK`.

Markers (printed on their own line; each stops the run and exits non-zero unless noted):

| Marker | Meaning |
| --- | --- |
| `WEEKLY_NOOP: <code>: <why>` | exit 0, nothing done, no API call, no deploy hook. Codes: `outside_season` (today is outside Nov 1 through the Sunday after Selection Sunday), `no_week_to_publish` (week 1 has not ended, or the Selection Sunday week was already published), `season_not_configured` (the season is not in `ingest/seasons.json`) |
| `INGEST_FAILURE` | `IngestSummary.ok` is false, or the fetch raised (including the quota floor); nothing after it runs, so stale ratings are never republished |
| `OVERRIDE_FAILURE` | an override entry or the file failed; the run stops before the model (a known-wrong game would be rated) |
| `PRIOR_FAILURE` | weeks 1-6 (prior weight above 0) need season S-1's games stored: backfill it, or pass `--allow-missing-prior` |
| `MODEL_FAILURE` | `ModelSolveError` / `NoTeamsError`; nothing written |
| `WRITE_FAILURE` | the ratings write failed; nothing published, a rerun is idempotent |
| `PUBLISH_FAILURE` | R2 not configured, or an upload or listing failed; ratings stay written, the site is not rebuilt |
| `WEEKLY_FAILURE` | usage errors (exit 2) and anything unexpected |
| `WEEKLY_OK` | success; `RANGE_DONE` closes a range run, `WEEKLY_DRY_RUN` replaces `WEEKLY_OK` in a dry run |

A season with no `seasons` row yet (a new season's first Monday) is ingested first (the only way to
learn its anchor) when the date is inside the season window, then the week is resolved; every other
Monday decides from `ingest/seasons.json` and the stored row without an API call.

Modes: `--season` / `--week` override the target (a week must have ended and be on or before the
Selection Sunday week). `--weeks A-B` or `--all-weeks` (season required) rate weeks in order from
stored data after one ingest, or none with `--no-ingest`; `--dry-run` (needs `--no-ingest`) rates,
compares with the stored ratings and prints what would be written and published. `--staging` writes
`ratings_staging` and publishes nothing, unless `--publish-staging` publishes under `cbb-staging/`
(the production prefix is never touched by a staging run; the staging ingest still writes `games`
and `teams`). Reruns are idempotent: unchanged ratings write 0 rows; artifacts are re-uploaded
(the week file differs only in `generated_at_utc`).

Artifacts (`artifacts/`): the week payload is built from the STORED `ratings` joined to `teams`, so a
republish equals the original: `{season, week, generated_at_utc, rankings: [{rank, team, conference,
logo, record, rating, delta}]}`, ranked by rating descending then team name, `rating` rounded to 2
decimals, `delta` = previous rank minus rank against the greatest earlier week of the season that
has ratings. R2 keys (prefix `cbb/`; same bucket as football, whose `rankings/` and `schedule/` keys
are never listed, read or written):

```
cbb/rankings/{season}/week-{WW}.json   cbb/rankings/{season}/latest.json
cbb/rankings/latest.json               cbb/rankings/index.json   {generated_at_utc, latest_season, latest_week, seasons:[{season, weeks}]}
```

`latest.json` files only move when the published week is the newest (a backfill of an old week
leaves them alone); the index is rebuilt by listing the bucket. Publishing never swallows an error.
Typical size: about 85 KB per week file (365 teams, indented JSON), 1.6 MB for a 19-week season.

Budgets: a Monday run is 4 CBBD calls; D1 rows written are the changed games (1 row each, more only
when `week` or `is_rated` change), 365 `ratings` rows (WITHOUT ROWID, one row per insert) and the
season stamp, roughly 1,500 (changed-row costs are unverified against real D1); R2 is 4 puts and a
listing. A full-season ratings backfill is 365 x 19 = 6,935 D1 rows and no API call.

### Launch checklist (operator; not executed by any code)

1. Add the GitHub secrets: `CBBD_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_D1_DATABASE_ID`,
   `CLOUDFLARE_API_TOKEN`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
   `R2_BUCKET_NAME`, `PERSONAL_SITE_DEPLOY_HOOK_URL`. No cost.
2. Backfill games with "Backfill Games" (dry run, then apply; the apply reuses the dry run's
   responses): season 2026 (2025-26, the prior of the current season) and season 2027 (2026-27).
   Each is 4 API calls and about 19,000 D1 rows written for a full season (the 2027 slice is smaller
   until the schedule fills in); both on one day stay well under the 100,000 per day limit. Optional:
   season 2025 as well (4 calls, about 19,000 rows) so that 2025-26 is browsable with proper priors.
3. Backfill ratings with "Backfill Ratings": staging first (`destination` staging, dry run, then
   apply; then `staging-published` to check the R2 files), then `production`. For the weeks already
   played of 2027 (and `all` for 2026). 0 API calls; D1 rows 365 per week per table (2026: 6,935;
   2027 weeks 1-3: 1,095), staging and production each; R2 about 5 requests per week. Weeks 1-6 of
   2026 need season 2025 stored, or `allow_missing_prior` ticked. Deltas need the earlier weeks
   stored, so backfill every week from 1.
4. Confirm Selection Sunday 2027 in `ingest/seasons.json` (currently `provisional`; the date decides
   `is_rated` and the last published week). After a correction run `ingest.run season 2027`.
5. Enable the weekly schedule: it is active once `weekly-update.yml` is on the default branch; the
   first scheduled Monday run costs 4 API calls and the D1 rows above. Optionally rehearse one week
   with "Test Week Run" first (4 calls, 365 `ratings_staging` rows plus the ingest writes).
6. On the site repository, flip the basketball sport from optional to required, then rebuild (the
   weekly job's deploy hook does it every Monday; "Backfill Ratings" does not call the hook).

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

CI uses `uv sync --frozen` and `uv run --frozen`. Copy `.env.example` to `.env` for local runs. Secrets (set in the repository settings for
Actions): `CBBD_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_D1_DATABASE_ID`, `CLOUDFLARE_API_TOKEN`
(D1 edit permission), `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`
(R2 object read/write on the bucket) and `PERSONAL_SITE_DEPLOY_HOOK_URL` (used by the weekly
workflow only). Use `CBB_STORE=local` to run against a local sqlite file instead. Optional
settings: `CBBD_QUOTA_FLOOR` (default 50) and `INGEST_MAX_DROPPED` (see `.env.example`).

## Migrations

Add `database/migrations/NNNN_description.sql`, then run the "Apply D1 Migrations" workflow
(manual), or locally `npx wrangler@4 d1 migrations apply cbb-rankings --remote` with
`CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` set. Applied migrations are tracked in the
`d1_migrations` table and are never edited.

## Workflows

- `tests.yml` - pytest on every push and pull request
- `apply-migrations.yml` - manual D1 migration apply
- `backfill-games.yml` - manual ingest-only backfill, dry run unless `apply` is ticked
- `weekly-update.yml` - Monday 10:00 UTC (and manual): the weekly run, then the site deploy hook
- `test-week-run.yml` - manual `main.py --staging` rehearsal of one week
- `backfill-ratings.yml` - manual ratings-only backfill from stored games, dry run unless `apply` is ticked
