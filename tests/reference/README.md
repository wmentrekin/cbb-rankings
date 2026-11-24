# Frozen football reference

Yardstick for the basketball port: a vendored copy of the football optimization
and golden outputs produced by the real football code. Nothing under
`tests/reference/` is imported by production code, and nothing here imports
`cfb-rankings`. It is re-frozen only when football itself changes (last: football
40a6b4f, which replaced the 8ab8201 copy).

## Provenance

- Repository: `cfb-rankings`, commit `40a6b4f` ("Merge pull request #23 ..."). What changed in
  the model since 8ab8201: the margin scale reads real margins (`model/margin_scale.py`; the alpha
  indexing bug is fixed), constants moved to `model/params.py`, no lower bound on team ratings in
  either solve (`r_min=None`), published ratings shifted so the lowest team reads 0.01
  (`shift_to_display`), a `two_sided_margin` option (False in production), and the prior model
  guards its empty FCS-loss sum. `process_data.py`, `connectivity.py`, `game_derivations.py` and the
  ingest derivation in `get_games.py` are unchanged.
- Stack that produced the goldens (pinned in `pyproject.toml` / `uv.lock`): cvxpy 1.8.1, numpy
  2.4.2, pandas 2.3.3, networkx 3.6.1, scipy 1.17.0, clarabel 0.11.1, osqp 1.1.1, scs 3.2.11;
  Python 3.12.3. Football's own extra imports (sqlalchemy 2.0.46, python-dotenv 1.2.1) were
  supplied ephemerally by `uv run --with` and are not dependencies of this repository.
- Solver CVXPY chose by default (no `solver=` argument) for both the prior-season and the main
  problem: **OSQP** (`problem.solver_stats.solver_name`), recorded in the goldens with the status
  and iteration count of every solve. Football never names a solver.

| Reference (`football_reference.py`) | Football source (40a6b4f) |
| --- | --- |
| `ModelParams`, `DEFAULT_PARAMS`, `LEGACY_FLOOR_PARAMS`, `shift_to_display` | `model/params.py:12-56` |
| `median_margin`, `compute_k_margin` (+ constants) | `model/margin_scale.py:21-50` |
| `compute_margin`, `compute_winner`, `compute_alpha` | `database/game_derivations.py:10-49` |
| `add_derived_columns` | `database/get_games.py:97-108` (the ingest `apply` calls) |
| `compute_connectivity_index` | `model/connectivity.py:4-30` |
| `process_game_data` | `model/process_data.py:7-102` |
| `get_games_by_year_week`, `get_fbs_teams_by_year` | `model/process_data.py:104-146` (SQL, plus `rankings_games` = `season_type = 'regular'` from `database/migrations/0001_rankings_games_view.sql`) |
| `get_data_by_year_up_to_week` | `model/process_data.py:148-165` |
| `get_prior_ratings` | `model/prior_model.py:10-136` |
| `get_ratings` | `model/model.py:11-160` |

### Edits (the complete list)

1. Inputs are in-memory frames: the two SQL reads become filters over whole-table frames
   (`season`, `week <= week` when `week` is truthy, `season_type == 'regular'`). Derived
   columns `margin`/`winner`/`alpha`, which football stores at ingest, are computed with the
   copied derivations on the raw fixture rows.
2. `get_ratings(..., params=DEFAULT_PARAMS, prior_ratings=None, solver=None)`: priors are passed in,
   or computed by calling the reference prior solve on the same frames (with the same `params`).
   A passed dict is copied (football adds default entries to its own dict).
3. Both solves also return a `details` dict: objective value, the four term values at the
   solution (`margin_penalty`, `loss_rate_penalty`, `fcs_loss_penalty`, `prior_term`), the dummy
   rating `r_fcs`, the solver name, lambda, `k_margin` and data counts; `get_ratings`'s also carries
   `raw_ratings`, the solve before `shift_to_display` (the returned `ratings` are shifted, as in
   football). The returned `ratings` still contain the `"fcs"` entry as in football.
4. Optional `solver=` (default `None`: `problem.solve()` exactly as football calls it; football's
   weekly solve passes `verbose=True`, which only prints).
5. `print`, `verbose=True` and logging removed; the unused diagnostics block in
   `compute_connectivity_index` removed. The "strict violations" loops were removed (print-only).
   Because the term values are read into `details`, an unguarded empty sum raises its
   `AttributeError` there instead of in football's diagnostic `print` (same exception, same cause).

Everything else, including variable names, constants, loop structure, the `two_sided_margin`
branch and the remaining unguarded empty sums, is copied literally.

## Layout

- `football_reference.py`: the frozen copy.
- `generate_goldens.py`: runs the REAL football modules against the fixture and writes
  `goldens/football_goldens.json`. Regenerate with
  `uv run --with sqlalchemy==2.0.46 --with python-dotenv==1.2.1 python tests/reference/generate_goldens.py --football <path to cfb-rankings @ 40a6b4f>`
  (or `CFB_RANKINGS_PATH`). Football's only database readers (`get_games_by_year_week`,
  `get_fbs_teams_by_year` in `model/process_data.py`) are replaced by in-memory readers;
  `sqlalchemy.create_engine` raises, so no connection is possible. The script refuses to run
  unless the checkout is clean at 40a6b4f.
- `measure_solvers.py`: solver timing/agreement through the reference (see below).
- `fixture_io.py`: loaders shared by the tests.
- `../fixtures/`: `make_football_fixture.py` (seeded), `football_teams.csv`, `football_games.csv`.
- Tests: `tests/test_reference_matches_football.py`, `test_reference_derivations.py`,
  `test_reference_fixture_coverage.py`.

What the goldens hold per scenario: `ratings` (football's return value: PUBLISHED, shifted),
`raw_ratings` (the solve, read from the solved problem's variables the way football reads them),
`display_shift`, `lowest_team`, the assembled data, records, and for the main solve the objective,
the four terms, `r_fcs`, solver, status, iterations, median margin and `k_margin`. The prior season
holds the RAW prior ratings (football never shifts them; the generator asserts they equal the prior
problem's variables). Football's four term values cannot be read from its `Problem` (cvxpy flattens
the sum), so the goldens evaluate them with plain Python at football's RAW solution, from football's
own assembled data, `k` from football's own `compute_k_margin` and constants from its
`DEFAULT_PARAMS`; the script asserts their sum equals `problem.value` (1e-7 relative).

## Fixture

Two fictional seasons in football's table shape: 2023 (prior; 15 teams, 66 games) and 2024
(current; 15 teams, 69 games incl. one week-16 postseason row that the `rankings_games` view
excludes). Oakhurst is FCS in 2023 and FBS in 2024 (absent from the priors, default prior 1);
Redfern is FBS in 2023 only. Raw columns only: scores, `neutral_site`, week, season, season_type.

Regenerated for 40a6b4f with one change: the scripted blowout (Glenrock over Northgate, week 5) is
183-3, a 180-point margin, instead of 91-3. With real margins the cap binds above median x (20/7)^2,
about 8.2 times the median margin (13 to 21 points here, so 106 to 172), and the old 88-point game
capped only under football's former alpha-median bug. Scripted scores do not consume the seeded
random stream, so every other row is identical.

Coverage (asserted by `test_reference_fixture_coverage.py`): weeks 1 (lambda 6/700), 3, 6, 7 (first
lambda 0), 10, 15; Larkspur has no game before week 4; neutral, home-win and away-win games; a
Carlow/Dunmore rematch (weeks 2 and 6); a capped 180-point game in every scenario from week 5 on and
1-2 point games; six FBS losses to FCS (margins 14, 21, 10, 12, 3 and different sites: home, away,
neutral) and FBS wins over FCS; Ashford undefeated through week 10 (first loss week 12); winless
Northgate; 0-0 unplayed games at week 3 (FBS vs FCS) and week 7 (FBS vs FBS). The lowest-rated team,
which sets the published shift, changes between scenarios: Kingsley (w01), Eastvale (w03), Northgate
(w06 onward).

### Uniqueness, per scenario

With no floor nothing sits at a bound any more: every team is interior (the bottom of every scenario
is below zero). Which ratings are comparable is unchanged in kind. A team with a loss is fixed by
its loss-rate term; with lambda > 0 the prior term fixes every team. A team with no losses has no
strictly convex term in its own rating once lambda is 0 (undefeated: only one-sided hinges, so any
higher rating is also optimal; no games: nothing, so any value up to 100 is optimal), so its rating
is a flat direction and solver-dependent; only objective and term values are comparable for it. The
tests skip exactly those teams (`non_unique_teams` in the goldens). The dummy rating `fcs` is a flat
direction when the lower-division term is 0 at the solution (w01: no lower-division loss yet, OSQP
returns 0.016); in every other scenario it is 15.0 (the bound) with an active hinge.

PUBLISHED ratings are unique only if the shift is, that is, if the lowest team is a unique one. The
generator asserts it in every scenario (the lowest team always has a loss or lambda > 0). In
general it need not hold: an unbeaten team with no D-I win, or a team with no games, at lambda 0 can
take any value up to 100, and a solver could place it lowest; every published rating would then be
solver-dependent (quirk 16).

| Scenario (2024) | lambda | lowest team | Ratings unique? | Teams excluded from rating comparison |
| --- | --- | --- | --- | --- |
| w01 | 6/700 | Kingsley | all (prior term) | none (the dummy `fcs` is flat) |
| w03 | 4/700 | Eastvale | all (prior term is strictly convex for every team, including Larkspur who has no games) | none |
| w06 | 1/700 | Northgate | all | none |
| w07 | 0 | Northgate | all but flat teams | Ashford, Kingsley, Oakhurst (zero losses) |
| w10 | 0 | Northgate | all but flat teams | Ashford, Oakhurst |
| w15 | 0 | Northgate | all | none (every team has a loss) |
| prior season (2023) | none | Northgate | all (every team has a loss) | none |

Empirical check (reference, same fixture, raw ratings of unique teams against the goldens): OSQP
re-run is bit-identical; CLARABEL 2.9e-5 (w01), 2.9e-7 (w03), 1.4e-7 (w06), 6.7e-5 (w07), 2.0e-4
(w10), 6.6e-7 (w15); SCS 9.1e-4, 4.0e-4, 1.0e-4, 6.7e-5, 2.0e-4, 7.5e-7. The excluded teams differ
by up to 75.9 (CLARABEL, w07) and 80.1 (w10), which is the flat direction. The shift agrees across
solvers because the lowest team is unique. `test_unique_ratings_do_not_depend_on_the_solver` keeps
this evidence in CI (CLARABEL vs OSQP, raw and published).

## Known quirks (reproduced, not fixed)

Each is exercised by a test where it can be (the real code's behavior is in `goldens` under
`quirks`). Fixed in football since 8ab8201 and so removed from this list: the "median margin" was
the median of alpha (now `model/margin_scale.py`, real margins), and the prior model crashed on a
season with no lower-division loss (now guarded; `prior_without_fcs_losses` in the goldens records
that it solves).

1. **`week=None` crashes**: `model/model.py:52` (`week < 7`) raises `TypeError` (`'<' not supported
   between instances of 'NoneType' and 'int'`), although the signature and docstring say `None` is
   allowed, and only AFTER the prior-season solve has run (`model.py:31`; the goldens record one
   solved problem before the exception). **`week=0`** is falsy: all games are read
   (`process_data.py:120,162`) but lambda is `7/700`.
2. **Remaining unguarded empty sums.** `cp.sum([])` returns the Python int `0`, whose `.value` does
   not exist: the margin and loss-rate sums (`model/model.py:93,104`, `model/prior_model.py:73,84`)
   are still unguarded (only the FCS sums are), so a window with no FBS vs FBS played game, or no
   played game at all, raises `AttributeError` in the diagnostics after a successful solve
   (`no_d1_games` in the goldens).
3. **The 0-0 unplayed sentinel**: `game_derivations.py:22-45` gives winner `None` for 0-0, and
   `process_data.py:43` skips it; but the game's pair is still added to `game_pairs`
   (`process_data.py:34`), so it counts toward the connectivity index (a diagnostic only).
   A real 0-0 final would also be dropped.
4. **Ties are away wins.** `game_derivations.py:45` returns the away team when scores are equal and
   non-zero, so a tie becomes a win for the away team with margin 0; the `[wins, losses, ties]`
   record's tie slot is never incremented. A tie between an FBS home team and an FCS away team is a
   recorded loss for the FBS team with no lower-division penalty (the loss detection at
   `process_data.py:84` needs a strict `<`). A 0-margin game enters the median of margins.
5. **No prior season, no run**: with no teams in the prior season `compute_connectivity_index`
   (`connectivity.py:23`) raises `ValueError` (`max()` of an empty sequence), so the first season
   of any dataset cannot be rated.
6. **Lower-division term is uncapped**: it uses the raw margin times alpha, while D-I games use the
   capped, sqrt-scaled margin (`model.py:74` vs `:79-92`).
7. **The dummy and team names share a namespace**: variables are named `r_{team}` and read back by
   name (`model.py:126-133`), so a team named `fcs` would collide with the dummy; the dummy's rating
   is returned inside `ratings["fcs"]`. Priors from the prior solve include that `fcs` entry too.
   `shift_to_display` (`params.py:52`) leaves out the key `"fcs"` when it looks for the lowest team,
   so a TEAM named `fcs` would also be ignored there.
8. **The prior solve runs for every week**, including weeks >= 7 where lambda is 0 and the priors
   are unused (`model.py:31`); it reads all regular-season games of the prior year with no cutoff.
9. **Status handling**: only "infeasible" and "unbounded" are rejected. `optimal_inaccurate`
   passes through (OSQP accuracy is about 1e-5 relative; at w07 and w10 OSQP's `r_fcs` is 15.00005
   and 15.00013, slightly past its bound), and the prior model on an infeasible status would hit
   `UnboundLocalError` (`prior_model.py:136`); not reproduced exactly because the reference reads
   term values first.
10. **Row order** is whatever the SQL returns (no `ORDER BY`): it only changes the `k` counter
    (unused by the objective) and floating-point summation order.
11. **Flat directions at lambda = 0** (above): the reported rating of an undefeated team is whatever
    the default solver stops at.
12. **No floor: the solve runs negative.** With `r_min=None` the bottom of every solve is below zero
    (w15: Northgate -3.45; prior season: Northgate -3.00). The loss-rate penalty `(loss_rate * r)^2`
    anchors the scale at zero, so a team with no loss and no D-I win (or no game) at lambda 0 has the
    optimal set (-inf, 100]; OSQP returns about 0 for it. The dummy FCS team keeps its 0.01..15
    bounds, now on a scale whose teams can be negative. The redundant `r >= 0` constraints of the
    prior model (`prior_model.py:99-101`) are skipped when `r_min` is None.
13. **Published ratings depend on the lowest team.** `shift_to_display` adds `0.01 - min(team
    ratings)` to every rating, so one game of the worst team moves every published rating by the
    same amount (ranks and gaps are unchanged). Between scenarios the shift here goes from 0.648
    (w01) to 3.462 (w15).
14. **Priors are raw, published ratings are shifted.** `get_prior_ratings` returns the raw solve
    (never shifted) and `get_ratings` regularizes toward it, while what football stores is shifted;
    the two scales differ by that season's shift. New FBS teams get prior 1 on the raw scale.
    The stored ratings are never read back as priors (the prior season is re-solved from games).
15. **The dummy moves with the shift**: `ratings["fcs"]` is published shifted (15 + shift), but
    `ratings_to_df` drops it, so it is never stored.
16. **The shift is unique only if the lowest team is.** If a flat team (no loss at lambda 0) were
    the lowest, the shift, and so every published rating, would be whatever the solver chose for it.
    Not the case in any scenario here or on the real basketball data checked.
17. **`two_sided_margin`** (`params.py:35`, `model.py:89-91`) is False in production; True would
    penalise a winner that is too far above the loser as well. Copied, not exercised by the goldens.

## Solver comparison (`measure_solvers.py`; one run against 40a6b4f, Python 3.12.3, this sandbox's CPU)

Fixture (15 teams): every solver finishes a whole `get_ratings` call (prior solve + main solve +
assembly) in 0.17 to 0.33 s. Against OSQP, unique ratings: CLARABEL 2.9e-7 (w03), 1.7e-4 (w10),
6.3e-7 (w15); SCS 4.6e-4, 1.7e-4, 6.7e-7. Objective relative difference CLARABEL 5.9e-9, 1.0e-5,
8.4e-12. At w10 OSQP's objective is the LOWER one by 1.0e-5 relative because its solution is
slightly infeasible (`r_fcs` 15.0001 > 15): this is why cross-solver parity at 1e-6 is not expected
and the parity tests pin the golden solver, OSQP.

Synthetic basketball-sized season (seed 7: 365 teams, 5,800 rated D-I games over 19 weeks, 1,500
lower-division games of which 12 are D-I losses, prior season of the same size), one call = prior
solve + main solve with the scalar-built football formulation: 20 to 34 s for every solver, almost
all of it CVXPY canonicalization of the ~5,800 `pos(...)**2` terms ("Objective contains too many
subexpressions"); the port's vectorized build solves the same problems in well under a second.
Unique ratings against OSQP: CLARABEL 1.2e-6 (week 3), 1.8e-4 (week 10), 1.3e-6 (week 19); SCS
7.2e-4, 3.9e-4, 1.2e-5. With the floor removed OSQP reaches `optimal` on the synthetic prior season
(at 8ab8201, with the floor, it hit its 10,000-iteration cap there), and on every week of the real
2025-26 and 2026-27 data (`model/README.md`).

The port runs OSQP, named (owner decision 2026-11-24: football's solver), with a CLARABEL fallback
when OSQP does not reach `optimal`.
