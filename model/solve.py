"""The optimization. No database access.

Minimise, over one rating r_t per D-I team and one shared rating r_lower for non-D-I opponents,

    gamma_margin * sum_g pos(r_loser + d_g - r_winner)^2        d_g = min(k*sqrt(margin_g), cap) * alpha_g
  + gamma_loss   * sum_t (loss_rate_t * r_t)^2
  + gamma_lower  * sum_l pos(r_team + margin_l*alpha_l - r_lower)^2    (D-I losses to non-D-I; raw margin)
  + lambda       * sum_t (r_t - prior_t)^2

subject to r_min <= r_t <= r_max and r_lower_min <= r_lower <= r_lower_max. This is football's
program term for term; the only difference in construction is that the ~6,000 `pos(...)**2`
terms are built as three matrix expressions instead of scalar by scalar, which cuts CVXPY
canonicalization from seconds to well under one second (README.md, "Parity").

Undetermined ratings (README.md, "Undetermined ratings"). With lambda = 0 a team with no loss has
no term that pins its rating, and r_lower is pinned only by an active lower-division hinge. Under
`flat_rating_rule="min_feasible"` those coordinates are set, after the solve, to their least value
over the whole optimal set, in closed form from the determined ratings; every other rating is left
exactly as solved and the objective is verified unchanged.
"""
from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Mapping, Sequence

import cvxpy as cp
import numpy as np
import scipy.sparse as sp

from model.assemble import Game, LowerLoss
from model.config import ModelConfig, check_week


class ModelSolveError(RuntimeError):
    """The solver did not return an acceptable optimal solution."""


# The "min_feasible" adjustment must leave the objective at the first-stage optimum. In exact
# arithmetic it changes only hinges that are zero at every optimum (plus the residue the solver
# leaves on them, of order gamma_margin * eps^2 for a solver accuracy eps), so the allowance is
# tight: relative to the objective, with an absolute floor for objectives near zero.
FLAT_OBJECTIVE_RTOL = 1e-8
FLAT_OBJECTIVE_ATOL = 1e-8


class SolverStatusError(ModelSolveError):
    """The solver raised or did not report an acceptable status (as opposed to a failed check)."""


@dataclass(frozen=True)
class SolveResult:
    ratings: dict[int, float]  # D-I teams only; the dummy team is never here
    r_lower: float  # the dummy rating; flat when no lower-division hinge is active (least value under "min_feasible")
    objective: float
    margin_term: float
    loss_rate_term: float
    lower_term: float
    prior_term: float
    lam: float  # prior weight used (0.0 when the prior term was off)
    k_margin: float
    median_used: float
    margin_scale_basis: str
    n_capped: int  # D-I games whose demanded gap hit max_gap_from_margin
    solver: str
    status: str
    iterations: int | None
    build_seconds: float  # problem construction + canonicalization up to the solver call
    solve_seconds: float  # CVXPY's solve call, wall clock
    solver_seconds: float | None  # the solver's own reported time
    n_teams: int
    n_games: int
    n_lower_losses: int
    n_default_priors: int = 0
    n_flat_teams: int = 0  # teams with no losses: their rating is undetermined when lam == 0
    flat_rating_rule: str = "solver"
    # Teams whose rating the objective leaves undetermined in THIS solve (no loss and lam == 0);
    # empty when lam > 0. Under "min_feasible" their ratings are the least optimal values.
    flat_team_ids: tuple[int, ...] = ()
    max_flat_adjustment: float = 0.0  # largest |published - solver| over flat_team_ids (0 under "solver")
    r_lower_adjustment: float = 0.0  # |published r_lower - solver r_lower| (0 under "solver")
    flat_objective_change: float = 0.0  # objective after the adjustment minus before (verified ~0)
    warnings: list[str] = field(default_factory=list)


def _objective_terms(rv, r_low, *, loser_idx, winner_idx, demand, loss_rate, low_idx, low_demand, lam, prior_vec, config):
    """(margin, loss_rate, lower, prior) term values at a point, in plain numpy."""
    margin_term = float(config.gamma_margin * np.sum(np.maximum(0.0, rv[loser_idx] + demand - rv[winner_idx]) ** 2))
    loss_term = float(config.gamma_loss * np.sum((loss_rate * rv) ** 2))
    lower_term = float(config.gamma_lower * np.sum(np.maximum(0.0, rv[low_idx] + low_demand - r_low) ** 2))
    prior_term = float(lam * np.sum((rv - prior_vec) ** 2)) if lam > 0 else 0.0
    return margin_term, loss_term, lower_term, prior_term


def _least_optimal(rv, r_low, flat, *, loser_idx, winner_idx, demand, low_idx, low_demand, config):
    """The least optimal value of every undetermined coordinate, given the determined ones.

    A flat team appears only as the WINNER of hinges whose loser is determined, so given the
    determined ratings its part of the objective, gamma_margin * sum pos(r_loser + d - r_team)^2,
    is non-increasing in its own rating and is zero from T = max(r_loser + d) on: its optimal set is
    [max(T, r_min), r_max] when T < r_max, and just {r_max} otherwise (the rating is then
    determined, at the bound, and the solver's value is kept like every other determined rating).
    With no D-I win, T = -inf and the least value is r_min. r_lower is the same with the
    lower-division hinges, whose teams are all determined. README.md, "Undetermined ratings", has
    the proof."""
    threshold = np.full(len(rv), -np.inf)
    if config.gamma_margin > 0 and len(winner_idx):
        np.maximum.at(threshold, winner_idx, rv[loser_idx] + demand)
    out = rv.copy()
    free = flat & (threshold < config.r_max)  # flat and not pinned at r_max
    out[free] = np.maximum(threshold[free], config.r_min)
    lower_threshold = float(np.max(rv[low_idx] + low_demand)) if config.gamma_lower > 0 and len(low_idx) else -np.inf
    if lower_threshold < config.r_lower_max:
        r_low = max(lower_threshold, config.r_lower_min)
    return out, float(r_low)


def _margin_scale(games: Sequence[Game], config: ModelConfig) -> tuple[float, float]:
    """(median_used, k). `alpha` reproduces football's indexing bug; `margin` is the documented formula."""
    if config.margin_scale_basis == "alpha":
        values = [abs(g.alpha) for g in games]
    else:
        values = [abs(g.margin) for g in games]
    median = float(np.median(values)) if values else config.median_fallback
    k = config.target_gap_for_median / max(np.sqrt(median), config.sqrt_floor)
    return median, float(k)


def _solve_once(
    games: Sequence[Game],
    lower_division_losses: Sequence[LowerLoss],
    records: Mapping[int, tuple[int, int]],
    team_ids: Sequence[int],
    priors: Mapping[int, float] | None,
    week: int,
    config: ModelConfig,
) -> SolveResult:
    """Rate `team_ids`. `priors=None` switches the prior term off (the prior-season solve);
    `week` must still be a valid week. Raises ModelSolveError unless the solver reports `optimal`
    (`optimal_inaccurate` too when `config.accept_inaccurate`)."""
    check_week(week)
    teams = sorted({int(t) for t in team_ids})
    if not teams:
        raise ValueError("team_ids is empty")
    index = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    unknown = (
        {g.winner for g in games} | {g.loser for g in games}
        | {x.team for x in lower_division_losses} | set(records)
    ) - set(index)
    if unknown:
        raise ValueError(f"teams not in team_ids: {sorted(unknown)[:5]}")

    t0 = time.perf_counter()
    lam = config.prior_lambda(week) if priors is not None else 0.0
    median, k = _margin_scale(games, config)

    # Football's per-game demanded gap: min(k*sqrt(max(0, margin)), cap) * alpha.
    margin = np.array([g.margin for g in games], dtype=float)
    alpha = np.array([g.alpha for g in games], dtype=float)
    scaled = k * np.sqrt(np.maximum(0.0, margin))
    capped = np.minimum(scaled, config.max_gap_from_margin)
    demand = capped * alpha
    n_capped = int(np.sum(scaled > config.max_gap_from_margin))
    m = len(games)
    loser_idx = np.array([index[g.loser] for g in games], dtype=int)
    winner_idx = np.array([index[g.winner] for g in games], dtype=int)

    wins_losses = np.array([records.get(t, (0, 0)) for t in teams], dtype=float).reshape(n, 2)
    played = wins_losses.sum(axis=1)
    loss_rate = np.divide(wins_losses[:, 1], played, out=np.zeros(n), where=played > 0)

    low_idx = np.array([index[x.team] for x in lower_division_losses], dtype=int)
    low_demand = np.array([x.margin * x.alpha for x in lower_division_losses], dtype=float)
    n_low = len(lower_division_losses)
    if config.flat_rating_rule == "min_feasible":
        # The closed form needs every team that loses a game to carry a loss-rate term.
        losers = np.union1d(loser_idx, low_idx).astype(int)
        no_loss = losers[loss_rate[losers] == 0]
        if len(no_loss):
            raise ValueError(f"records show no loss for teams that lost a game: {[teams[i] for i in no_loss[:5]]}")

    prior_vec = np.full(n, float(config.default_prior))
    n_default = 0
    if priors is not None:
        for t, i in index.items():
            if t in priors:
                prior_vec[i] = float(priors[t])
            else:
                n_default += 1

    r = cp.Variable(n, name="r")
    r_lower = cp.Variable(name="r_lower")

    terms = []
    if m:
        rows = np.concatenate([np.arange(m), np.arange(m)])
        cols = np.concatenate([loser_idx, winner_idx])
        vals = np.concatenate([np.ones(m), -np.ones(m)])
        diff = sp.csr_array((vals, (rows, cols)), shape=(m, n))  # r_loser - r_winner
        terms.append(config.gamma_margin * cp.sum_squares(cp.pos(diff @ r + demand)))
    if np.any(loss_rate > 0):
        terms.append(config.gamma_loss * cp.sum_squares(cp.multiply(loss_rate, r)))
    if n_low:
        pick = sp.csr_array((np.ones(n_low), (np.arange(n_low), low_idx)), shape=(n_low, n))
        terms.append(config.gamma_lower * cp.sum_squares(cp.pos(pick @ r + low_demand - r_lower)))
    if lam > 0:
        terms.append(lam * cp.sum_squares(r - prior_vec))
    objective = cp.Minimize(cp.sum(terms) if terms else cp.Constant(0.0))
    constraints = [r >= config.r_min, r <= config.r_max, r_lower >= config.r_lower_min, r_lower <= config.r_lower_max]
    problem = cp.Problem(objective, constraints)
    built = time.perf_counter()

    try:
        problem.solve(solver=config.solver, verbose=False)
    except (cp.error.SolverError, cp.error.DCPError) as exc:
        raise SolverStatusError(f"solver {config.solver} failed: {exc}") from exc
    solved = time.perf_counter()

    ok = {cp.OPTIMAL} | ({cp.OPTIMAL_INACCURATE} if config.accept_inaccurate else set())
    if problem.status not in ok:
        raise SolverStatusError(f"solver {config.solver} returned status {problem.status!r}, not 'optimal'")
    if r.value is None or r_lower.value is None:
        raise ModelSolveError(f"solver {config.solver} returned no values (status {problem.status!r})")

    rv = np.asarray(r.value, dtype=float)
    r_low = float(r_lower.value)
    shape = dict(loser_idx=loser_idx, winner_idx=winner_idx, demand=demand, low_idx=low_idx, low_demand=low_demand)
    term_args = dict(**shape, loss_rate=loss_rate, lam=lam, prior_vec=prior_vec, config=config)
    terms_first = _objective_terms(rv, r_low, **term_args)

    # Undetermined coordinates: no loss-rate term and no prior term. With lam > 0 there are none.
    flat = (loss_rate == 0) if lam == 0 else np.zeros(n, dtype=bool)
    max_adjust = lower_adjust = objective_change = 0.0
    if config.flat_rating_rule == "min_feasible":
        solved_rv, solved_low = rv, r_low
        rv, r_low = _least_optimal(solved_rv, solved_low, flat, **shape, config=config)
        terms_final = _objective_terms(rv, r_low, **term_args)
        objective_change = sum(terms_final) - sum(terms_first)
        if abs(objective_change) > FLAT_OBJECTIVE_ATOL + FLAT_OBJECTIVE_RTOL * abs(sum(terms_first)):
            raise ModelSolveError(
                f"min_feasible adjustment changed the objective by {objective_change:.3e} "
                f"(from {sum(terms_first):.10g}); the first-stage solution is not optimal enough"
            )
        if not np.array_equal(rv[~flat], solved_rv[~flat]):
            raise ModelSolveError("min_feasible adjustment moved a determined rating")
        max_adjust = float(np.max(np.abs(rv[flat] - solved_rv[flat]), initial=0.0))
        lower_adjust = abs(r_low - solved_low)
        margin_term, loss_term, lower_term, prior_term = terms_final
    else:
        margin_term, loss_term, lower_term, prior_term = terms_first

    warnings: list[str] = []
    if problem.status != cp.OPTIMAL:
        warnings.append(f"solver status {problem.status!r} accepted by config")
    stats = problem.solver_stats
    return SolveResult(
        ratings={t: float(rv[i]) for t, i in index.items()},
        r_lower=r_low,
        objective=float(problem.value),
        margin_term=margin_term,
        loss_rate_term=loss_term,
        lower_term=lower_term,
        prior_term=prior_term,
        lam=lam,
        k_margin=k,
        median_used=median,
        margin_scale_basis=config.margin_scale_basis,
        n_capped=n_capped,
        solver=stats.solver_name if stats and stats.solver_name else config.solver,
        status=problem.status,
        iterations=getattr(stats, "num_iters", None),
        build_seconds=built - t0,
        solve_seconds=solved - built,
        solver_seconds=getattr(stats, "solve_time", None),
        n_teams=n,
        n_games=m,
        n_lower_losses=n_low,
        n_default_priors=n_default,
        n_flat_teams=int(np.sum(wins_losses[:, 1] == 0)),
        flat_rating_rule=config.flat_rating_rule,
        flat_team_ids=tuple(teams[i] for i in np.flatnonzero(flat)),
        max_flat_adjustment=max_adjust,
        r_lower_adjustment=lower_adjust,
        flat_objective_change=objective_change,
        warnings=warnings,
    )


def solve(
    games: Sequence[Game],
    lower_division_losses: Sequence[LowerLoss],
    records: Mapping[int, tuple[int, int]],
    team_ids: Sequence[int],
    priors: Mapping[int, float] | None,
    week: int,
    config: ModelConfig,
) -> SolveResult:
    """Rate `team_ids` with `config.solver`. If that solver does not reach `optimal` and
    `config.fallback_solver` is set, solve again with the fallback solver and the least-optimal
    rule for undetermined ratings, and record a warning. The fallback pairing is deliberate: on
    real data OSQP's ratings for unbeaten teams match the least-optimal values to about two
    decimals, while an interior-point solver alone puts them mid-range, tens of points higher."""
    args = (games, lower_division_losses, records, team_ids, priors, week)
    try:
        return _solve_once(*args, config)
    except SolverStatusError as exc:
        if not config.fallback_solver or config.fallback_solver == config.solver:
            raise
        fallback = dataclasses.replace(
            config, solver=config.fallback_solver, flat_rating_rule="min_feasible", fallback_solver=None
        )
        result = _solve_once(*args, fallback)
        result.warnings.append(
            f"{exc}; solved with fallback {config.fallback_solver} and the least-optimal rule instead"
        )
        return result
