# Frozen football reference

Yardstick for the basketball port: a vendored copy of the football optimization
and golden outputs produced by the real football code. Nothing under
`tests/reference/` is imported by production code, and nothing here imports
`cfb-rankings`.

## Provenance

- Repository: `cfb-rankings`, commit `8ab8201` ("Merge pull request #21 ...").
- Stack that produced the goldens (pinned in `pyproject.toml` / `uv.lock`, identical
  to football's `uv.lock`): cvxpy 1.8.1, numpy 2.4.2, pandas 2.3.3, networkx 3.6.1,
  scipy 1.17.0, highspy 1.13.1, clarabel 0.11.1, osqp 1.1.1, scs 3.2.11; Python 3.12.3.
  Football's own extra imports (sqlalchemy 2.0.46, python-dotenv 1.2.1) were supplied
  ephemerally by `uv run --with` and are not dependencies of this repository.
- Solver CVXPY chose by default (no `solver=` argument) for both the prior-season and
  the main problem: **OSQP** (`problem.solver_stats.solver_name`), recorded in the goldens.
  The goldens and the parity tests use it. Football never names a solver.

| Reference function (`football_reference.py`) | Football source (8ab8201) |
| --- | --- |
| `compute_margin`, `compute_winner`, `compute_alpha` | `database/game_derivations.py:10-49` |
| `add_derived_columns` | `database/get_games.py:97-108` (the ingest `apply` calls) |
| `compute_connectivity_index` | `model/connectivity.py:4-30` |
| `process_game_data` | `model/process_data.py:7-102` |
| `get_games_by_year_week`, `get_fbs_teams_by_year` | `model/process_data.py:104-146` (SQL, plus `rankings_games` = `season_type = 'regular'` from `database/migrations/0001_rankings_games_view.sql`) |
| `get_data_by_year_up_to_week` | `model/process_data.py:148-165` |
| `get_prior_ratings` | `model/prior_model.py:8-135` |
| `get_ratings` | `model/model.py:9-158` |

### Edits (the complete list)

1. Inputs are in-memory frames: the two SQL reads become filters over whole-table frames
   (`season`, `week <= week` when `week` is truthy, `season_type == 'regular'`). Derived
   columns `margin`/`winner`/`alpha`, which football stores at ingest, are computed with the
   copied derivations on the raw fixture rows.
2. `get_ratings(..., prior_ratings=None)`: priors are passed in, or computed by calling the
   reference prior solve on the same frames. A passed dict is copied (football adds default
   entries to its own dict).
3. Both solves also return a `details` dict: objective value, the four term values at the
   solution (`margin_penalty`, `loss_rate_penalty`, `fcs_loss_penalty`, `prior_term`),
   the dummy rating `r_fcs`, the solver name, lambda, and data counts. The returned
   `ratings` still contain the `"fcs"` entry as in football.
4. Optional `solver=` (default `None`: `problem.solve()` exactly as football calls it).
5. `print`, `verbose=True` and logging removed; the unused diagnostics block in
   `compute_connectivity_index` removed. The "strict violations" loop was removed (print-only).

Everything else, including variable names, constants, loop structure and the (unguarded)
empty sums, is copied literally.

## Layout

- `football_reference.py`: the frozen copy.
- `generate_goldens.py`: runs the REAL football modules against the fixture and writes
  `goldens/football_goldens.json`. Regenerate with
  `uv run --with sqlalchemy==2.0.46 --with python-dotenv==1.2.1 python tests/reference/generate_goldens.py --football <path to cfb-rankings @ 8ab8201>`
  (or `CFB_RANKINGS_PATH`). Football's only database readers (`get_games_by_year_week`,
  `get_fbs_teams_by_year` in `model/process_data.py`) are replaced by in-memory readers;
  `sqlalchemy.create_engine` raises, so no connection is possible. The script refuses to run
  unless the checkout is clean at 8ab8201.
- `measure_solvers.py`: solver timing/agreement through the reference (see below).
- `fixture_io.py`: loaders shared by the tests.
- `../fixtures/`: `make_football_fixture.py` (seeded), `football_teams.csv`, `football_games.csv`.
- Tests: `tests/test_reference_matches_football.py`, `test_reference_derivations.py`,
  `test_reference_fixture_coverage.py`.

Football's four term values cannot be read from its `Problem` (cvxpy flattens the sum), so
the goldens evaluate them with plain Python at football's returned solution, from football's
own assembled data, and the script asserts their sum equals `problem.value` (1e-7 relative).

## Fixture

Two fictional seasons in football's table shape: 2023 (prior; 15 teams, 66 games) and 2024
(current; 15 teams, 69 games incl. one week-16 postseason row that the `rankings_games` view
excludes). Oakhurst is FCS in 2023 and FBS in 2024 (absent from the priors, default prior 1);
Redfern is FBS in 2023 only. Raw columns only: scores, `neutral_site`, week, season, season_type.

Coverage (asserted by `test_reference_fixture_coverage.py`): weeks 3 (lambda 4/700), 6, 7
(first lambda 0), 10, 15; Larkspur has no game before week 4; neutral, home-win and away-win
games; a Carlow/Dunmore rematch (weeks 2 and 6); an 88-point game and 1-2 point games; six
FBS losses to FCS (margins 14, 21, 10, 12, 3 and different sites: home, away, neutral) and
FBS wins over FCS; Ashford undefeated through week 10 (first loss week 12); winless Northgate;
0-0 unplayed games at week 3 (FBS vs FCS) and week 7 (FBS vs FBS).

### Uniqueness, per scenario

A team with no losses has no strictly convex term in its own rating once lambda is 0
(undefeated: only one-sided hinges, so any higher rating is also optimal; no games: nothing),
so its rating is a flat direction and solver-dependent; only objective and term values are
comparable for it. The tests skip exactly those teams (`non_unique_teams` in the goldens).
The dummy rating `fcs` is also a flat direction when the lower-division term is 0 at the
solution (no active hinge); in all five scenarios it is 15.0 (the bound) with an active hinge.

| Scenario (2024) | lambda | Ratings unique? | Teams excluded from rating comparison |
| --- | --- | --- | --- |
| w03 | 4/700 | all (prior term is strictly convex for every team, including Larkspur who has no games) | none |
| w06 | 1/700 | all | none |
| w07 | 0 | all but flat teams | Ashford, Kingsley, Oakhurst (zero losses) |
| w10 | 0 | all but flat teams | Ashford, Oakhurst |
| w15 | 0 | all | none (every team has a loss) |
| prior season (2023) | none | all (every team has a loss; checked) | none |

Empirical check (reference, same fixture): OSQP re-run twice is bit-identical (max diff 0.0);
CLARABEL vs OSQP, ratings of unique teams: max diff 2.6e-6 (w03), 1.3e-7 (w06), 1.7e-5 (w07),
3.6e-5 (w10), 9.1e-8 (w15); SCS vs OSQP: 1.8e-4, 5.6e-4, 4.0e-5, 1.2e-4, 5.2e-7. The excluded
teams differ by up to 38.7 (CLARABEL, w07) and 41.6 (w10), which is the flat direction.
`test_unique_ratings_do_not_depend_on_the_solver` keeps this evidence in CI (CLARABEL vs OSQP).

## Known quirks (reproduced, not fixed)

Each is exercised by a test where it can be (the real code's behavior is in `goldens` under `quirks`).

1. **The "median margin" is the median of alpha.** `model/model.py:63` and
   `model/prior_model.py:44` unpack the game tuple `(i, j, k, winner, margin, alpha, week, season)`
   as `(_, _, _, _, _, m, _, _)`: `m` is index 5, `alpha`, not the margin (index 4). So
   `median_margin` is one of 0.8, 0.9, 1.0, 1.1, 1.2 and `k_margin = 7 / sqrt(median)` is about
   6.4 to 7.8 regardless of the scoring distribution; the 20-point cap binds from a margin of
   about 7 to 10 (not about 80, as a true median margin of 7 to 10 points would imply). The goldens use football's actual values
   (median alpha 0.8 at weeks 3, 7, 10, 15; 1.0 at week 6). The plan's wording
   "median margin and k computed over D-I vs D-I games" is true of the population, not of the quantity.
2. **`week=None` crashes**: `model/model.py:53` (`None < 7`) raises `TypeError`, although the
   signature (line 9) and docstring say `None` is allowed. **`week=0`** is falsy: all games are
   read (`process_data.py:120,162`) but lambda is `7/700`.
3. **Unguarded empty sums.** `cp.sum([])` returns the Python int `0`, whose `.value` does not exist:
   `model/prior_model.py:59,74,85` (and `model/model.py:96,107`; line 81 is guarded) make the
   diagnostics at `prior_model.py:131-133` / `model.py:151,153` raise `AttributeError` after a
   successful solve whenever a season has no lower-division loss (prior model), no D-I vs D-I
   game, or no played game. A season with no lower-division loss therefore cannot be a prior season (only the prior-model case is exercised in the tests).
4. **The 0-0 unplayed sentinel**: `game_derivations.py:22-45` gives winner `None` for 0-0, and
   `process_data.py:43` skips it; but the game's pair is still added to `game_pairs`
   (`process_data.py:34`), so it counts toward the connectivity index (a diagnostic only).
   A real 0-0 final would also be dropped.
5. **Ties are away wins.** `game_derivations.py:45` returns the away team when scores are equal and
   non-zero, so a tie becomes a win for the away team with margin 0; the `[wins, losses, ties]`
   record's tie slot is never incremented. A tie between an FBS home team and an FCS away team is a
   recorded loss for the FBS team with no lower-division penalty (the loss detection at
   `process_data.py:84` needs a strict `<`).
6. **No prior season, no run**: with no teams in the prior season `compute_connectivity_index`
   (`connectivity.py:23`) raises `ValueError` (`max()` of an empty sequence), so the first season
   of any dataset cannot be rated.
7. **Lower-division term is uncapped**: it uses the raw margin times alpha, while D-I games use the
   capped, sqrt-scaled margin (`model.py:80` vs `:86-95`).
8. **The dummy and team names share a namespace**: variables are named `r_{team}` and read back by
   name (`model.py:128-135`), so a team named `fcs` would collide with the dummy; the dummy's
   rating is returned inside `ratings["fcs"]`. Priors from the prior solve include that `fcs` entry too.
9. **Redundant prior-model constraints** (`prior_model.py:97-100`, `r >= 0` and the dummy's bounds
   repeated); harmless.
10. **The prior solve runs for every week**, including weeks >= 7 where lambda is 0 and the priors
    are unused (`model.py:29`); it reads all regular-season games of the prior year with no cutoff.
11. **Status handling**: only "infeasible" and "unbounded" are rejected. `optimal_inaccurate`
    passes through (OSQP accuracy is about 1e-5 relative, see below), and the prior model on an
    infeasible status would hit `UnboundLocalError` (`prior_model.py:135`); not reproduced exactly
    because the reference reads term values first.
12. **Row order** is whatever the SQL returns (no `ORDER BY`): it only changes the `k` counter
    (unused by the objective) and floating-point summation order.
13. **Flat directions at lambda = 0** (above): the reported rating of an undefeated team is whatever
    the default solver stops at.

## Solver comparison (`measure_solvers.py`; one run, Python 3.12.3, this sandbox's CPU)

Fixture (15 teams): every solver finishes a whole `get_ratings` call (prior solve + main solve +
assembly) in 0.17 to 0.29 s. CLARABEL agrees with OSQP on unique ratings to 2.6e-6 (w03),
3.6e-5 (w10), 9.1e-8 (w15); objective relative difference 2.4e-10, 7.1e-6, 1.5e-10 (OSQP is the
less accurate one at w10, where its objective is higher by 7.1e-6 relative; this is why
cross-solver parity at 1e-6 is not expected and the parity tests pin the golden solver, OSQP).

Synthetic basketball-sized season (seed 7: 365 teams, 5,800 rated D-I games over 19 weeks, 1,500
lower-division games of which 12 are D-I losses, prior season of the same size), week 19, one call
= prior solve + main solve:

| Solver | whole call | prior solve: solver time / iterations | main solve: solver time / iterations | max rating diff vs OSQP | objective rel diff vs OSQP |
| --- | --- | --- | --- | --- | --- |
| OSQP (football's implicit default) | 36.5 s | 5.4 s / 10,000 (the iteration cap) | 0.26 s / 575 | 0 | 0 |
| CLARABEL | 43.7 s (34.3 s in a second run) | 0.23 s / 14 | 0.20 s / 15 | 7.6e-6 | 1.8e-11 |
| SCS | 48.2 s | 9.2 s / 12,425 | 0.37 s / 500 | 4.3e-3 | 5.1e-9 |

About 12 to 17 s of each problem is cvxpy canonicalization of the ~5,800 `pos(...)**2` terms and is
independent of the solver (cvxpy also warns "Objective contains too many subexpressions"); the
solver itself is a quarter second except for OSQP and SCS on the prior solve. At earlier weeks of the
synthetic season (3 and 10), unique ratings differ from OSQP by up to 5.9e-3 (CLARABEL) and
1.3e-2 (SCS), with objective differences about 5e-6: OSQP's default tolerance, not the other
solvers, is the loose one (cvxpy warned "Solution may be inaccurate" for OSQP at this size).

Recommendation for the port: name **CLARABEL** explicitly. It is not football's implicit default
(OSQP); it converges in about 15 interior-point iterations at both sizes, and OSQP hits its
10,000-iteration cap on the 365-team prior solve. The parity run must still pass
`solver="OSQP"` (the solver that produced the goldens), as the requirement says.
