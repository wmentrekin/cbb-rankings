# AGENTS.md

## Project

Weekly batch pipeline (GitHub Actions) that ranks NCAA Division I men's basketball teams with a
convex quadratic program ported from `cfb-rankings`: ingest games from CollegeBasketballData
(CBBD) into Cloudflare D1, solve, publish JSON to R2. Storage, ingestion (`ingest/`), the model (`model/`), the weekly run (`main.py`) and publishing (`artifacts/`) exist. Plan:
`docs/cbb-rankings-port/`. Source behaviour: `docs/source-probe.md`.

## Layout

- `database/migrations/` - D1 schema, plain SQLite SQL, applied in name order
- `database/store.py` - the `Store` interface, upsert SQL builder, chunking, identifier checks
- `database/d1.py` - `D1Client`: REST client (retries, batch, `/raw` reads)
- `database/local.py` - `LocalStore`: same interface on `sqlite3`, applies the migrations
- `database/__init__.py` - `get_store()`, chosen by `CBB_STORE` (`d1` default, `local`)
- `ingest/http.py` - `CbbdClient`: retrying GET with a call counter; `ingest/teams.py`,
  `ingest/games.py` - fetch and map; `ingest/derive.py` - pure per-game derivations and
  classification, `AlphaConfig`; `ingest/rules.py` - the played/rated/record definitions;
  `ingest/weeks.py` - seasons table and week logic; `ingest/seasons.json` - hand-entered Selection
  Sundays; `ingest/run.py` - `ingest_season` (the scheduled path), `ingest_week` (manual), gates,
  guards, CLI; `ingest/overrides.py` + `ingest/game_result_overrides.json` - operator overrides
  (`score`, `exclude`, `site`); `ingest/planning.py` - no-write `PlanningStore` and `CachingClient`
  for dry runs
- `model/` - `config.py` (`ModelConfig`, `FOOTBALL_COMPAT`, `BASKETBALL_DEFAULT`), `assemble.py`, `solve.py`,
  `pipeline.py` (`rate_week`); see `model/README.md`
- `main.py` - the weekly run and the ratings-only backfill (`run(Options, store=, api=, r2=, today=)` is
  the testable core); `artifacts/rankings.py` - the week payload from stored ratings;
  `artifacts/r2.py` - boto3 client for R2 and `RankingsPublisher`
- `scripts/backfill_games.py` - ingest-only backfill (dry run by default); workflow
  `.github/workflows/backfill-games.yml`
- `tests/` - pytest; no network
- `wrangler.jsonc` - only the `d1_databases` entry (used by `wrangler d1 migrations`)

Packages are flat top-level directories (`database/`, `ingest/`, `model/`, `artifacts/`);
pytest adds the repo root to `sys.path`.

## Commands

```
uv sync            # install (Python 3.12); CI uses `uv sync --frozen` and `uv run --frozen`
uv run pytest -q   # tests
```

## Storage rules (must be respected by every later task)

- Teams are keyed by source team id everywhere; names are display only. `teams` holds D-I teams
  per season; a game side whose id is not in `teams` for that season is non-D-I. Team ids in
  `games` are deliberately not foreign keys.
- Write only through `store.upsert(table, rows, key)`. It sends each chunk as ONE JSON text
  parameter expanded with `json_each`, because D1's REST API delivers plain numeric bound
  parameters as REAL (integers would be stored as floats). Do not bind integers directly in write
  statements. Rows must share the same columns; values are int, float, str or None.
- Upserts carry a changed-values guard (`WHERE t.c IS NOT excluded.c ...`): an unchanged row
  writes nothing. Do not replace it with an unconditional update.
- Chunks are at most 500 rows and about 1 MB serialized; a request has 30 s on D1. Chunks are
  separate statements, not atomic together; use `execute_batch` (atomic, rolls back whole) when
  atomicity matters. A write retried after a timeout is flagged in `WriteStats.retried_writes`;
  when it is non-zero `rows_written` may be under-counted.
- Table and column names are interpolated into SQL and must match `[A-Za-z_][A-Za-z0-9_]*`;
  never interpolate values.
- Budgets: free plan allows 100,000 rows written per day, and every index adds a written row per
  insert (a `games` insert writes 3 rows: table plus two indexes). A full 6,300-game season is
  about 19,000 rows written on first insert; re-runs write 0 when nothing changed. Verified on real
  D1 (storage task): a 1,200-row `games` upsert in chunks of 500/500/200 and a 365-row `ratings`
  upsert (the insert and unchanged-rerun costs). Not verified: a full 6,300-game season in one run, and
  the cost of CHANGED rows (the backfill planner's model: 1 row, plus 2 index rows when `week` or
  `is_rated` changes). Add indexes
  only for planned queries. Composite-key tables are `WITHOUT ROWID` so the key is not a second
  written row. Report `WriteStats.rows_written` from any bulk job.
- API budget: 1,200 requests per 5 minutes. Time Travel keeps 7 days.
- `LocalStore` runs the same SQL; its `rows_written` is sqlite3's changed-row count (no index
  rows) and `rows_read` is 0. Use it for unit tests and dry runs.
- Migrations are append-only: add `NNNN_name.sql`; never edit an applied one.
- Never touch any D1 database other than the one in `wrangler.jsonc`.

## Ingestion rules (must be respected by every later task)

- Scheduled path: the weekly job calls `ingest.run.ingest_season` every run (4 API calls: teams
  plus 3 game pages for a 6,300-game season). That refreshes teams, re-derives `is_rated` for every
  game and heals a missed week. `ingest_week` (1 call) is a manual shortcut, not the scheduled path.
- Played and rated, ONE definition, in `ingest/rules.py` (import it, never restate it): a game is
  played when `winner_team_id IS NOT NULL` (`PLAYED_SQL`, `is_played`); a rated, played game is
  `is_rated = 1 AND winner_team_id IS NOT NULL` (`RATED_PLAYED_SQL`, `is_rated_played`); W-L records
  count rated, played games only (`RECORD_SQL`), so an `exclude` override or the duplicate guard
  removes a game from both the rating and the record. Status alone is not the test: a final with
  missing or equal scores (two real 0-0 finals exist in 2026-27: 374609, 375008) keeps
  `status = 'final'` but is stored with a NULL result AND `is_rated = 0`, and is counted in the
  summary anomalies as `final_tie` / `final_score_missing`. Non-D-I: id not in `teams` for the season.
- Weeks: use `ingest.weeks` only (`week_for_date`, `week_bounds`, `last_week`, `resolve_week`,
  `get_season_row`); never recompute an anchor or reimplement week arithmetic. The anchor is stored
  once per season and never changes.
- Alpha constants: `ingest.derive.AlphaConfig` / `DEFAULT_ALPHA` (1.0, 0.8, 1.2) are the single
  source; the model's config must take its defaults from them.
- Selection Sunday comes from `ingest/seasons.json` (entered by hand, must be a Sunday, `provisional`
  warns); add the new season there before ingesting it. Changing a stored date makes the next
  `ingest_season` warn and re-rate every game; `ingest_week` fails instead (a window cannot re-rate
  the season).
- Gates: `IngestSummary.ok` / `.failures`. A run fails on no teams or no games fetched (for a week:
  only inside the season), dropped or malformed rows above the threshold (larger of 5 and 0.5% of
  rows fetched; env `INGEST_MAX_DROPPED` or `max_dropped_rows`), or more stored games absent from a
  full season fetch than the allowance (larger of 5 and 0.5% of the stored games of the season; env
  `INGEST_MAX_ABSENT`, argument `max_absent_games`, CLI `--allow-absent N`): a truncated fetch looks
  like that, so nothing is neutralized and NO game row is written (teams and the seasons row are
  upserted; the season is not stamped). The operator override `--allow-absent N` exists on
  `python -m ingest.run season` and `scripts/backfill_games.py` only; the weekly workflow never sets
  it (nor the env var). `last_ingested_at` is stamped only by a run that
  passed every gate, after all writes; all API calls precede the first write. `ingest_week` raises
  `SeasonNotIngestedError` (no API call) when the season row is missing or `last_ingested_at` is NULL.
  The CLI and `scripts/backfill_games.py` exit non-zero when `ok` is false and print
  `INGEST_FAILURE: <reasons>` (CI and the orchestration grep for it).
- Guards on the stored season (anomaly kinds in `summary.anomalies`, first ids in `anomaly_ids`):
  `absent_from_source` - a stored game missing from a complete season fetch (up to the allowance
  above) is neutralized
  (`is_rated = 0`, status `cancelled`, result cleared, row kept; counted once, when neutralized, and
  restored if the source relists it); `duplicate_rated_pair` - rows with the same unordered team pair
  and `game_date` that would all be rated and played keep only the largest id rated (re-reported on
  every run while the source still lists both). Teams: a changed D-I team count versus the stored
  one is a warning only (stored teams are never deleted).
- CBBD quota (about 1,000 calls a month) is shared with a live football pipeline: a full season is 4
  calls. `CbbdClient` reads `X-CallLimit-Remaining` after every call and raises `QuotaFloorError`
  before the next one once it is below `CBBD_QUOTA_FLOOR` (default 50); a response without a readable
  header sets `quota_header_missing` and the ingest summary carries a warning once per run (the floor
  cannot protect the quota then, but the run does not fail); a 429 is retried once
  unless it carries `Retry-After`. Check `IngestSummary.api_calls` and `quota_remaining`; do not
  add per-game or per-team calls. Never log the API key.
- `ingest.derive.eastern_date` rejects timestamps without an offset and bare dates (the row becomes
  a `malformed_row`); the source's `startDate` ends in `Z`.
- Tests use `tests/fakes.py` (`FakeApi`, trimmed real-response fixtures); no network.

## Model rules (must be respected by every later task)

- `model/` has no database access except `pipeline.rate_week`, which only reads (`store.query_rows`);
  the orchestration writes `ratings`. Teams are integer ids; the dummy lower-division team is never a
  rating key (only `SolveResult.r_lower`).
- Played = `winner_team_id IS NOT NULL`, rated = `is_rated = 1`; margin and alpha come from the stored
  columns. `week` is an int >= 1.
- Every constant lives in `model.config.ModelConfig`. `FOOTBALL_COMPAT` and the football-parity tests
  are frozen yardsticks (`tests/reference/**` is never edited); `BASKETBALL_DEFAULT` may differ only
  where `tests/test_model_config.py` says so. The margin-scale basis is chosen in one line,
  `BASKETBALL_MARGIN_SCALE_BASIS` in `model/config.py` (owner decision 2026-11-23: the true median
  margin). Undetermined ratings (quirk 13: unbeaten teams at lambda 0) are published at their least
  optimal value (`flat_rating_rule="min_feasible"`; football keeps `"solver"`).
- Production solver is CLARABEL, named in config; only status `optimal` is accepted, anything else
  raises `ModelSolveError` so an unattended run fails loudly. Parity tests pass `solver="OSQP"`.
- The objective is built vectorized; keep it mathematically identical to football's (the parity tests
  are the check). Run `uv run pytest -q` (about 10 s; the 365-team scale test is included).

## Weekly run rules (must be respected by every later task)

- `main.py` order is fixed: validate the override file (before any API call or write) ->
  `ingest_season` -> `apply_overrides` -> catch-up weeks -> prior-season check -> `rate_week` ->
  write ratings -> publish. Each failure stops the run with its marker and a non-zero exit, and
  nothing after it runs: `INGEST_FAILURE`, `OVERRIDE_FAILURE`, `CATCHUP_FAILURE`, `PRIOR_FAILURE`,
  `MODEL_FAILURE`, `WRITE_FAILURE`, `PUBLISH_FAILURE`, `WEEKLY_FAILURE` (usage, unexpected, or a
  season missing from `ingest/seasons.json` from Nov 1 through Mar 31). Success is
  `WEEKLY_OK season=S week=W` (one line per week published); a no-op Monday prints
  `WEEKLY_NOOP: <code>: <why>` and exits 0. The workflow runs the deploy hook only after `WEEKLY_OK`;
  never add `|| true`. A failed ingest that wrote rows still runs `apply_overrides` before the run
  fails, so stored data never keeps the overridden games reverted.
- The season of a scheduled run comes from the calendar alone (`season_for_date`). Not configured in
  `ingest/seasons.json` means: Nov 1 through Mar 31, `WEEKLY_FAILURE` (naming the entry to add);
  April through October, a no-op, with a `WEEKLY_WARNING` line in September and October. Add each
  season's entry (Selection Sunday, `provisional: true` until confirmed) before September.
- Catch-up (default mode, no `--week`/`--weeks`/`--all-weeks`): after ingest and overrides, every
  week from 1 to the resolved week that has no rows in the target ratings table (`ratings`, or
  `ratings_staging` with `--staging`) is rated, written and published in ascending order BEFORE the
  resolved week, so a failed or skipped Monday heals on the next run and deltas use the right previous
  week. At most `--max-catch-up` (default 4) earlier weeks; more is `CATCHUP_FAILURE` telling the
  operator to run the "Backfill Ratings" workflow. The prior-season requirement covers catch-up
  weeks. Limit: only a missing ratings table row triggers a catch-up; a week whose ratings were
  written but whose upload failed (`PUBLISH_FAILURE`) is republished by rerunning it explicitly
  (`--week`), not by the next Monday.
- The scheduled path never takes a shortcut: always `ingest_season` (4 calls), except that a no-op
  Monday decides from `ingest/seasons.json` (window Nov 1 of S-1 through Selection Sunday + 7 days)
  and the stored season row, with no API call. A season with no row is ingested first, then resolved.
- A week is publishable only if it has ended and is on or before `last_week(row)`.
  Weeks 1-6 (`ModelConfig.prior_lambda > 0`) require season S-1's teams and rated games in the
  store (`PRIOR_FAILURE`), unless `--allow-missing-prior`.
- Ratings are stored rounded to 6 decimals so solver noise does not rewrite D1 rows; the write is
  `store.upsert` on (team_id, season, week) plus one `DELETE` of that (season, week)'s rows whose team
  is not in the result, with the integers carried in one JSON parameter (`json_extract` / `json_each`,
  never bound integers). Staging runs write `ratings_staging` only.
- Artifacts are built from the stored rows (`artifacts.rankings.build_payload`), never from the
  model result. Keys live under one prefix (`cbb/`, or `cbb-staging/` for `--publish-staging`);
  `artifacts.r2` never lists, reads or writes outside it (football's `rankings/...` and
  `schedule/...` share the bucket). `latest.json` files follow the true latest week; the index is
  rebuilt by listing. Publishing errors raise `PublishError`; nothing is swallowed. The JSON shapes
  are a contract with the site (`docs/cbb-rankings-port/plan.yaml`): do not change them.
- Ratings-only mode (`--weeks A-B` / `--all-weeks`, `--no-ingest`, `--dry-run`) is the only way to
  fill history; it is idempotent (0 D1 rows on a rerun; artifacts are re-uploaded).
- `n_flat_teams` (teams with no losses, rating undetermined once the prior has faded) is logged in
  the run summary only.

## Operator tools: overrides and backfill

Overrides (`ingest/game_result_overrides.json`, `{"version": 1, "overrides": [...]}`, shipped empty).
Three kinds of entry; all keys are required except `periods` and `swap_home_away`, unknown keys are
rejected, and `reason` must be a non-empty human explanation. A game takes at most one `score` or
`site` entry plus optionally one `exclude`:

```json
{"kind": "score", "game_id": 401, "season": 2026, "home_team_id": 52, "away_team_id": 99,
 "home_score": 70, "away_score": 72, "periods": 2, "reason": "Official scorer corrected the final"}
{"kind": "exclude", "game_id": 402, "season": 2026, "home_team_id": 52, "away_team_id": 721,
 "reason": "Exhibition against a non-D-I school that the source lists as official"}
{"kind": "site", "game_id": 213466, "season": 2026, "home_team_id": 902, "away_team_id": 344,
 "neutral_site": false, "swap_home_away": true, "reason": "Played at Washington State's arena"}
```

- `score` sets the scores (both present, whole, not equal) and re-derives winner_team_id, margin
  and alpha with `ingest.derive.derive_result` from the stored neutral_site; only a stored `final`
  game can be re-scored. `exclude` sets `is_rated = 0` and leaves scores alone (the team pair may
  be in either order). `site` fixes the source's inverted-site quirks: it sets `neutral_site` and,
  with `swap_home_away`, swaps home and away (ids, names and scores together), then re-derives
  winner, margin and alpha. A `site` entry lists the teams as the SOURCE has them; a game already
  swapped is recognised, so applying twice writes nothing. Real examples from the probe: 213466
  (home Chaminade at Washington State's arena: swap) and 358311 (a Maui game marked not neutral).
- Season and team ids are checked against the stored row; a mismatch, a missing game or a bad
  status fails that entry only: it is logged `OVERRIDE_FAILURE: game_id=... reason=...`, listed in
  the returned `OverrideSummary.failed`, and the rest still apply. A malformed file raises
  `OverrideFileError` (also logged with the marker). Entries for other seasons are skipped.
- A `score` entry pointing at a game that later becomes cancelled or is neutralized as absent from
  the source fails (`only a final game can be re-scored`), and so does any entry whose team ids or
  season no longer match the stored row: the run stops with `OVERRIDE_FAILURE` naming the game id
  until a person removes or corrects the entry (intended: a changed game needs a human decision).
  `exclude` and `site` entries on such a game still apply harmlessly. The file itself is validated
  before the weekly run spends any API call or writes anything.
- ORDERING RULE: ingest upserts rewrite an overridden game from the source, so
  `ingest.overrides.apply_overrides(store, season)` must run right after every ingest and before the
  model reads games. `python -m ingest.run` and `scripts/backfill_games.py` do this themselves and
  exit non-zero on any `OVERRIDE_FAILURE` or `INGEST_FAILURE`; the weekly orchestration must do the
  same (tests restore corrected values after a re-ingest, so the order is enforced by tests).
- Write budget: applying is idempotent (a matching row is not written; the upsert has the
  changed-values guard). An overridden game costs writes twice per run (the full-season ingest
  overwrites it, the override restores it): about 2 rows for `score` and `site`, up to about 6 for
  `exclude` (two index rows move each time). Ten overrides are under 100 rows a day of the 100,000.
  These changed-row costs are estimates, not verified against real D1.

Backfill (`scripts/backfill_games.py`, workflow "Backfill Games"): ingest only, for `--season S`
or `--season S --week W`. DRY RUN BY DEFAULT, and the dry run spends API calls (full season 4,
week 1; shared quota) because it fetches to compute the plan; it reports rows per table
(new / changed / unchanged), estimated D1 rows written (3 per new game, 1 per team or season
row, 1 per changed row, plus 2 index rows when week/is_rated change; the guards' neutralized and
unrated rows are included because they go through the same upsert; the overrides add 1 row for
`score`/`site` and 3 for `exclude`), anomalies, warnings, API calls and quota left, and writes
nothing (`PlanningStore` rejects every write but `upsert`, which it only counts). The changed-row
cost model is UNVERIFIED against real D1 (the insert and no-op cases were verified on a 1,200-row
`games` upsert in chunks of 500/500/200 and a 365-row `ratings` upsert). `--apply`
reuses the dry run's responses (no extra calls), writes, applies the overrides and prints
`BACKFILL_APPLIED season=S [week=W] ...`. It is refused (exit 2) when the estimate exceeds
`--max-rows-written` (default 40,000; a fresh full season is about 19,000). Exit 1 on a
fetch/ingest failure, an `INGEST_FAILURE` (nothing is written then), or any `OVERRIDE_FAILURE`. A
rerun of an unchanged season estimates 1 row (the season's `last_ingested_at`).

## Secrets (names only)

`CBBD_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_D1_DATABASE_ID`, `CLOUDFLARE_API_TOKEN`
(non-secret settings: `CBB_STORE`, `CBB_LOCAL_DB`, `CBBD_QUOTA_FLOOR`, `INGEST_MAX_DROPPED`, `INGEST_MAX_ABSENT`);
`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME` (the weekly run and the
ratings backfill), `PERSONAL_SITE_DEPLOY_HOOK_URL` (only the weekly workflow's hook step reads it).
Never log or commit values. In cloud agent sessions the token
variable may hold the placeholder `proxy-injected`; a proxy supplies the real credential.

## Shared Workflow Framework

This repo imports a shared agent workflow at `.agents/` (git subtree). Read `.agents/AGENTS.md`
before starting substantial work; invoke it with `/work`.
