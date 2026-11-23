# AGENTS.md

## Project

Weekly batch pipeline (GitHub Actions) that ranks NCAA Division I men's basketball teams with a
convex quadratic program ported from `cfb-rankings`: ingest games from CollegeBasketballData
(CBBD) into Cloudflare D1, solve, publish JSON to R2. Only storage exists so far; ingestion
(`ingest/`), the model (`model/`) and publishing (`artifacts/`) arrive in later batches. Plan:
`docs/cbb-rankings-port/`. Source behaviour: `docs/source-probe.md`.

## Layout

- `database/migrations/` - D1 schema, plain SQLite SQL, applied in name order
- `database/store.py` - the `Store` interface, upsert SQL builder, chunking, identifier checks
- `database/d1.py` - `D1Client`: REST client (retries, batch, `/raw` reads)
- `database/local.py` - `LocalStore`: same interface on `sqlite3`, applies the migrations
- `database/__init__.py` - `get_store()`, chosen by `CBB_STORE` (`d1` default, `local`)
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

## Secrets (names only)

`CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_D1_DATABASE_ID`, `CLOUDFLARE_API_TOKEN`; later tasks add
`CBBD_API_KEY`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`,
`PERSONAL_SITE_DEPLOY_HOOK_URL`. Never log or commit values. In cloud agent sessions the token
variable may hold the placeholder `proxy-injected`; a proxy supplies the real credential.

## Shared Workflow Framework

This repo imports a shared agent workflow at `.agents/` (git subtree). Read `.agents/AGENTS.md`
before starting substantial work; invoke it with `/work`.
