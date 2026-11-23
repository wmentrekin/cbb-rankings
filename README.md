# cbb-rankings

A convex quadratic program that ranks NCAA Division I men's basketball teams from game outcomes,
ported from `cfb-rankings`. A weekly GitHub Actions job ingests games from
[CollegeBasketballData](https://collegebasketballdata.com) into Cloudflare D1, solves the model,
and publishes JSON rankings to R2 for the site.

Status: storage layer only (schema, migrations, D1 client). Ingestion, the model and publishing
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

## Setup

```
uv sync
uv run pytest -q
```

Copy `.env.example` to `.env` for local runs. Secrets (set in the repository settings for
Actions): `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_D1_DATABASE_ID`, `CLOUDFLARE_API_TOKEN`
(D1 edit permission). Use `CBB_STORE=local` to run against a local sqlite file instead.

## Migrations

Add `database/migrations/NNNN_description.sql`, then run the "Apply D1 Migrations" workflow
(manual), or locally `npx wrangler d1 migrations apply cbb-rankings --remote` with
`CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` set. Applied migrations are tracked in the
`d1_migrations` table and are never edited.

## Workflows

- `tests.yml` - pytest on every push and pull request
- `apply-migrations.yml` - manual D1 migration apply
