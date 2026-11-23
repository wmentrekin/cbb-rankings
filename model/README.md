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

Prior ratings come from the same program run on the previous season's rated games with the prior
term off. Teams absent from the prior season get `default_prior`; prior-season teams absent now
are ignored. It is skipped when lambda is 0 (week >= `prior_fade_weeks`), since it cannot change
the result.

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
| `margin_scale_basis` | `"alpha"` | `"margin"` (PENDING OWNER DECISION) |

`tests/test_model_config.py` pins every football value and asserts `BASKETBALL_DEFAULT` differs
from `FOOTBALL_COMPAT` only in `solver` and `margin_scale_basis`, so a retune is a visible diff.

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

The basketball choice is made in exactly one place, `BASKETBALL_MARGIN_SCALE_BASIS` at the top of
`model/config.py`, marked `# PENDING OWNER DECISION`. Nothing else depends on it (tests that need
a basis name it explicitly). `SolveResult` reports `median_used`, `k_margin` and `n_capped` so the
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
| 13 | flat ratings of zero-loss teams at lambda 0 are whatever the solver returns | same (undetermined by the problem; see below); reported via `SolveResult.n_flat_teams` |
| solver | CVXPY's default (OSQP) | CLARABEL, named; OSQP hits its iteration cap on a 365-team prior solve |
| build | ~6,000 scalar `pos(...)**2` terms | the same terms as three matrix expressions (parity holds; build and solve fall from about 17 s to about 0.3 s per problem) |

Kept as in football: quirk 7 (the lower-division term is uncapped and unscaled), the 20-point cap
on D-I games only, D-I wins over non-D-I counted in records only.

Rating ties and flat directions: a team with no losses has no strictly convex term in its own
rating once lambda is 0, so its rating is not determined by the problem (any higher value is also
optimal) and depends on the solver. Likewise `r_lower` is determined only while some
lower-division hinge is active. Teams at the floor `r_min` are tied, so their order among
themselves is arbitrary.

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
