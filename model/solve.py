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
"""
from __future__ import annotations

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


@dataclass(frozen=True)
class SolveResult:
    ratings: dict[int, float]  # D-I teams only; the dummy team is never here
    r_lower: float  # the dummy rating; flat (solver-dependent) when no lower-division loss is active
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
    warnings: list[str] = field(default_factory=list)


def _margin_scale(games: Sequence[Game], config: ModelConfig) -> tuple[float, float]:
    """(median_used, k). `alpha` reproduces football's indexing bug; `margin` is the documented formula."""
    if config.margin_scale_basis == "alpha":
        values = [abs(g.alpha) for g in games]
    else:
        values = [abs(g.margin) for g in games]
    median = float(np.median(values)) if values else config.median_fallback
    k = config.target_gap_for_median / max(np.sqrt(median), config.sqrt_floor)
    return median, float(k)


def solve(
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
        raise ModelSolveError(f"solver {config.solver} failed: {exc}") from exc
    solved = time.perf_counter()

    ok = {cp.OPTIMAL} | ({cp.OPTIMAL_INACCURATE} if config.accept_inaccurate else set())
    if problem.status not in ok:
        raise ModelSolveError(f"solver {config.solver} returned status {problem.status!r}, not 'optimal'")
    if r.value is None or r_lower.value is None:
        raise ModelSolveError(f"solver {config.solver} returned no values (status {problem.status!r})")

    rv = np.asarray(r.value, dtype=float)
    r_low = float(r_lower.value)
    margin_term = float(config.gamma_margin * np.sum(np.maximum(0.0, rv[loser_idx] + demand - rv[winner_idx]) ** 2)) if m else 0.0
    loss_term = float(config.gamma_loss * np.sum((loss_rate * rv) ** 2))
    lower_term = float(config.gamma_lower * np.sum(np.maximum(0.0, rv[low_idx] + low_demand - r_low) ** 2)) if n_low else 0.0
    prior_term = float(lam * np.sum((rv - prior_vec) ** 2)) if lam > 0 else 0.0

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
        warnings=warnings,
    )
