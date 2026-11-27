"""Every constant the optimization uses, in one frozen dataclass.

The field defaults ARE football's production model (cfb-rankings @ 40a6b4f, `model/params.py`
plus the constants still inline in `model/model.py`), so `FOOTBALL_COMPAT = ModelConfig()` and
the parity tests run it. `BASKETBALL_DEFAULT` is what production runs; it differs from football in
exactly the fields set where it is defined (a test pins the set).
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Literal

from ingest.derive import DEFAULT_ALPHA, AlphaConfig

# How a rating the objective leaves undetermined is chosen (model/README.md, "Undetermined ratings"):
#   "solver":       whatever the solver returns (football's behavior, reference quirk 11).
#   "min_feasible": the least value among all optimal solutions, computed in closed form. An opt-in
#                   alternative and the fallback solve's rule, not used in normal runs.
# Owner decision (2026-11-24): basketball stays with football's method, so production uses "solver"
# with football's solver. On real data OSQP's answer for unbeaten teams matches "min_feasible" to
# about two decimals, because OSQP stops as soon as their win hinges reach zero.
FlatRatingRule = Literal["solver", "min_feasible"]


class ModelConfigError(ValueError):
    """A ModelConfig field is out of range."""


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class ModelConfig:
    # Rating bounds inside the solve. r_min None = no lower bound (football since 40a6b4f: the
    # loss-rate penalty anchors the scale at zero and the bottom of the solve runs negative).
    # The shared dummy lower-division team keeps its bounds.
    r_min: float | None = None
    r_max: float = 100.0
    r_lower_min: float = 0.01
    r_lower_max: float = 15.0
    # Published ratings are the raw solve moved by one constant so the lowest D-I team reads
    # display_min (model.pipeline.shift_to_display). None = publish raw. Prior ratings are never
    # shifted: the prior-season solve is fed back raw.
    display_min: float | None = 0.01
    # Term weights.
    gamma_margin: float = 0.05
    gamma_loss: float = 0.5
    gamma_lower: float = 5.0
    # Margin scaling: k = target_gap_for_median / max(sqrt(median margin), sqrt_floor); a D-I game
    # demands min(k * sqrt(margin), max_gap_from_margin) * alpha rating points. The median is over
    # the margins of the D-I vs D-I played games being rated (median_fallback when there are none).
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
    # Football names no solver; CVXPY picks OSQP for this problem. Named so a CVXPY upgrade
    # cannot change it silently.
    solver: str = "OSQP"
    accept_inaccurate: bool = False
    # Used only when `solver` does not reach `optimal`: solve again with this solver and the
    # "min_feasible" rule (see model.solve.solve). None disables it.
    fallback_solver: str | None = None
    flat_rating_rule: FlatRatingRule = "solver"

    def __post_init__(self) -> None:
        def need(ok: bool, msg: str) -> None:
            if not ok:
                raise ModelConfigError(msg)

        for name in (
            "r_max", "r_lower_min", "r_lower_max", "gamma_margin", "gamma_loss", "gamma_lower",
            "target_gap_for_median", "max_gap_from_margin", "median_fallback", "sqrt_floor",
            "prior_fade_scale", "default_prior",
        ):
            value = getattr(self, name)
            need(_finite(value), f"{name} must be a finite number, got {value!r}")
        for name in ("r_min", "display_min"):
            value = getattr(self, name)
            need(value is None or _finite(value), f"{name} must be None or a finite number, got {value!r}")
        need(self.r_min is None or self.r_min < self.r_max, "need r_min < r_max")
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
        need(self.fallback_solver is None or (isinstance(self.fallback_solver, str) and bool(self.fallback_solver)),
             "fallback_solver must be None or a non-empty name")
        need(isinstance(self.accept_inaccurate, bool), "accept_inaccurate must be a bool")
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


# Football exactly (cfb-rankings @ 40a6b4f): no rating floor, published shift to 0.01, the
# true-median margin scale, the prior fading out at week 7 (scale 700), OSQP, solver-chosen
# flat ratings, no second solver. Used by the parity tests.
FOOTBALL_COMPAT = ModelConfig()

# Production. Every difference from football is a line here (tests/test_model_config.py pins the set).
BASKETBALL_DEFAULT = dataclasses.replace(
    FOOTBALL_COMPAT,
    # Owner decision (2026-11-24): the prior fades to zero at week 10, not 7, so the game network is
    # large and connected before it is dropped (conference play starts in week 9; by week 10 every
    # team has about four conference games). Week 1 keeps football's strength: 9/1050 = 6/700.
    prior_fade_weeks=10,
    prior_fade_scale=1050.0,
    # Owner decision (2026-11-24): if OSQP does not reach `optimal`, solve again with CLARABEL and
    # the least-optimal rule rather than fail (football has no second solver).
    fallback_solver="CLARABEL",
    # Owner decision (2026-11-24): football's PROPORTION, not its number. Football's 7.0 is about
    # half its median margin (FBS vs FBS regular-season medians 13, 13, 14, 13 for 2023-2026); the
    # full 2025-26 D-I vs D-I median margin is 10.0 points (5,647 rated games), so 5.0, fixed.
    target_gap_for_median=5.0,
    # Football's 20.0, kept. With target 5 the cap binds only above median * (20 / 5)^2 = 160
    # points at the full-season median; the largest 2025-26 D-I vs D-I margin was 72.
    max_gap_from_margin=20.0,
)
