# model

The ratings optimization, ported from `cfb-rankings` @ 8ab8201 (`model/model.py`,
`model/prior_model.py`, `model/process_data.py`). The methodology is unchanged; constants and
sport labels are configuration (`model/config.py`).

## Formulation

One rating `r_t` per D-I team `t` and one shared rating `r_L` for every non-D-I opponent.
Minimize

```
  gamma_margin * sum over D-I games g      pos(r_loser + d_g - r_winner)^2
+ gamma_loss   * sum over D-I teams t      (loss_rate_t * r_t)^2
+ gamma_lower  * sum over D-I losses l to non-D-I   pos(r_team + margin_l * alpha_l - r_L)^2
+ lambda(week) * sum over D-I teams t      (r_t - prior_t)^2

d_g = min(k * sqrt(margin_g), max_gap_from_margin) * alpha_g
k   = target_gap_for_median / max(sqrt(median), sqrt_floor)
lambda(week) = (prior_fade_weeks - week) / prior_fade_scale   for week < prior_fade_weeks, else 0

r_min <= r_t <= r_max        r_lower_min <= r_L <= r_lower_max
```

`pos(x) = max(x, 0)`. `loss_rate_t = losses / (wins + losses)` over every played game of the
team, including games against non-D-I teams (a D-I win over a non-D-I team is a record-only
event). `alpha` is stored per game at ingest (1.0 neutral, 0.8 home win, 1.2 away win, from
`ingest.derive.DEFAULT_ALPHA`); the model reads it and never recomputes it. The lower-division
term uses the RAW margin times alpha (no square root, no scale, no cap).

Among the optimal solutions, the published one is chosen by `flat_rating_rule` (see "Undetermined
ratings" below): production publishes the one with the least rating for every team the objective
leaves undetermined.

Prior ratings come from the same program run on the previous season's rated games with the prior
term off (and the same `flat_rating_rule`). Teams absent from the prior season get
`default_prior`; prior-season teams absent now are ignored. It is skipped when lambda is 0
(week >= `prior_fade_weeks`), since it cannot change the result.

## Configuration

| Field | Football (`FOOTBALL_COMPAT`) | `BASKETBALL_DEFAULT` |
| --- | --- | --- |
| `r_min`, `r_max` | 0.01, 100 | same |
| `r_lower_min`, `r_lower_max` | 0.01, 15 (football `r_fcs_*`) | same |
| `gamma_margin`, `gamma_loss`, `gamma_lower` | 0.05, 0.5, 5 | same |
| `target_gap_for_median`, `max_gap_from_margin` | 7.0, 20.0 | same |
| `median_fallback`, `sqrt_floor` | 1.0, 1e-6 | same |
| `prior_fade_weeks`, `prior_fade_scale` | 7, 700 | same |
| `default_prior` | 1 | same |
| `alpha` | `ingest.derive.DEFAULT_ALPHA` (1.0 / 0.8 / 1.2) | same |
| `accept_inaccurate` | False | False |
| `solver` | OSQP (CVXPY's implicit choice for the goldens) | CLARABEL |
| `margin_scale_basis` | `"alpha"` | `"margin"` (owner decision 2026-11-23) |
| `flat_rating_rule` | `"solver"` | `"min_feasible"` |

`tests/test_model_config.py` pins every football value and asserts `BASKETBALL_DEFAULT` differs
from `FOOTBALL_COMPAT` exactly in `solver`, `margin_scale_basis` and `flat_rating_rule`, so a
retune is a visible diff.

## The margin-scale basis (reference README, quirk 1)

The documented formula takes `median` over the margins of victory of D-I vs D-I played games.
Football's code unpacks its game tuple at the wrong index, so its `median` is the median of the
per-game ALPHA values (0.8 to 1.2), giving `k` about 6.4 to 7.8 whatever the scores are, and the
20-point cap binds from a margin of about 7 to 10 points.

`margin_scale_basis` selects which quantity feeds `median`:

- `"alpha"`: football's actual behavior, bit for bit (parity with the goldens).
- `"margin"`: the documented formula. The median is over the played D-I vs D-I games in the data
  window (through the requested week), `median_fallback` when there are none, and the `sqrt_floor`
  applies as in football.

Owner decision (2026-11-23): basketball uses the documented formula, the true median margin
(`"margin"`), and football is being fixed separately to match. The choice is made in exactly one
place, `BASKETBALL_MARGIN_SCALE_BASIS` at the top of `model/config.py`. Nothing else depends on it
(tests that need a basis name it explicitly). `SolveResult` reports `median_used`, `k_margin` and `n_capped` so the
effect is visible per run.

## Intentional differences from cfb-rankings @ 8ab8201

Numbers refer to "Known quirks" in `tests/reference/README.md`.

| # | Football | Here |
| --- | --- | --- |
| 1 | median of alpha | selectable; `"alpha"` reproduces it, `"margin"` is the documented formula (see above) |
| 2 | `week=None` crashes, `week=0` reads all games with lambda 7/700 | `week` must be an int >= 1 (`ValueError`) |
| 3 | unguarded empty sums crash after the solve | no lower-division loss, no games or no records solve normally (term value 0) |
| 4 | 0-0 sentinel for unplayed games, which still count toward connectivity | played means `winner_team_id IS NOT NULL`; connectivity counts played D-I vs D-I games only |
| 5 | ties are away wins; record has a tie slot | no ties (ingest stores them as unplayed); records are `(wins, losses)` |
| 6 | first season with no prior data raises | every team gets `default_prior`; a warning is returned |
| 8 | teams keyed by name; dummy is `ratings["fcs"]` and can collide with a team | teams keyed by integer id; the dummy is a separate variable, returned only as `SolveResult.r_lower` |
| 9 | redundant constraints | removed (same feasible set) |
| 10 | prior solve runs every week | skipped when lambda is 0 |
| 11 | any status except infeasible/unbounded accepted; prior solve can hit `UnboundLocalError` | only `optimal` accepted; anything else, or a solver error, raises `ModelSolveError`. `accept_inaccurate=True` also admits `optimal_inaccurate` |
| 12 | row order from unordered SQL | games sorted by id; results do not depend on row order |
| 13 | flat ratings of zero-loss teams at lambda 0 are whatever the solver returns | `flat_rating_rule="min_feasible"`: the least optimal value, deterministic and solver-independent (see "Undetermined ratings"); `FOOTBALL_COMPAT` keeps `"solver"` |
| solver | CVXPY's default (OSQP) | CLARABEL, named; OSQP hits its iteration cap on a 365-team prior solve |
| build | ~6,000 scalar `pos(...)**2` terms | the same terms as three matrix expressions (parity holds; build and solve fall from about 17 s to about 0.3 s per problem) |

Kept as in football: quirk 7 (the lower-division term is uncapped and unscaled), the 20-point cap
on D-I games only, D-I wins over non-D-I counted in records only.

## Undetermined ratings (reference README, quirk 13)

**Which ratings the objective determines.** Let x and y be two optimal points. The objective is
convex, so it is constant on the segment between them; a sum of convex functions that is affine
on a segment has every summand affine on it. Hence, over the whole optimal set:

- `(loss_rate_t * r_t)^2` is affine only if `loss_rate_t * r_t` is constant, so every team with a
  loss (D-I or lower-division; `gamma_loss > 0`) has ONE optimal rating;
- with lambda > 0, `(r_t - prior_t)^2` makes every rating unique;
- `pos(u)^2` with u affine is affine only if u <= 0 throughout or u is a positive constant, so the
  value of every hinge, `pos(...)`, is the same at every optimum.

So at lambda = 0 the only coordinates that can vary are the teams with no loss (including teams
with no games, or with wins over non-D-I opponents only), and `r_lower`. A team with no loss
appears in the objective only as the WINNER of its D-I games, and each loser has a loss, so it is
determined; two unbeaten teams never share a game. Fixing the determined ratings at their unique
values, the objective splits into one independent one-dimensional piece per unbeaten team F,

```
gamma_margin * sum over F's D-I wins g  pos(r_loser(g) + d_g - r_F)^2,
```

non-increasing in r_F and zero from `T_F = max over F's D-I wins of (r_loser + d_g)` on
(`T_F = -inf` with no D-I win). Its optimal set is the interval `[max(T_F, r_min), r_max]` when
`T_F < r_max`, and the single point `r_max` otherwise (the hinge is then positive and the rating
is determined at the bound). A win hinge of an unbeaten team is therefore zero at every optimum
except in that `r_max` case. `r_lower` is the same with the lower-division hinges (all of whose
teams have a loss): `[max(M, r_lower_min), r_lower_max]` with `M = max(r_team + margin * alpha)`
over D-I losses to non-D-I teams when `M < r_lower_max`, else the bound `r_lower_max` (football's
15: the case in all five reference scenarios). The optimal set is the product of the determined
values and these intervals, so it has a componentwise least point; lowering an unbeaten team
inside its interval changes no other team's optimal rating and no term's value.

**The rule.** `flat_rating_rule="min_feasible"` (owner decision; `BASKETBALL_DEFAULT`) publishes
that least point: an unbeaten team is rated `max(r_min, max over its D-I wins of r_loser + d_g)`,
just high enough to explain every win by its margin (demanded gap, alpha and the 20-point cap
included); a team with no D-I win (no games, or only wins over non-D-I teams) gets `r_min`; and
`r_lower` gets its least optimal value, `max(M, r_lower_min)`, when no lower-division hinge is
active (`r_lower_min` with no such loss). It applies to the main solve and to the prior-season
solve (so an unbeaten prior-season team's prior is its least value). With lambda > 0 no team is
undetermined, so the team part is a no-op by construction (the set of flat teams is empty);
`r_lower` has no prior term, so its rule applies at every week. `"solver"` (`FOOTBALL_COMPAT`)
keeps whatever the solver returns.

**Why a closed form, not a second solve.** The least point is also the unique minimiser of the sum
of ratings over the optimal set, but a second-stage solve needs "objective <= optimum + tol", and
any tolerance lets determined ratings drift (by about sqrt(tol / curvature)). The closed form
reads only the determined ratings the solver returned and writes only the undetermined
coordinates: determined ratings are bit for bit the first-stage values, and since `T_F` is a max
of (determined rating + constant), a flat rating's error is at most the largest error among the
determined ratings (cross-solver differences of flat teams are bounded by those of determined
teams, which the tests check). A coordinate determined at its upper bound (`T_F >= r_max`,
`M >= r_lower_max`) keeps the solver's value like any determined one, so no bound violation of the
solver is turned into a hinge change.

**Runtime checks** (`model/solve.py`, `ModelSolveError` on failure): the objective at the final
point equals the objective at the solver's point within `1e-8 + 1e-8 * |objective|` (measured: 0.0
with CLARABEL on the fixture, the synthetic season and the real 2025-26 season; with OSQP, which
stops with unbeaten teams slightly below their threshold, at most -3.6e-5 absolute, -3.3e-9
relative, on the synthetic season), and no determined rating changed at all. Under
`"min_feasible"`, records that show no loss for a team that lost a game raise `ValueError` (the
characterization needs every loser to carry a loss-rate term), and the config requires
`gamma_loss > 0`.

`SolveResult` reports `flat_team_ids` (teams undetermined in this solve: no loss and lambda 0;
empty when lambda > 0), `max_flat_adjustment` (largest |published - solver|),
`r_lower_adjustment` and `flat_objective_change`. `n_flat_teams` keeps its meaning (teams with no
losses, undetermined once lambda is 0).

Teams at the floor `r_min` are tied, so their order among themselves is arbitrary (callers sort).

## Modules

- `assemble.py`: `assemble(rows, team_ids) -> Assembled` (games, lower-division losses, records,
  connectivity), sorted by game id.
- `solve.py`: `solve(games, lower_division_losses, records, team_ids, priors, week, config) ->
  SolveResult`; no database access; `priors=None` is the prior-season solve.
- `pipeline.py`: `rate_week(store, season, week, config) -> RatingsResult`; reads only.

## Parity

`tests/test_model_parity.py` runs the raw football fixture (converted to this repository's `games`
rows by `tests/football_adapter.py`) through `assemble`, the prior-season solve and `solve` with
`FOOTBALL_COMPAT`, and through `rate_week` on a local sqlite store, and compares with
`tests/reference/goldens/football_goldens.json`, generated by the real football code: assembled
games, lower-division losses and records equal; objective and the four term values within 1e-6
relative (1e-9 absolute floor); ratings within 1e-3 absolute for the teams that are unique in the
scenario; `r_lower` within 1e-3. Weeks 3, 6, 7, 10, 15 and the prior season are covered. The
parity runs use OSQP because that is the solver that produced the goldens.
