"""A seeded synthetic basketball-sized season (365 teams, about 5,800 rated games, 12 D-I losses to
lower-division teams) solved with the production default config. Vectorized construction keeps this
under a few seconds, so it runs in the default suite."""
import dataclasses
import time

import cvxpy as cp
import numpy as np
import pytest

from model.config import BASKETBALL_DEFAULT
from model.pipeline import rate_week
from tests.reference.measure_solvers import synthetic

SEASON = 2025
LOWER_OFFSET = 100_000


def season_rows(games, teams, season):
    ids = {n: int(i) for n, i in zip(teams[teams.season == season].school, teams[teams.season == season].id)}
    rows = []
    for r in games[games.season == season].itertuples():
        def tid(name):
            return ids[name] if name in ids else LOWER_OFFSET + int(name.removeprefix("Lower"))

        home, away = tid(r.home_team), tid(r.away_team)
        home_won = r.home_score > r.away_score
        rows.append({
            "id": int(r.id) + season * 10_000, "season": season, "week": int(r.week), "game_date": "2000-01-01",
            "start_date": "2000-01-01T00:00:00Z", "season_type": "regular", "game_type": "STD", "is_rated": 1,
            "status": "final", "home_team_id": home, "home_team": r.home_team, "away_team_id": away,
            "away_team": r.away_team, "home_score": int(r.home_score), "away_score": int(r.away_score),
            "neutral_site": int(r.neutral_site), "conference_game": 0, "margin": abs(int(r.home_score) - int(r.away_score)),
            "winner_team_id": home if home_won else away,
            "alpha": 1.0 if r.neutral_site else (0.8 if home_won else 1.2),
        })
    return ids, rows


@pytest.fixture(scope="module")
def loaded_store():
    from database.local import LocalStore

    games, teams = synthetic()
    store = LocalStore(":memory:")
    for season in (SEASON - 1, SEASON):
        ids, rows = season_rows(games, teams, season)
        store.upsert("teams", [{"id": i, "season": season, "school": n, "conference_id": 1, "conference": "X"} for n, i in ids.items()], ("id", "season"))
        store.upsert("games", rows, ("id",))
    yield store
    store.close()


@pytest.mark.parametrize("week", [3, 19])
def test_basketball_sized_season_solves_optimal_with_the_default_config(loaded_store, week):
    start = time.perf_counter()
    out = rate_week(loaded_store, SEASON, week, BASKETBALL_DEFAULT)
    elapsed = time.perf_counter() - start
    assert out.solve.status == "optimal" and out.solve.solver == "OSQP"
    assert len(out.published_ratings) == len(out.raw_ratings) == 365
    if week == 3:
        # Without the former rating floor OSQP now reaches `optimal` on this synthetic prior season
        # too (with the floor it hit its iteration cap); the fallback is exercised below.
        assert out.prior_solve is not None and out.prior_solve.status == "optimal"
        assert out.prior_solve.solver == "OSQP" and out.prior_solve.warnings == []
        assert out.prior_solve.n_games > 5000
    else:
        assert out.prior_solve is None
        assert out.solve.n_games > 5000 and out.solve.n_lower_losses == 12
    raw = np.array(list(out.raw_ratings.values()))
    assert raw.min() < 0 and raw.max() <= BASKETBALL_DEFAULT.r_max + 1e-6  # no floor: the bottom runs negative
    assert min(out.published_ratings.values()) == pytest.approx(BASKETBALL_DEFAULT.display_min, abs=1e-12)
    assert elapsed < 30  # measured about 1 s; the scalar-built football formulation needed 12-17 s per problem


@pytest.fixture
def osqp_never_optimal(monkeypatch):
    """Deterministic trigger for the fallback: every OSQP solve reports `optimal_inaccurate`."""
    real = cp.Problem.solve

    def fake(self, *a, **k):
        value = real(self, *a, **k)
        if k.get("solver") == "OSQP":
            self._status = cp.OPTIMAL_INACCURATE
        return value

    monkeypatch.setattr(cp.Problem, "solve", fake)


def test_a_non_optimal_osqp_solve_falls_back_to_clarabel_with_the_least_optimal_rule(loaded_store, request):
    week = 12  # lambda 0: unbeaten teams are undetermined, the case the fallback's rule is for
    osqp = rate_week(loaded_store, SEASON, week, BASKETBALL_DEFAULT)
    request.getfixturevalue("osqp_never_optimal")
    out = rate_week(loaded_store, SEASON, week, BASKETBALL_DEFAULT)
    assert out.solve.status == "optimal" and out.solve.solver == "CLARABEL"
    assert out.solve.flat_rating_rule == "min_feasible" and len(out.solve.flat_team_ids) >= 3
    assert any("fallback CLARABEL" in w for w in out.warnings)
    flat = set(out.solve.flat_team_ids)
    det = max(abs(out.raw_ratings[t] - osqp.raw_ratings[t]) for t in out.raw_ratings if t not in flat)
    flt = max(abs(out.raw_ratings[t] - osqp.raw_ratings[t]) for t in flat)
    # Determined ratings agree to OSQP's accuracy (measured 3.1e-4). Undetermined ones: the fallback
    # publishes their least optimal value, which OSQP approaches from above (it stops once their win
    # hinges are about zero): measured up to 0.64 apart on this synthetic season, against 84-90
    # points for CLARABEL's own (interior) choice.
    assert det < 1e-2 and flt < 1.0, (det, flt)
    assert all(osqp.raw_ratings[t] >= out.raw_ratings[t] - 1e-2 for t in flat)
    assert out.display_shift == pytest.approx(osqp.display_shift, abs=1e-2)


def test_without_a_fallback_a_non_optimal_solve_raises(loaded_store, osqp_never_optimal):
    from model.solve import ModelSolveError

    with pytest.raises(ModelSolveError, match="not 'optimal'"):
        rate_week(loaded_store, SEASON, 3, dataclasses.replace(BASKETBALL_DEFAULT, fallback_solver=None))


def test_when_the_fallback_fails_too_the_error_names_both_solvers(loaded_store, monkeypatch):
    from model.solve import SolverStatusError

    real = cp.Problem.solve

    def fake(self, *a, **k):
        value = real(self, *a, **k)
        self._status = cp.OPTIMAL_INACCURATE
        return value

    monkeypatch.setattr(cp.Problem, "solve", fake)
    with pytest.raises(SolverStatusError, match=r"OSQP.*fallback CLARABEL also failed: .*CLARABEL"):
        rate_week(loaded_store, SEASON, 3, BASKETBALL_DEFAULT)
