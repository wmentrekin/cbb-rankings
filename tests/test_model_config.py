import dataclasses

import pytest

from ingest.derive import DEFAULT_ALPHA
from model import config as config_module
from model.config import BASKETBALL_DEFAULT, FOOTBALL_COMPAT, ModelConfig, ModelConfigError

# cfb-rankings @ 8ab8201: model/model.py:33-44 and model/prior_model.py:25-34, 36-40.
FOOTBALL_VALUES = {
    "r_min": 0.01,
    "r_max": 100,
    "r_lower_min": 0.01,  # football: r_fcs_min
    "r_lower_max": 15,  # r_fcs_max
    "gamma_margin": 0.05,
    "gamma_loss": 0.5,
    "gamma_lower": 5,  # gamma_fcs
    "target_gap_for_median": 7.0,
    "max_gap_from_margin": 20.0,  # MAX_RATING_GAP_FROM_MARGIN
    "median_fallback": 1.0,
    "sqrt_floor": 1e-6,
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
    assert FOOTBALL_COMPAT.margin_scale_basis == "alpha"  # football's actual behavior
    assert FOOTBALL_COMPAT.accept_inaccurate is False
    assert FOOTBALL_COMPAT.flat_rating_rule == "solver"  # quirk 13: whatever the solver returns


def test_every_config_field_is_covered_by_the_pin():
    pinned = set(FOOTBALL_VALUES) | {"alpha", "solver", "margin_scale_basis", "accept_inaccurate", "flat_rating_rule"}
    assert {f.name for f in dataclasses.fields(ModelConfig)} == pinned


def test_basketball_default_differs_from_football_only_in_solver_basis_and_flat_rule():
    differing = {
        f.name for f in dataclasses.fields(ModelConfig)
        if getattr(BASKETBALL_DEFAULT, f.name) != getattr(FOOTBALL_COMPAT, f.name)
    }
    assert differing == {"solver", "margin_scale_basis", "flat_rating_rule"}
    assert BASKETBALL_DEFAULT.flat_rating_rule == "min_feasible"
    assert BASKETBALL_DEFAULT.solver == "CLARABEL" != FOOTBALL_COMPAT.solver
    assert BASKETBALL_DEFAULT.accept_inaccurate is False


def test_margin_scale_basis_is_chosen_in_one_place():
    source = open(config_module.__file__).read()
    assert "PENDING OWNER DECISION" not in source  # decided 2026-11-23: the documented formula
    assert config_module.BASKETBALL_MARGIN_SCALE_BASIS == "margin"
    assert source.count("BASKETBALL_MARGIN_SCALE_BASIS:") == 1
    assert BASKETBALL_DEFAULT.margin_scale_basis == config_module.BASKETBALL_MARGIN_SCALE_BASIS


@pytest.mark.parametrize("week, expected", [(1, 6 / 700), (3, 4 / 700), (6, 1 / 700), (7, 0.0), (8, 0.0), (19, 0.0)])
def test_prior_lambda_fades_to_zero_at_the_fade_week(week, expected):
    assert FOOTBALL_COMPAT.prior_lambda(week) == pytest.approx(expected, abs=0, rel=1e-15)


@pytest.mark.parametrize("week", [0, -1, None, 1.5, "3", True])
def test_week_must_be_an_int_at_least_one(week):
    with pytest.raises(ValueError):
        FOOTBALL_COMPAT.prior_lambda(week)


@pytest.mark.parametrize("bad", [
    {"r_min": 5, "r_max": 1},
    {"r_lower_min": 20},
    {"gamma_margin": -1},
    {"target_gap_for_median": 0},
    {"max_gap_from_margin": 0},
    {"sqrt_floor": 0},
    {"prior_fade_weeks": 0},
    {"prior_fade_scale": 0},
    {"r_max": float("nan")},
    {"margin_scale_basis": "median"},
    {"solver": ""},
    {"accept_inaccurate": "yes"},
    {"flat_rating_rule": "max"},
    {"flat_rating_rule": "min_feasible", "gamma_loss": 0},
])
def test_invalid_configs_are_rejected(bad):
    with pytest.raises(ModelConfigError):
        dataclasses.replace(FOOTBALL_COMPAT, **bad)


def test_config_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        FOOTBALL_COMPAT.r_max = 5
