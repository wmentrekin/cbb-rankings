"""Every constant the optimization uses, in one frozen dataclass.

`FOOTBALL_COMPAT` carries football's values exactly (cfb-rankings @ 8ab8201) and is what the
parity tests run. `BASKETBALL_DEFAULT` is what production runs; it differs from football in
exactly three fields, the solver, the margin-scale basis and the flat-rating rule (a test pins this).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from ingest.derive import DEFAULT_ALPHA, AlphaConfig

MarginScaleBasis = Literal["margin", "alpha"]
# How a rating the objective leaves undetermined is chosen (model/README.md, "Undetermined ratings"):
#   "solver":       whatever the solver returns (football's behavior, reference quirk 13).
#   "min_feasible": the least value among all optimal solutions (owner decision): an unbeaten team is
#                   rated just high enough to explain its wins by their margins.
FlatRatingRule = Literal["solver", "min_feasible"]

# The ONE place the basketball margin-scale basis is chosen.
#   "margin": the documented formula, median margin of victory over D-I vs D-I played games.
#   "alpha":  football's actual behavior (an indexing bug makes it the median of per-game alpha).
# Owner decision (2026-11-23): basketball runs the documented formula, and football is being
# fixed to match. This one line is the only place the choice is made.
BASKETBALL_MARGIN_SCALE_BASIS: MarginScaleBasis = "margin"

BASKETBALL_SOLVER = "CLARABEL"


class ModelConfigError(ValueError):
    """A ModelConfig field is out of range."""


@dataclass(frozen=True)
class ModelConfig:
    # Rating bounds: D-I teams, and the shared dummy lower-division team.
    r_min: float = 0.01
    r_max: float = 100.0
    r_lower_min: float = 0.01
    r_lower_max: float = 15.0
    # Term weights.
    gamma_margin: float = 0.05
    gamma_loss: float = 0.5
    gamma_lower: float = 5.0
    # Margin scaling: k = target_gap_for_median / max(sqrt(median), sqrt_floor); a D-I game
    # demands min(k * sqrt(margin), max_gap_from_margin) * alpha rating points.
    target_gap_for_median: float = 7.0
    max_gap_from_margin: float = 20.0
    median_fallback: float = 1.0
    sqrt_floor: float = 1e-6
    # Prior fade: lambda = (prior_fade_weeks - week) / prior_fade_scale for week < prior_fade_weeks.
    prior_fade_weeks: int = 7
    prior_fade_scale: float = 700.0
    default_prior: float = 1.0
    # Home/away multipliers. Stored per game at ingest (`games.alpha`); the model never
    # recomputes them. Kept here so one object (ingest.derive.DEFAULT_ALPHA) is the source.
    alpha: AlphaConfig = DEFAULT_ALPHA
    solver: str = BASKETBALL_SOLVER
    accept_inaccurate: bool = False
    margin_scale_basis: MarginScaleBasis = BASKETBALL_MARGIN_SCALE_BASIS
    flat_rating_rule: FlatRatingRule = "min_feasible"

    def __post_init__(self) -> None:
        def need(ok: bool, msg: str) -> None:
            if not ok:
                raise ModelConfigError(msg)

        for name in (
            "r_min", "r_max", "r_lower_min", "r_lower_max", "gamma_margin", "gamma_loss",
            "gamma_lower", "target_gap_for_median", "max_gap_from_margin", "median_fallback",
            "sqrt_floor", "prior_fade_scale", "default_prior",
        ):
            value = getattr(self, name)
            need(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
                 f"{name} must be a finite number, got {value!r}")
        need(0 <= self.r_min < self.r_max, "need 0 <= r_min < r_max")
        need(0 <= self.r_lower_min <= self.r_lower_max, "need 0 <= r_lower_min <= r_lower_max")
        for name in ("gamma_margin", "gamma_loss", "gamma_lower"):
            need(getattr(self, name) >= 0, f"{name} must be >= 0")
        need(self.target_gap_for_median > 0, "target_gap_for_median must be > 0")
        need(self.max_gap_from_margin > 0, "max_gap_from_margin must be > 0")
        need(self.median_fallback >= 0, "median_fallback must be >= 0")
        need(self.sqrt_floor > 0, "sqrt_floor must be > 0")
        need(isinstance(self.prior_fade_weeks, int) and self.prior_fade_weeks >= 1, "prior_fade_weeks must be an int >= 1")
        need(self.prior_fade_scale > 0, "prior_fade_scale must be > 0")
        for name in ("neutral", "home_win", "away_win"):
            need(getattr(self.alpha, name) > 0, f"alpha.{name} must be > 0")
        need(isinstance(self.solver, str) and bool(self.solver), "solver must be a non-empty name")
        need(isinstance(self.accept_inaccurate, bool), "accept_inaccurate must be a bool")
        need(self.margin_scale_basis in ("margin", "alpha"), "margin_scale_basis must be 'margin' or 'alpha'")
        need(self.flat_rating_rule in ("solver", "min_feasible"), "flat_rating_rule must be 'solver' or 'min_feasible'")
        # The least-optimal-rating rule relies on the loss-rate term fixing every team that has a loss.
        need(self.flat_rating_rule == "solver" or self.gamma_loss > 0, "flat_rating_rule 'min_feasible' needs gamma_loss > 0")

    def prior_lambda(self, week: int) -> float:
        """Weight of the prior term for a week: fades linearly to 0 at `prior_fade_weeks`."""
        check_week(week)
        if week < self.prior_fade_weeks:
            return (self.prior_fade_weeks - week) / self.prior_fade_scale
        return 0.0


def check_week(week: int) -> int:
    if isinstance(week, bool) or not isinstance(week, int) or week < 1:
        raise ValueError(f"week must be an int >= 1, got {week!r}")
    return week


# Football's values, football's implicit solver (OSQP; CVXPY's default for these problems),
# football's margin-scale behavior and football's solver-chosen flat ratings. Used by the parity
# tests only.
FOOTBALL_COMPAT = ModelConfig(solver="OSQP", margin_scale_basis="alpha", flat_rating_rule="solver")

BASKETBALL_DEFAULT = ModelConfig()
