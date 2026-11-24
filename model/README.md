# model

The ratings optimization, ported from `cfb-rankings` and proven against football @ 40a6b4f
(`model/params.py`, `model/margin_scale.py`, `model/model.py`, `model/prior_model.py`,
`model/process_data.py`). The methodology is football's; constants and sport labels are
configuration (`model/config.py`), and every difference from football is a field of
`BASKETBALL_DEFAULT`.

## Formulation

One rating `r_t` per D-I team `t` and one shared rating `r_L` for every non-D-I opponent.
Minimize

```
  gamma_margin * sum over D-I games g      pos(r_loser + d_g - r_winner)^2
+ gamma_loss   * sum over D-I teams t      (loss_rate_t * r_t)^2
+ gamma_lower  * sum over D-I losses l to non-D-I   pos(r_team + margin_l * alpha_l - r_L)^2
+ lambda(week) * sum over D-I teams t      (r_t - prior_t)^2

d_g = min(k * sqrt(margin_g), max_gap_from_margin) * alpha_g
k   = target_gap_for_median / max(sqrt(median margin), sqrt_floor)
lambda(week) = (prior_fade_weeks - week) / prior_fade_scale   for week < prior_fade_weeks, else 0

r_t <= r_max   (no lower bound: r_min is None)        r_lower_min <= r_L <= r_lower_max
```

`pos(x) = max(x, 0)`. The median margin is over the D-I vs D-I played games being rated (the data
window through the requested week; `median_fallback` when there are none), football's
`model/margin_scale.py`. So a median-margin game demands exactly `target_gap_for_median` rating
points before the home/away multiplier, and the cap binds for margins above
`median * (max_gap_from_margin / target_gap_for_median)^2`. `loss_rate_t = losses / (wins +
losses)` over every played game of the team, including games against non-D-I teams (a D-I win over
a non-D-I team is a record-only event). `alpha` is stored per game at ingest (1.0 neutral, 0.8 home
win, 1.2 away win, from `ingest.derive.DEFAULT_ALPHA`); the model reads it and never recomputes
it. The lower-division term uses the RAW margin times alpha (no square root, no scale, no cap).

**Scales.** With no lower bound the loss-rate penalty anchors the scale at zero and the bottom of
the solve runs negative. `solve` returns these RAW ratings. `model.pipeline.rate_week` then puts
them on the PUBLISHED scale with `shift_to_display` (the only place it is computed): every rating
plus one constant, so the lowest D-I team reads `display_min` (0.01). The dummy is not a rating key
and is not shifted (`RatingsResult.display_shift` applies to it if needed). A uniform shift changes
no gap and no rank, but it is set by the lowest team, so one game of the worst team moves every
published rating. `RatingsResult` carries `raw_ratings`, `published_ratings` and `display_shift`;
`main.py` stores and publishes `published_ratings`.

Prior ratings come from the same program run on the previous season's rated GAMES with the prior
term off (and the same `flat_rating_rule`), re-solved on every run, never read from the `ratings`
table, and used RAW (unshifted), as in football. Teams absent from the prior season get
`default_prior` (on the raw scale); prior-season teams absent now are ignored. It is skipped when
lambda is 0 (week >= `prior_fade_weeks`), since it cannot change the result.

Among the optimal solutions, the published one is chosen by `flat_rating_rule` (see "Undetermined
ratings" below): production takes the solver's (OSQP's) value, as football does.

## Configuration

`ModelConfig()`'s defaults are football's values, so `FOOTBALL_COMPAT = ModelConfig()`.

| Field | Football (`FOOTBALL_COMPAT`, 40a6b4f) | `BASKETBALL_DEFAULT` |
| --- | --- | --- |
| `r_min`, `r_max` | None (no lower bound), 100 | same |
| `display_min` | 0.01 (`shift_to_display`) | same |
| `r_lower_min`, `r_lower_max` | 0.01, 15 (football `r_fcs_*`) | same |
| `gamma_margin`, `gamma_loss`, `gamma_lower` | 0.05, 0.5, 5 | same |
| `target_gap_for_median` | 7.0 | **5.0**: football's proportion, not its number. Football's 7 is about half its median margin (FBS vs FBS regular-season medians 13, 13, 14, 13 for 2023-2026); the full 2025-26 D-I vs D-I median margin is 10.0 points (5,647 rated played games), so 5.0, a fixed constant as football's 7 is |
| `max_gap_from_margin` | 20.0 | same (20.0). With target 5 the cap binds above median x 16 points: 160 at the full-season median of 10 (football: 13.5 x (20/7)^2 = 110); the largest 2025-26 D-I vs D-I margin was 72, so the cap is inactive on that season |
| `median_fallback`, `sqrt_floor` | 1.0, 1e-6 | same |
| `prior_fade_weeks`, `prior_fade_scale` | 7, 700 | **10, 1050**: the game network must be large and connected before the prior is dropped; conference play starts in week 9 and by week 10 every team has about four conference games. Week 1 keeps football's strength, 9/1050 = 6/700 |
| `default_prior` | 1 | same |
| `alpha` | `ingest.derive.DEFAULT_ALPHA` (1.0 / 0.8 / 1.2) | same |
| `solver` | OSQP (CVXPY's implicit choice for the goldens) | OSQP, named |
| `accept_inaccurate` | False | False |
| `fallback_solver` | none | **CLARABEL** (used only if OSQP does not reach `optimal`) |
| `flat_rating_rule` | `"solver"` | `"solver"` (`"min_feasible"` is opt-in and the fallback's rule) |

`tests/test_model_config.py` pins every football value and asserts `BASKETBALL_DEFAULT` differs
from `FOOTBALL_COMPAT` exactly in `prior_fade_weeks`, `prior_fade_scale`, `target_gap_for_median`
and `fallback_solver`. `target_gap_for_median` and `max_gap_from_margin` are set in one place
(`BASKETBALL_DEFAULT` in `model/config.py`); nothing else depends on their values.

On the real data (2025-26 weeks 1-19, and 2026-27 weeks 1-3 with 2025-26 as the prior season)
`BASKETBALL_DEFAULT` reaches `optimal` with OSQP in every main and prior solve (75 to 350
iterations); the fallback was never used; the 22 weeks take 2.3 s in all.

At lambda 0 and with the cap not binding, `target_gap_for_median` is a pure scale: every demanded
gap is proportional to it, so the optimal ratings scale by the same factor and ranks do not change
(measured on 2025-26 week 19: target 5 vs 7 gives ratings x 5/7, Spearman 1.0). It changes ranks
only through the prior term (priors and `default_prior` are not rescaled), the dummy's fixed
0.01..15 bounds, and the cap.

## Intentional differences from cfb-rankings @ 40a6b4f

Numbers refer to "Known quirks" in `tests/reference/README.md`.

| # | Football | Here |
| --- | --- | --- |
| config | fade at week 7 (scale 700); target gap 7; one solver | fade at week 10 (scale 1050); target gap 5; CLARABEL fallback (see the table above) |
| 1 | `week=None` crashes after the prior solve, `week=0` reads all games with lambda 7/700 | `week` must be an int >= 1 (`ValueError`) |
| 2 | unguarded empty margin/loss-rate sums crash after the solve | no games or no records solve normally (term value 0) |
| 3 | 0-0 sentinel for unplayed games, which still count toward connectivity | played means `winner_team_id IS NOT NULL`; connectivity counts played D-I vs D-I games only |
| 4 | ties are away wins; record has a tie slot | no ties (ingest stores them as unplayed); records are `(wins, losses)` |
| 5 | first season with no prior data raises | every team gets `default_prior`; a warning is returned |
| 7 | teams keyed by name; dummy is `ratings["fcs"]` (shifted with the teams) and can collide with a team | teams keyed by integer id; the dummy is a separate variable, returned only as `SolveResult.r_lower` (raw) |
| 8 | prior solve runs every week | skipped when lambda is 0 |
| 9 | any status except infeasible/unbounded accepted; prior solve can hit `UnboundLocalError` | only `optimal` accepted; anything else, or a solver error, raises `ModelSolveError` (after the fallback, if configured). `accept_inaccurate=True` also admits `optimal_inaccurate` |
| 10 | row order from unordered SQL | games sorted by id; results do not depend on row order |
| 11 | flat ratings of zero-loss teams at lambda 0 are whatever the solver returns | kept: same solver, same behavior (see "Undetermined ratings"); a deterministic `"min_feasible"` rule exists as an opt-in and for the fallback solve |
| 17 | `two_sided_margin` option (False in production) | not implemented (production's one-sided hinge only) |
| solver | CVXPY's default (OSQP) | OSQP, named, with a CLARABEL fallback: football accepts an inaccurate solve silently, basketball retries with the fallback and fails loudly if that fails too |
| build | ~6,000 scalar `pos(...)**2` terms | the same terms as three matrix expressions (parity holds; build and solve fall from about 17 s to about 0.3 s per problem) |

Kept as in football: no rating floor and the published shift (quirks 12-16), raw priors, quirk 6
(the lower-division term is uncapped and unscaled), the cap on D-I games only, D-I wins over non-D-I
counted in records only.

## Undetermined ratings (reference README, quirk 11)

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
(`T_F = -inf` with no D-I win). Its optimal set is the interval `[T_F, r_max]` (`[max(T_F, r_min),
r_max]` if a floor is configured) when `T_F < r_max`, and the single point `r_max` otherwise (the hinge is then positive and the rating
is determined at the bound). A win hinge of an unbeaten team is therefore zero at every optimum
except in that `r_max` case. `r_lower` is the same with the lower-division hinges (all of whose
teams have a loss): `[max(M, r_lower_min), r_lower_max]` with `M = max(r_team + margin * alpha)`
over D-I losses to non-D-I teams when `M < r_lower_max`, else the bound `r_lower_max` (football's
15: the case in every reference scenario but w01). With no floor, a team with no loss and no D-I
win (no games, or only wins over non-D-I teams) has the optimal set `(-inf, r_max]`. The optimal set is the product of the determined
values and these intervals, so it has a componentwise least point except in that unbounded case;
lowering an unbeaten team
inside its interval changes no other team's optimal rating and no term's value.

**What production does.** By the owner's decision of 2026-11-24 basketball stays with football's
method: `BASKETBALL_DEFAULT` uses `flat_rating_rule="solver"` with OSQP, so an undetermined rating
is whatever OSQP returns. On the real 2025-26 season (no floor, target 5) that value is within
0.04 of the least optimal point described below at weeks 10-18 (OSQP stops as soon as an unbeaten
team's win hinges reach zero), whereas CLARABEL returns a point far inside the interval, tens of
points higher (75-90 on the fixture and the synthetic season). The closed-form rule below is therefore not used in normal runs; it is the rule the
fallback solve applies, so that a CLARABEL fallback reproduces what OSQP would have published.

**The rule.** `flat_rating_rule="min_feasible"` (opt-in, and the fallback's rule) publishes
that least point: an unbeaten team is rated `max over its D-I wins of (r_loser + d_g)` (and at
least `r_min` if a floor is configured), just high enough to explain every win by its margin
(demanded gap, alpha and the cap included). A team with no D-I win (no games, or only wins over
non-D-I teams) has no least point without a floor: it gets `UNCONSTRAINED_RATING` = 0.0, the point
the loss-rate penalty anchors the scale at (and about what OSQP returns, since it starts at zero and
gets no gradient for it); with a floor it gets `r_min`. Neither choice touches the objective, which
the runtime check confirms. And
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
relative, on the synthetic season, measured with the former floor; the tests assert the bound
on the fixture and the synthetic season without it), and no determined rating changed at all. Under
`"min_feasible"`, records that show no loss for a team that lost a game raise `ValueError` (the
characterization needs every loser to carry a loss-rate term), and the config requires
`gamma_loss > 0`.

`SolveResult` reports `flat_team_ids` (teams undetermined in this solve: no loss and lambda 0;
empty when lambda > 0), `max_flat_adjustment` (largest |published - solver|),
`r_lower_adjustment` and `flat_objective_change`. `n_flat_teams` keeps its meaning (teams with no
losses, undetermined once lambda is 0).

An undetermined team could in principle be the lowest team, which would make the display shift
(and so every published rating) solver-dependent; it is not in any reference scenario or real week
checked, because the lowest team always has losses.

## Modules

- `assemble.py`: `assemble(rows, team_ids) -> Assembled` (games, lower-division losses, records,
  connectivity), sorted by game id.
- `solve.py`: `solve(games, lower_division_losses, records, team_ids, priors, week, config) ->
  SolveResult`; no database access; `priors=None` is the prior-season solve.
- `pipeline.py`: `rate_week(store, season, week, config) -> RatingsResult` (raw and published
  ratings); reads only. `shift_to_display(raw, config)`: the published scale.

## Parity

`tests/test_model_parity.py` runs the raw football fixture (converted to this repository's `games`
rows by `tests/football_adapter.py`) through `assemble`, the prior-season solve and `solve` with
`FOOTBALL_COMPAT`, and through `rate_week` on a local sqlite store, and compares with
`tests/reference/goldens/football_goldens.json`, generated by the real football code: assembled
games, lower-division losses and records equal; objective and the four term values within 1e-6
relative (1e-9 absolute floor); `k` and the median margin; raw ratings and published (shifted)
ratings within 1e-3 absolute for the teams that are unique in the scenario, the shift and the lowest
team; `r_lower` within 1e-3 where determined. Weeks 1, 3, 6, 7, 10, 15 and the prior season are
covered (the lowest team changes from w01 to w03 to w06, so the shift is exercised). The parity
runs use OSQP because that is the solver that produced the goldens; measured differences are at
most 3.2e-12 relative on the objective and 6e-11 on ratings.
