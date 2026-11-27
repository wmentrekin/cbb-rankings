import dataclasses

import pytest

from ingest.derive import DEFAULT_ALPHA
from model import config as config_module
from model.config import BASKETBALL_DEFAULT, FOOTBALL_COMPAT, ModelConfig, ModelConfigError

# cfb-rankings @ 40a6b4f: model/params.py:18-35 (ModelParams defaults), model/margin_scale.py:26-27,
# model/model.py:42 (default prior) and :52-57 (the fade).
FOOTBALL_VALUES = {
    "r_min": None,  # no lower bound on team ratings
    "r_max": 100,
    "r_lower_min": 0.01,  # football: r_fcs_min
    "r_lower_max": 15,  # r_fcs_max
    "display_min": 0.01,  # shift_to_display: the lowest team reads 0.01
    "gamma_margin": 0.05,
    "gamma_loss": 0.5,
    "gamma_lower": 5,  # gamma_fcs
    "target_gap_for_median": 7.0,  # target_gap
    "max_gap_from_margin": 20.0,  # max_gap
    "median_fallback": 1.0,  # FALLBACK_MEDIAN_MARGIN
    "sqrt_floor": 1e-6,  # MIN_SQRT_MEDIAN
    "prior_fade_weeks": 7,  # `if week < 7: lambda = (7 - week) / 700`
    "prior_fade_scale": 700,
    "default_prior": 1,  # `prior_ratings[team] = 1`
}


def test_football_compat_equals_every_football_constant():
    for name, value in FOOTBALL_VALUES.items():
        assert getattr(FOOTBALL_COMPAT, name) == value, name
    assert (FOOTBALL_COMPAT.alpha.neutral, FOOTBALL_COMPAT.alpha.home_win, FOOTBALL_COMPAT.alpha.away_win) == (1.0, 0.8, 1.2)
    assert FOOTBALL_COMPAT.alpha is DEFAULT_ALPHA  # one source of truth with ingest
    assert FOOTBALL_COMPAT.solver == "OSQP"  # what CVXPY chose for the goldens
    assert FOOTBALL_COMPAT.accept_inaccurate is False
    assert FOOTBALL_COMPAT.flat_rating_rule == "solver"  # quirk 11: whatever the solver returns
    assert FOOTBALL_COMPAT.fallback_solver is None  # football has no second solver


def test_every_config_field_is_covered_by_the_pin():
    pinned = set(FOOTBALL_VALUES) | {"alpha", "solver", "accept_inaccurate", "flat_rating_rule", "fallback_solver"}
    assert {f.name for f in dataclasses.fields(ModelConfig)} == pinned


def test_basketball_default_differs_from_football_exactly_in_the_owner_s_four_fields():
    differing = {
        f.name for f in dataclasses.fields(ModelConfig)
        if getattr(BASKETBALL_DEFAULT, f.name) != getattr(FOOTBALL_COMPAT, f.name)
    }
    # Owner decisions (2026-11-24), each derived where BASKETBALL_DEFAULT is defined:
    # - prior_fade_weeks 10 / prior_fade_scale 1050: the prior fades out at week 10 (conference play
    #   starts in week 9; by week 10 every team has about four conference games); week 1 keeps
    #   football's strength, 9/1050 = 6/700.
    # - target_gap_for_median 5.0: football's proportion, about half the median margin (football 7 vs
    #   medians of 13-14); the full 2025-26 D-I vs D-I median margin is 10.0 points.
    # - fallback_solver CLARABEL: a second solver when OSQP does not reach `optimal`.
    assert differing == {"prior_fade_weeks", "prior_fade_scale", "target_gap_for_median", "fallback_solver"}
    assert (BASKETBALL_DEFAULT.prior_fade_weeks, BASKETBALL_DEFAULT.prior_fade_scale) == (10, 1050)
    assert BASKETBALL_DEFAULT.target_gap_for_median == 5.0
    assert BASKETBALL_DEFAULT.max_gap_from_margin == FOOTBALL_COMPAT.max_gap_from_margin == 20.0
    assert BASKETBALL_DEFAULT.fallback_solver == "CLARABEL" and FOOTBALL_COMPAT.fallback_solver is None
    assert BASKETBALL_DEFAULT.solver == FOOTBALL_COMPAT.solver == "OSQP"
    assert BASKETBALL_DEFAULT.flat_rating_rule == FOOTBALL_COMPAT.flat_rating_rule == "solver"
    assert BASKETBALL_DEFAULT.r_min is None and BASKETBALL_DEFAULT.display_min == 0.01
    assert BASKETBALL_DEFAULT.accept_inaccurate is False


def test_margin_scale_constants_are_set_in_one_place():
    """target_gap / max_gap are plain fields set once, in BASKETBALL_DEFAULT (model/config.py);
    nothing else depends on their values, and a change there must also change this test."""
    source = open(config_module.__file__).read()
    assert source.count("target_gap_for_median=") == 1 and source.count("max_gap_from_margin=") == 1


@pytest.mark.parametrize("week, expected", [(1, 6 / 700), (3, 4 / 700), (6, 1 / 700), (7, 0.0), (8, 0.0), (19, 0.0)])
def test_football_prior_lambda_fades_to_zero_at_week_7(week, expected):
    assert FOOTBALL_COMPAT.prior_lambda(week) == pytest.approx(expected, abs=0, rel=1e-15)


@pytest.mark.parametrize("week, expected", [(1, 9 / 1050), (3, 7 / 1050), (6, 4 / 1050), (7, 3 / 1050), (9, 1 / 1050),
                                            (10, 0.0), (11, 0.0), (19, 0.0)])
def test_basketball_prior_lambda_fades_to_zero_at_week_10(week, expected):
    assert BASKETBALL_DEFAULT.prior_lambda(week) == pytest.approx(expected, abs=0, rel=1e-15)
    if week == 1:  # week 1 keeps football's strength
        assert BASKETBALL_DEFAULT.prior_lambda(1) == pytest.approx(FOOTBALL_COMPAT.prior_lambda(1), rel=1e-15)


@pytest.mark.parametrize("week", [0, -1, None, 1.5, "3", True])
def test_week_must_be_an_int_at_least_one(week):
    with pytest.raises(ValueError):
        FOOTBALL_COMPAT.prior_lambda(week)


@pytest.mark.parametrize("bad", [
    {"r_min": 5, "r_max": 1},
    {"r_min": float("nan")},
    {"display_min": float("inf")},
    {"display_min": "0.01"},
    {"r_lower_min": 20},
    {"gamma_margin": -1},
    {"target_gap_for_median": 0},
    {"max_gap_from_margin": 0},
    {"sqrt_floor": 0},
    {"prior_fade_weeks": 0},
    {"prior_fade_scale": 0},
    {"r_max": float("nan")},
    {"solver": ""},
    {"fallback_solver": ""},
    {"accept_inaccurate": "yes"},
    {"flat_rating_rule": "max"},
    {"flat_rating_rule": "min_feasible", "gamma_loss": 0},
])
def test_invalid_configs_are_rejected(bad):
    with pytest.raises(ModelConfigError):
        dataclasses.replace(FOOTBALL_COMPAT, **bad)


def test_a_floor_and_publishing_raw_are_still_expressible():
    legacy = dataclasses.replace(FOOTBALL_COMPAT, r_min=0.01, display_min=None)  # football's LEGACY_FLOOR_PARAMS
    assert legacy.r_min == 0.01 and legacy.display_min is None


def test_config_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        FOOTBALL_COMPAT.r_max = 5
