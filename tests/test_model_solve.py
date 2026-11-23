"""Behavior of `solve` and `rate_week` on tiny problems whose answers can be reasoned out."""
import dataclasses
import random

import cvxpy as cp
import numpy as np
import pytest
from scipy.optimize import minimize

from ingest.derive import compute_alpha, derive_result
from model.assemble import Game, LowerLoss, assemble
from model.config import FOOTBALL_COMPAT
from model.pipeline import NoTeamsError, rate_week
from model.solve import ModelSolveError, solve
from tests.football_adapter import build

# Explicit config so these tests do not depend on the provisional basketball basis choice.
CFG = dataclasses.replace(FOOTBALL_COMPAT, solver="CLARABEL", margin_scale_basis="margin")
LOWER_ID = 900


def run(games, lower, teams, priors=None, week=10, config=CFG, records=None):
    if records is None:
        records = {t: [0, 0] for t in teams}
        for g in games:
            records[g.winner][0] += 1
            records[g.loser][1] += 1
        for x in lower:
            records[x.team][1] += 1
    return solve(games, lower, records, teams, priors, week, config)


def test_lower_division_loss_lowers_the_loser_and_wins_do_not_enter_the_objective():
    teams = [1, 2, 3]
    games = [Game(1, 2, 30, 1.0), Game(1, 3, 30, 1.0), Game(2, 3, 5, 1.0)]
    records = {1: (2, 0), 2: (1, 1), 3: (0, 2)}  # same records in both runs: only the lower term differs
    without = solve(games, [], records, teams, None, 10, CFG)
    with_loss = solve(games, [LowerLoss(1, 20, 1.2)], records, teams, None, 10, CFG)
    assert with_loss.lower_term > 0 and without.lower_term == 0
    assert with_loss.ratings[1] < without.ratings[1]
    # A lower-division WIN is not an objective input at all; through assemble it only reaches the record.
    win = derive_result("final", 1, LOWER_ID, 80, 60, False)[0]
    rows = [{"id": 1, "home_team_id": 1, "away_team_id": LOWER_ID, **win}]
    a = assemble(rows, teams)
    assert a.games == [] and a.lower_losses == [] and a.records[1] == (1, 0)


def test_lower_division_term_uses_raw_margin_times_alpha_without_scaling_or_cap():
    # One D-I team that lost to the dummy by 40 at alpha 1.2 (gap 48, far above the 20 cap).
    # With r_lower pinned at its bound 15 the minimiser of 5*pos(r + 48 - 15)^2 + 0.5*(1*r)^2 is
    # r = 0.01 (floor), and the term value is 5 * (0.01 + 48 - 15)^2.
    res = solve([], [LowerLoss(1, 40, 1.2)], {1: (0, 1)}, [1], None, 10, CFG)
    assert res.ratings[1] == pytest.approx(0.01, abs=1e-6)
    assert res.r_lower == pytest.approx(15.0, abs=1e-6)
    assert res.lower_term == pytest.approx(5 * (0.01 + 48 - 15) ** 2, rel=1e-6)


def test_home_win_neutral_and_away_win_alpha_change_the_demanded_gap():
    gaps = {}
    for label, neutral, home_won in (("home", False, True), ("neutral", True, True), ("away", False, False)):
        alpha = compute_alpha(home_won, neutral)
        res = solve([Game(1, 2, 9, alpha)], [], {1: (1, 0), 2: (0, 1)}, [1, 2], {1: 1.0, 2: 1.0}, 1, CFG)
        gaps[label] = res.ratings[1] - res.ratings[2]
    # alpha 0.8 < 1.0 < 1.2: an away win demands the biggest gap, a home win the smallest
    assert gaps["home"] < gaps["neutral"] < gaps["away"]


def test_prior_pulls_toward_priors_before_the_fade_week_and_vanishes_at_it():
    # A team with no games: only the prior term acts on it.
    for week in range(1, 7):
        res = solve([], [], {1: (0, 0)}, [1], {1: 50.0}, week, CFG)
        assert res.lam == pytest.approx((7 - week) / 700)
        assert res.ratings[1] == pytest.approx(50.0, abs=1e-4)
        assert res.prior_term == pytest.approx(0, abs=1e-6)
    for week in (7, 8, 19):
        res = solve([], [], {1: (0, 0)}, [1], {1: 50.0}, week, CFG)
        assert res.lam == 0.0 and res.prior_term == 0.0


def test_prior_changes_the_solution_only_while_lambda_is_positive():
    teams = [1, 2, 3]
    games = [Game(1, 2, 10, 1.0), Game(2, 3, 10, 1.0), Game(3, 1, 3, 1.0)]
    priors = {1: 40.0, 2: 1.0, 3: 1.0}
    early = solve(games, [], {t: (1, 1) for t in teams}, teams, priors, 1, CFG)
    early_default = solve(games, [], {t: (1, 1) for t in teams}, teams, {}, 1, CFG)
    assert early.ratings[1] > early_default.ratings[1] + 0.1
    late_a = solve(games, [], {t: (1, 1) for t in teams}, teams, priors, 7, CFG)
    late_b = solve(games, [], {t: (1, 1) for t in teams}, teams, {}, 7, CFG)
    assert late_a.ratings == late_b.ratings


def test_priors_for_absent_teams_use_default_prior_and_extra_priors_are_ignored():
    res = solve([], [], {1: (0, 0), 2: (0, 0)}, [1, 2], {1: 30.0, 99: 77.0}, 1, CFG)
    assert res.ratings[1] == pytest.approx(30.0, abs=1e-4)
    assert res.ratings[2] == pytest.approx(1.0, abs=1e-4)  # default_prior
    assert res.n_default_priors == 1 and 99 not in res.ratings


def test_team_with_no_games_is_a_variable_with_a_defined_rating():
    teams = [1, 2, 3]
    games = [Game(1, 2, 10, 1.0)]
    res = solve(games, [], {1: (1, 0), 2: (0, 1), 3: (0, 0)}, teams, {}, 1, CFG)
    assert set(res.ratings) == set(teams)
    assert res.ratings[3] == pytest.approx(1.0, abs=1e-4)  # pulled to default_prior while lambda > 0
    late = solve(games, [], {1: (1, 0), 2: (0, 1), 3: (0, 0)}, teams, {}, 10, CFG)
    assert CFG.r_min - 1e-6 <= late.ratings[3] <= CFG.r_max + 1e-6  # flat, but inside the bounds


def test_season_without_lower_division_losses_solves():
    res = run([Game(1, 2, 10, 1.0), Game(2, 1, 4, 1.0)], [], [1, 2])
    assert res.status == "optimal" and res.n_lower_losses == 0 and res.lower_term == 0.0


def test_no_games_and_no_records_solves_to_a_valid_answer():
    res = solve([], [], {}, [1, 2], None, 10, CFG)
    assert res.status == "optimal" and res.objective == 0.0 and res.median_used == CFG.median_fallback
    assert res.k_margin == pytest.approx(7.0)  # fallback median 1.0
    assert set(res.ratings) == {1, 2}


def test_disconnected_schedule_solves_and_dummy_is_never_a_rating():
    games = [Game(1, 2, 10, 1.0), Game(2, 1, 3, 1.2), Game(3, 4, 12, 0.8), Game(4, 3, 6, 1.0)]
    res = run(games, [LowerLoss(2, 8, 1.0)], [1, 2, 3, 4])
    assert res.status == "optimal"
    assert set(res.ratings) == {1, 2, 3, 4}
    assert LOWER_ID not in res.ratings and isinstance(res.r_lower, float)


def test_first_season_with_no_prior_data_rates_every_team_from_default_prior(store):
    ids, team_rows, game_rows = build()
    season = 2024
    store.upsert("teams", [t for t in team_rows if t["season"] == season], ("id", "season"))
    store.upsert("games", [g for g in game_rows if g["season"] == season], ("id",))
    out = rate_week(store, season, 3, FOOTBALL_COMPAT)
    assert out.prior_solve is None
    assert out.solve.n_default_priors == len(out.ratings)
    assert any("no prior-season data" in w for w in out.warnings)
    assert out.solve.lam == pytest.approx(4 / 700)


def test_rate_week_skips_the_prior_solve_after_the_fade_week(store, monkeypatch):
    ids, team_rows, game_rows = build()
    store.upsert("teams", team_rows, ("id", "season"))
    store.upsert("games", game_rows, ("id",))
    import model.pipeline as pipeline

    calls = []
    real = pipeline.solve
    monkeypatch.setattr(pipeline, "solve", lambda *a, **k: calls.append(a[4]) or real(*a, **k))
    rate_week(store, 2024, 6, FOOTBALL_COMPAT)
    assert [c is None for c in calls] == [True, False]  # prior solve (priors None), then main
    calls.clear()
    out = rate_week(store, 2024, 7, FOOTBALL_COMPAT)
    assert len(calls) == 1 and out.prior_solve is None


def test_rate_week_rejects_bad_input(store):
    with pytest.raises(ValueError):
        rate_week(store, 2024, 0)
    with pytest.raises(NoTeamsError):
        rate_week(store, 2024, 1)


def test_rate_week_rates_a_team_with_no_games(store):
    ids, team_rows, game_rows = build()
    team_rows = team_rows + [{"id": 5000, "season": 2024, "school": "Idle", "conference_id": 1, "conference": "X"}]
    store.upsert("teams", team_rows, ("id", "season"))
    store.upsert("games", game_rows, ("id",))
    out = rate_week(store, 2024, 3, FOOTBALL_COMPAT)
    assert out.records[5000] == (0, 0) and 5000 in out.ratings


@pytest.mark.parametrize("status", ["infeasible", "unbounded", "optimal_inaccurate", "solver_error"])
def test_non_optimal_status_raises(monkeypatch, status):
    real = cp.Problem.solve

    def fake(self, *a, **k):
        value = real(self, *a, **k)
        self._status = status
        return value

    monkeypatch.setattr(cp.Problem, "solve", fake)
    with pytest.raises(ModelSolveError, match=status):
        run([Game(1, 2, 10, 1.0)], [], [1, 2])


def test_accept_inaccurate_flag_allows_only_optimal_inaccurate(monkeypatch):
    real = cp.Problem.solve

    def make(status):
        def fake(self, *a, **k):
            value = real(self, *a, **k)
            self._status = status
            return value
        return fake

    lenient = dataclasses.replace(CFG, accept_inaccurate=True)
    monkeypatch.setattr(cp.Problem, "solve", make("optimal_inaccurate"))
    res = run([Game(1, 2, 10, 1.0)], [], [1, 2], config=lenient)
    assert res.status == "optimal_inaccurate" and res.warnings
    monkeypatch.setattr(cp.Problem, "solve", make("infeasible"))
    with pytest.raises(ModelSolveError):
        run([Game(1, 2, 10, 1.0)], [], [1, 2], config=lenient)


def test_solver_errors_become_model_solve_errors():
    with pytest.raises(ModelSolveError):
        run([Game(1, 2, 10, 1.0)], [], [1, 2], config=dataclasses.replace(CFG, solver="NO_SUCH_SOLVER"))


def test_same_inputs_in_shuffled_row_order_give_identical_ratings():
    ids, team_rows, game_rows = build()
    teams = [t["id"] for t in team_rows if t["season"] == 2024]
    rows = [g for g in game_rows if g["season"] == 2024 and g["is_rated"]]
    base = assemble(rows, teams)
    expected = solve(base.games, base.lower_losses, base.records, teams, {}, 5, CFG)
    for seed in range(3):
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)
        a = assemble(shuffled, teams)
        got = solve(a.games, a.lower_losses, a.records, teams, {}, 5, CFG)
        assert got.ratings == expected.ratings and got.objective == expected.objective


def test_inputs_must_be_consistent():
    with pytest.raises(ValueError, match="not in team_ids"):
        solve([Game(1, 3, 5, 1.0)], [], {}, [1, 2], None, 1, CFG)
    with pytest.raises(ValueError):
        solve([], [], {}, [], None, 1, CFG)
    with pytest.raises(ValueError):
        solve([], [], {}, [1], None, 0, CFG)


# --- margin_scale_basis -------------------------------------------------------------------

# Hand calculation. Margins 1, 4, 9, 400 -> median (4 + 9) / 2 = 6.5; k = 7 / sqrt(6.5) = 2.74563.
# Demanded gaps min(k * sqrt(margin), 20) * alpha: margin 1 -> k = 2.7456 (x1.0); margin 4 -> 2k = 5.4913
# (x0.8); margin 9 -> 3k = 8.2369 (x1.2); margin 400 -> 20k = 54.9 -> capped at 20 (x1.0).
HAND_GAMES = [Game(1, 2, 1, 1.0), Game(2, 3, 4, 0.8), Game(3, 4, 9, 1.2), Game(1, 4, 400, 1.0)]
HAND_RECORDS = {1: (2, 0), 2: (1, 1), 3: (1, 1), 4: (0, 2)}
HAND_PRIORS = {t: 1.0 for t in (1, 2, 3, 4)}


def independent_minimum(demands, priors_weight):
    """Minimise the hand-written objective for HAND_GAMES with scipy (no CVXPY, no model code)."""
    pairs = [(1, 2), (2, 3), (3, 4), (1, 4)]  # (winner, loser)
    loss_rate = {1: 0.0, 2: 0.5, 3: 0.5, 4: 1.0}

    def f(r):
        rating = dict(zip((1, 2, 3, 4), r))
        total = 0.0
        for (w, l), d in zip(pairs, demands):
            total += 0.05 * max(0.0, rating[l] + d - rating[w]) ** 2
        total += sum(0.5 * (loss_rate[t] * rating[t]) ** 2 for t in rating)
        total += priors_weight * sum((rating[t] - 1.0) ** 2 for t in rating)
        return total

    out = minimize(f, np.full(4, 5.0), bounds=[(0.01, 100)] * 4, method="L-BFGS-B", options={"ftol": 1e-15, "gtol": 1e-10})
    return out.fun, dict(zip((1, 2, 3, 4), out.x))


def test_margin_basis_uses_the_median_of_real_margins_hand_calculated():
    res = solve(HAND_GAMES, [], HAND_RECORDS, [1, 2, 3, 4], HAND_PRIORS, 1, CFG)
    k = 7 / np.sqrt(6.5)
    assert res.margin_scale_basis == "margin"
    assert res.median_used == 6.5
    assert res.k_margin == pytest.approx(k, rel=1e-12) and res.k_margin == pytest.approx(2.74563, abs=1e-5)
    assert res.n_capped == 1  # only the 400-point game
    demands = [k * 1 * 1.0, k * 2 * 0.8, k * 3 * 1.2, 20.0 * 1.0]
    fun, ratings = independent_minimum(demands, 6 / 700)
    assert res.objective == pytest.approx(fun, rel=1e-6)
    for t in (1, 2, 3, 4):
        assert res.ratings[t] == pytest.approx(ratings[t], abs=1e-3)


def test_alpha_basis_uses_the_median_of_alpha_on_the_same_games():
    cfg = dataclasses.replace(CFG, margin_scale_basis="alpha")
    res = solve(HAND_GAMES, [], HAND_RECORDS, [1, 2, 3, 4], HAND_PRIORS, 1, cfg)
    assert res.median_used == 1.0  # median(0.8, 1.0, 1.0, 1.2): football's actual quantity
    assert res.k_margin == pytest.approx(7.0)
    assert res.n_capped == 2  # 7*sqrt(9)=21 and 7*sqrt(400)=140 exceed the cap
    # k = 7 gives the margin-1 game gap 7*1 = 7, not 2.7456, so the two bases are visibly different.
    base = solve(HAND_GAMES, [], HAND_RECORDS, [1, 2, 3, 4], HAND_PRIORS, 1, CFG)
    assert res.objective != pytest.approx(base.objective, rel=1e-3)


def test_margin_basis_fallback_and_floor():
    assert solve([], [], {}, [1], None, 1, CFG).median_used == CFG.median_fallback
    zero = dataclasses.replace(CFG, median_fallback=0.0)
    res = solve([], [], {}, [1], None, 1, zero)
    assert res.k_margin == pytest.approx(7.0 / 1e-6)  # sqrt floor
    # a median of 0 (all margins 0) is floored the same way
    assert solve([Game(1, 2, 0, 1.0)], [], {1: (1, 0), 2: (0, 1)}, [1, 2], None, 1, CFG).k_margin == pytest.approx(7.0 / 1e-6)


def test_basis_switch_is_live_on_the_football_fixture():
    ids, team_rows, game_rows = build()
    teams = [t["id"] for t in team_rows if t["season"] == 2024]
    a = assemble([g for g in game_rows if g["season"] == 2024 and g["is_rated"]], teams)
    by_alpha = solve(a.games, a.lower_losses, a.records, teams, {}, 15, FOOTBALL_COMPAT)
    by_margin = solve(a.games, a.lower_losses, a.records, teams, {}, 15, dataclasses.replace(FOOTBALL_COMPAT, margin_scale_basis="margin"))
    assert by_alpha.median_used in (0.8, 0.9, 1.0, 1.1, 1.2)
    assert by_margin.median_used == float(np.median([g.margin for g in a.games])) > 5
    assert by_margin.k_margin < by_alpha.k_margin
    assert by_margin.n_capped < by_alpha.n_capped
    assert by_margin.objective != pytest.approx(by_alpha.objective, rel=1e-3)
