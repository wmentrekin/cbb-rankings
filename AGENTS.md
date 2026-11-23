# AGENTS.md

## Project

Weekly batch pipeline (GitHub Actions) that ranks NCAA Division I men's basketball teams with a
convex quadratic program ported from `cfb-rankings`: ingest games from CollegeBasketballData
(CBBD) into Cloudflare D1, solve, publish JSON to R2. Storage and ingestion (`ingest/`) exist; the model (`model/`) and publishing (`artifacts/`) arrive in later batches. Plan:
`docs/cbb-rankings-port/`. Source behaviour: `docs/source-probe.md`.

## Layout

- `database/migrations/` - D1 schema, plain SQLite SQL, applied in name order
- `database/store.py` - the `Store` interface, upsert SQL builder, chunking, identifier checks
- `database/d1.py` - `D1Client`: REST client (retries, batch, `/raw` reads)
- `database/local.py` - `LocalStore`: same interface on `sqlite3`, applies the migrations
- `database/__init__.py` - `get_store()`, chosen by `CBB_STORE` (`d1` default, `local`)
- `ingest/http.py` - `CbbdClient`: retrying GET with a call counter; `ingest/teams.py`,
  `ingest/games.py` - fetch and map; `ingest/derive.py` - pure per-game derivations and
  classification, `AlphaConfig`; `ingest/weeks.py` - seasons table and week logic;
  `ingest/seasons.json` - hand-entered Selection Sundays; `ingest/run.py` - `ingest_season`,
  `ingest_week`, CLI; `ingest/overrides.py` + `ingest/game_result_overrides.json` - operator result
  overrides; `ingest/planning.py` - no-write `PlanningStore` and `CachingClient` for dry runs
- `scripts/backfill_games.py` - ingest-only backfill (dry run by default); workflow
  `.github/workflows/backfill-games.yml`
- `tests/` - pytest; no network
- `wrangler.jsonc` - only the `d1_databases` entry (used by `wrangler d1 migrations`)

Packages are flat top-level directories (`database/`, later `ingest/`, `model/`, `artifacts/`);
pytest adds the repo root to `sys.path`.

## Commands

```
uv sync            # install (Python 3.12)
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
  atomicity matters.
- Table and column names are interpolated into SQL and must match `[A-Za-z_][A-Za-z0-9_]*`;
  never interpolate values.
- Budgets: free plan allows 100,000 rows written per day, and every index adds a written row per
  insert (a `games` insert writes 3 rows: table plus two indexes). A full 6,300-game season is
  about 19,000 rows written on first insert; re-runs write 0 when nothing changed. Add indexes
  only for planned queries. Composite-key tables are `WITHOUT ROWID` so the key is not a second
  written row. Report `WriteStats.rows_written` from any bulk job.
- API budget: 1,200 requests per 5 minutes. Time Travel keeps 7 days.
- `LocalStore` runs the same SQL; its `rows_written` is sqlite3's changed-row count (no index
  rows) and `rows_read` is 0. Use it for unit tests and dry runs.
- Migrations are append-only: add `NNNN_name.sql`; never edit an applied one.
- Never touch any D1 database other than the one in `wrangler.jsonc`.

## Ingestion rules (must be respected by every later task)

- Weeks: use `ingest.weeks` only (`week_for_date`, `week_bounds`, `last_week`, `resolve_week`,
  `get_season_row`); never recompute an anchor or reimplement week arithmetic. The anchor is stored
  once per season and never changes.
- Alpha constants: `ingest.derive.AlphaConfig` / `DEFAULT_ALPHA` (1.0, 0.8, 1.2) are the single
  source; the model's config must take its defaults from them.
- Played test for the model: `winner_team_id IS NOT NULL` (a malformed final is stored with status
  `final` but NULL result). Rated games: `is_rated = 1`. Non-D-I: id not in `teams` for the season.
- Selection Sunday comes from `ingest/seasons.json` (entered by hand, `provisional` warns); add the
  new season there before ingesting it.
- CBBD quota (about 1,000 calls a month) is shared with football: a full season is 4 calls, a weekly
  update 1. Check `IngestSummary.api_calls` and `quota_remaining`; do not add per-game or per-team
  calls. Never log the API key.
- Tests use `tests/fakes.py` (`FakeApi`, trimmed real-response fixtures); no network.

## Operator tools: overrides and backfill

Overrides (`ingest/game_result_overrides.json`, `{"version": 1, "overrides": [...]}`, shipped empty).
Two kinds of entry; all keys are required except `periods`, unknown keys are rejected, one entry
per `game_id`, and `reason` must be a non-empty human explanation:

```json
{"kind": "score", "game_id": 401, "season": 2026, "home_team_id": 52, "away_team_id": 99,
 "home_score": 70, "away_score": 72, "periods": 2, "reason": "Official scorer corrected the final"}
{"kind": "exclude", "game_id": 402, "season": 2026, "home_team_id": 52, "away_team_id": 721,
 "reason": "Exhibition against a non-D-I school that the source lists as official"}
```

- `score` sets the scores (both present, whole, not equal) and re-derives winner_team_id, margin
  and alpha with `ingest.derive.derive_result` from the stored neutral_site; only a stored `final`
  game can be re-scored. `exclude` sets `is_rated = 0` and leaves scores alone.
- Season and both team ids are checked against the stored row; a mismatch, a missing game or a bad
  status fails that entry only: it is logged `OVERRIDE_FAILURE: game_id=... reason=...`, listed in
  the returned `OverrideSummary.failed`, and the rest still apply. A malformed file raises
  `OverrideFileError` (also logged with the marker). Entries for other seasons are skipped.
- ORDERING RULE: ingest upserts rewrite an overridden game from the source, so the weekly
  orchestration must call `ingest.overrides.apply_overrides(store, season)` right after ingest and
  before the model reads games (after every `ingest_season`/`ingest_week`, in the backfill too). Any
  `OVERRIDE_FAILURE` must fail the job (CI greps for the marker or reads `summary.ok`).
- Write budget: applying is idempotent (a matching row is not written; the upsert has the
  changed-values guard). An overridden game inside the ingest window costs writes twice per run
  (ingest overwrites it, the override restores it): about 2 rows for `score`, up to about 6 for
  `exclude` (two index rows move). Ten overrides are under 100 rows a day of the 100,000.

Backfill (`scripts/backfill_games.py`, workflow "Backfill Games"): ingest only, for `--season S`
or `--season S --week W`. DRY RUN BY DEFAULT, and the dry run spends API calls (full season 4,
week 1; shared quota) because it fetches to compute the plan; it reports rows per table
(new / changed / unchanged), estimated D1 rows written (3 per new game, 1 per team or season
row, 1 per changed row, plus 2 index rows when week/is_rated change), anomalies, warnings, API
calls and quota left, and writes nothing (`PlanningStore` rejects every write but `upsert`, which
it only counts). `--apply` reuses the dry run's responses (no extra calls), writes, applies the
overrides and prints `BACKFILL_APPLIED season=S [week=W] ...`. It is refused (exit 2) when the
estimate exceeds `--max-rows-written` (default 40,000; a fresh full season is about 19,000). Exit
1 on a fetch/ingest failure, an empty fetch, or any `OVERRIDE_FAILURE`. A rerun of an unchanged
season estimates 1 row (the season's `last_ingested_at`).

## Secrets (names only)

`CBBD_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_D1_DATABASE_ID`, `CLOUDFLARE_API_TOKEN`; later
tasks add `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`,
`PERSONAL_SITE_DEPLOY_HOOK_URL`. Never log or commit values. In cloud agent sessions the token
variable may hold the placeholder `proxy-injected`; a proxy supplies the real credential.

## Shared Workflow Framework

This repo imports a shared agent workflow at `.agents/` (git subtree). Read `.agents/AGENTS.md`
before starting substantial work; invoke it with `/work`.
