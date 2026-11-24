"""`flat_rating_rule`: ratings the objective leaves undetermined (no loss at lambda 0, and r_lower
with no active lower-division hinge) are published at their least optimal value under
"min_feasible", and left to the solver under "solver" (football, FOOTBALL_COMPAT).

The characterization these tests check (model/README.md, "Undetermined ratings"): every team with
a loss is fixed by its loss-rate term, so it has one value over the whole optimal set; a team with
no loss appears only as the winner of hinges against such teams, so its optimal values form the
interval [max(T, r_min), r_max] with T = max over its D-I wins of (r_loser + demanded gap), or just
{r_max} when T >= r_max."""
import dataclasses
import random

import numpy as np
import pytest

from ingest.derive import derive_result
from model.assemble import Game, LowerLoss, assemble
from model.config import BASKETBALL_DEFAULT, FOOTBALL_COMPAT
from model.pipeline import rate_week
from model.solve import solve
from tests.football_adapter import build
from tests.reference.measure_solvers import synthetic
from tests.test_model_scale import season_rows

MF = dataclasses.replace(FOOTBALL_COMPAT, solver="CLARABEL", margin_scale_basis="margin", flat_rating_rule="min_feasible")
SOLVER_RULE = dataclasses.replace(MF, flat_rating_rule="solver")
EXACT = 1e-9  # closed-form arithmetic on the solver's own determined ratings: float rounding only


def records_of(games, lower=(), extra=None):
    records = {}
    for g in games:
        records.setdefault(g.winner, [0, 0])[0] += 1
        records.setdefault(g.loser, [0, 0])[1] += 1
    for x in lower:
        records.setdefault(x.team, [0, 0])[1] += 1
    records.update(extra or {})
    return {t: tuple(v) for t, v in records.items()}


def both(games, lower, teams, week=10, priors=None, records=None):
    records = records_of(games, lower) if records is None else records
    mf = solve(games, lower, records, teams, priors, week, MF)
    sv = solve(games, lower, records, teams, priors, week, SOLVER_RULE)
    return mf, sv


def demand(res, margin, alpha):
    return min(res.k_margin * np.sqrt(margin), MF.max_gap_from_margin) * alpha


def assert_determined_unchanged(mf, sv):
    flat = set(mf.flat_team_ids)
    assert mf.flat_team_ids == sv.flat_team_ids
    for t in mf.ratings:
        if t not in flat:
            assert mf.ratings[t] == sv.ratings[t], t  # bit for bit: same first-stage solve
    assert mf.objective == sv.objective
    assert abs(mf.flat_objective_change) <= 1e-8 * max(1.0, abs(mf.objective))


# --- hand-computed cases -------------------------------------------------------------------

# Teams 2 and 3 split two games and both lose to 1, so they have losses and are determined; every
# margin is 9, so the median is 9, k = 7/3 and a neutral 9-point win demands exactly 7 points.
A_GAMES = [Game(1, 2, 9, 1.0), Game(2, 3, 9, 1.0), Game(3, 2, 9, 1.0)]


def test_one_win_unbeaten_team_is_rated_exactly_the_demanded_gap_above_its_victim():
    mf, sv = both(A_GAMES, [], [1, 2, 3])
    assert mf.k_margin == pytest.approx(7 / 3, rel=1e-15)
    assert mf.flat_team_ids == (1,)
    assert mf.ratings[1] == pytest.approx(mf.ratings[2] + 7.0, abs=EXACT)
    assert mf.margin_term == pytest.approx(sv.margin_term, rel=1e-8, abs=1e-12)  # its hinge is 0 either way
    assert_determined_unchanged(mf, sv)
    assert mf.max_flat_adjustment == pytest.approx(abs(sv.ratings[1] - mf.ratings[1]), abs=EXACT)


def test_several_wins_the_binding_win_is_the_max_of_loser_rating_plus_demanded_gap():
    # 2 is strong (it beats 3, 4, 5 big); 5 is weak. Team 1 beats 2 by 4 on a neutral floor and
    # beats 5 by 9 at home (alpha 0.8): the narrower win over the strong team binds.
    games = [
        Game(1, 2, 4, 1.0), Game(1, 5, 9, 0.8),
        Game(2, 3, 20, 1.0), Game(2, 4, 20, 1.0), Game(2, 5, 20, 1.0), Game(3, 2, 2, 1.0),
        Game(3, 4, 6, 1.0), Game(4, 5, 6, 1.0), Game(5, 3, 1, 1.2),
    ]
    mf, sv = both(games, [], [1, 2, 3, 4, 5])
    candidates = {2: mf.ratings[2] + demand(mf, 4, 1.0), 5: mf.ratings[5] + demand(mf, 9, 0.8)}
    assert candidates[2] > candidates[5]
    assert mf.ratings[1] == pytest.approx(max(candidates.values()), abs=EXACT)
    assert mf.flat_team_ids == (1,)
    assert_determined_unchanged(mf, sv)


def test_capped_margin_demands_the_cap_times_alpha():
    # median of (9, 9, 400) is 9 -> k = 7/3; 400 points would demand 46.7, capped at 20, x1.2 away.
    games = [Game(1, 2, 400, 1.2), Game(2, 3, 9, 1.0), Game(3, 2, 9, 1.0)]
    mf, sv = both(games, [], [1, 2, 3])
    assert mf.n_capped == 1
    assert mf.ratings[1] == pytest.approx(mf.ratings[2] + 24.0, abs=EXACT)
    assert_determined_unchanged(mf, sv)


def test_wins_only_over_lower_division_and_no_games_both_give_r_min():
    # Team 4 is 3-0, all against non-D-I opponents (record-only events); team 5 has no games.
    records = records_of(A_GAMES, extra={4: (3, 0), 5: (0, 0)})
    mf, sv = both(A_GAMES, [], [1, 2, 3, 4, 5], records=records)
    assert set(mf.flat_team_ids) == {1, 4, 5}
    assert mf.ratings[4] == MF.r_min and mf.ratings[5] == MF.r_min
    assert_determined_unchanged(mf, sv)


def test_unbeaten_teams_in_disconnected_components_each_get_their_own_threshold():
    games = A_GAMES + [Game(11, 12, 1, 1.0), Game(12, 13, 25, 1.0), Game(13, 12, 4, 0.8)]
    mf, sv = both(games, [], [1, 2, 3, 11, 12, 13])
    assert mf.flat_team_ids == (1, 11)
    assert mf.ratings[1] == pytest.approx(mf.ratings[2] + demand(mf, 9, 1.0), abs=EXACT)
    assert mf.ratings[11] == pytest.approx(mf.ratings[12] + demand(mf, 1, 1.0), abs=EXACT)
    assert_determined_unchanged(mf, sv)


def test_unbeaten_team_whose_threshold_exceeds_r_max_is_determined_and_kept():
    # A chain of 400-point neutral wins (capped gap 20 each; ten 1-point games between 8 and 9 make
    # the median 1, k = 7) with tiny loss rates pushes team 2 to about 81; team 1 then needs
    # r_2 + 20 > r_max, so r_max is its unique optimum (its hinge stays positive).
    chain = [2, 3, 4, 5, 6, 7]
    games = ([Game(1, 2, 400, 1.0)] + [Game(a, b, 400, 1.0) for a, b in zip(chain, chain[1:])]
             + [Game(7, 6, 1, 1.0)] + [Game(8, 9, 1, 1.0), Game(9, 8, 1, 1.0)] * 5)
    records = records_of(games, extra={t: (1000, 1) for t in chain})
    mf, sv = both(games, [], [1] + chain + [8, 9], records=records)
    assert mf.flat_team_ids == (1,)
    assert mf.ratings[2] + 20 > MF.r_max
    assert mf.ratings[1] == sv.ratings[1] == pytest.approx(MF.r_max, abs=1e-6)
    assert mf.max_flat_adjustment == 0.0
    assert_determined_unchanged(mf, sv)


def test_r_lower_takes_its_least_optimal_value():
    # Inactive hinge: team 2 lost to the lower-division side by 2 at home (alpha 1.2 for the away
    # winner): least r_lower = r_2 + 2.4, far below the bound 15.
    lower = [LowerLoss(2, 2, 1.2)]
    mf, sv = both(A_GAMES, lower, [1, 2, 3])
    assert mf.r_lower == pytest.approx(mf.ratings[2] + 2.4, abs=EXACT) and mf.lower_term == 0.0
    assert mf.r_lower_adjustment == pytest.approx(abs(sv.r_lower - mf.r_lower), abs=EXACT)
    # No lower-division loss at all: the bound r_lower_min.
    assert both(A_GAMES, [], [1, 2, 3])[0].r_lower == MF.r_lower_min
    # Active hinge (r_2 + 48 > 15): determined at the upper bound; the solver's value is kept.
    mf, sv = both(A_GAMES, [LowerLoss(2, 40, 1.2)], [1, 2, 3])
    assert mf.r_lower == sv.r_lower == pytest.approx(MF.r_lower_max, abs=1e-6) and mf.lower_term > 0
    # The r_lower rule also applies while lambda > 0 (r_lower has no prior term).
    early, _ = both(A_GAMES, lower, [1, 2, 3], week=2, priors={})
    assert early.flat_team_ids == () and early.r_lower == pytest.approx(early.ratings[2] + 2.4, abs=EXACT)


def test_rule_is_a_no_op_while_the_prior_is_on():
    for week in range(1, 7):
        mf, sv = both(A_GAMES, [], [1, 2, 3], week=week, priors={1: 30.0})
        assert mf.lam > 0 and mf.flat_team_ids == () and mf.max_flat_adjustment == 0.0
        assert mf.ratings == sv.ratings and mf.objective == sv.objective


def test_records_must_show_the_losses_the_games_contain():
    with pytest.raises(ValueError, match="no loss"):
        solve(A_GAMES, [], {1: (1, 0), 2: (0, 0), 3: (1, 1)}, [1, 2, 3], None, 10, MF)
    with pytest.raises(ValueError, match="no loss"):
        solve([], [LowerLoss(1, 3, 1.0)], {1: (2, 0)}, [1], None, 10, MF)
    solve(A_GAMES, [], {1: (1, 0), 2: (0, 0), 3: (1, 1)}, [1, 2, 3], None, 10, SOLVER_RULE)  # football's rule does not check


# --- the football fixture ------------------------------------------------------------------

IDS, TEAM_ROWS, GAME_ROWS = build()
TEAMS_2024 = [t["id"] for t in TEAM_ROWS if t["season"] == 2024]
TEAMS_2023 = [t["id"] for t in TEAM_ROWS if t["season"] == 2023]


def fixture_week(week, rows=None):
    rows = rows or [g for g in GAME_ROWS if g["season"] == 2024 and g["is_rated"] and g["week"] <= week]
    return assemble(rows, TEAMS_2024)


@pytest.fixture(scope="module")
def fixture_priors():
    a = assemble([g for g in GAME_ROWS if g["season"] == 2023 and g["is_rated"]], TEAMS_2023)
    return solve(a.games, a.lower_losses, a.records, TEAMS_2023, None, 1, MF).ratings


def assert_least_optimal(res, a, config):
    """Certificate: each flat team sits exactly at max(r_min, max over wins of r_loser + gap), so
    its win hinges are all zero and lowering it by any amount would make one positive."""
    k = res.k_margin
    for t in res.flat_team_ids:
        gaps = [res.ratings[g.loser] + min(k * np.sqrt(g.margin), config.max_gap_from_margin) * g.alpha
                for g in a.games if g.winner == t]
        least = max([config.r_min] + gaps)
        if least < config.r_max:
            assert res.ratings[t] == pytest.approx(least, abs=EXACT), t
        assert all(x - res.ratings[t] <= EXACT for x in gaps), t


@pytest.mark.parametrize("week", range(1, 16))
def test_fixture_every_week_objective_and_determined_ratings_unchanged(fixture_priors, week):
    a = fixture_week(week)
    mf = solve(a.games, a.lower_losses, a.records, TEAMS_2024, fixture_priors, week, MF)
    sv = solve(a.games, a.lower_losses, a.records, TEAMS_2024, fixture_priors, week, SOLVER_RULE)
    assert_determined_unchanged(mf, sv)
    assert_least_optimal(mf, a, MF)
    values = np.array(list(mf.ratings.values()))
    assert values.min() >= MF.r_min - 1e-6 and values.max() <= MF.r_max + 1e-6
    assert MF.r_lower_min - 1e-6 <= mf.r_lower <= MF.r_lower_max + 1e-6
    if week < 7:
        assert mf.flat_team_ids == () and mf.ratings == sv.ratings
    else:
        assert set(mf.flat_team_ids) == {t for t, (w, l) in a.records.items() if l == 0}


@pytest.mark.parametrize("week", [7, 10])
def test_fixture_min_feasible_is_solver_independent(week):
    """CLARABEL, OSQP and SCS agree on EVERY team under "min_feasible"; under "solver" the flat
    teams differ by tens of points. A flat rating is a max of (determined rating + constant), so
    its cross-solver difference cannot exceed the largest one among determined teams."""
    a = fixture_week(week)
    out = {
        (rule, s): solve(a.games, a.lower_losses, a.records, TEAMS_2024, {}, week,
                         dataclasses.replace(MF, solver=s, flat_rating_rule=rule))
        for rule in ("solver", "min_feasible") for s in ("CLARABEL", "OSQP", "SCS")
    }
    flat = set(out["min_feasible", "CLARABEL"].flat_team_ids)
    assert len(flat) >= 2
    for s in ("OSQP", "SCS"):
        base, other = out["min_feasible", "CLARABEL"].ratings, out["min_feasible", s].ratings
        det = max(abs(base[t] - other[t]) for t in base if t not in flat)
        flt = max(abs(base[t] - other[t]) for t in flat)
        assert det < 1e-3 and flt <= det + EXACT, (s, det, flt)  # measured: OSQP 1.8e-5 / 3.6e-5, SCS 4.7e-5 / 1.3e-4
        loose = max(abs(out["solver", "CLARABEL"].ratings[t] - out["solver", s].ratings[t]) for t in flat)
        assert loose > 10, (s, loose)  # measured 28 to 37


def test_fixture_min_feasible_is_independent_of_input_order():
    rows = [g for g in GAME_ROWS if g["season"] == 2024 and g["is_rated"] and g["week"] <= 10]
    a = fixture_week(10, rows)
    expected = solve(a.games, a.lower_losses, a.records, TEAMS_2024, {}, 10, MF)
    for seed in range(3):
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)
        b = fixture_week(10, shuffled)
        teams = TEAMS_2024[:]
        random.Random(seed).shuffle(teams)
        got = solve(b.games, b.lower_losses, b.records, teams, {}, 10, MF)
        assert got.ratings == expected.ratings and got.r_lower == expected.r_lower


# --- a seeded basketball-sized synthetic season --------------------------------------------

@pytest.fixture(scope="module")
def synthetic_season():
    games, teams = synthetic()  # seed 7: 365 teams, 5,800 rated D-I games over 19 weeks
    ids, rows = season_rows(games, teams, 2025)
    return sorted(ids.values()), rows


@pytest.mark.parametrize("week", [7, 10, 14])
def test_synthetic_basketball_season_is_solver_independent(synthetic_season, week):
    """OSQP and SCS both reach status `optimal` on these main solves (lambda 0; it is the prior
    solve at this size where OSQP hits its iteration cap), so production's optimal-only rule is
    kept. Determined ratings agree to OSQP's / SCS's own accuracy (measured up to 4.7e-3 and
    9.4e-3); the flat ones agree to no worse, while under "solver" they differ by 1 to 11 points."""
    teams, rows = synthetic_season
    a = assemble([r for r in rows if r["week"] <= week], teams)
    out = {
        (rule, s): solve(a.games, a.lower_losses, a.records, teams, {}, week,
                         dataclasses.replace(BASKETBALL_DEFAULT, solver=s, flat_rating_rule=rule))
        for rule in ("solver", "min_feasible") for s in ("CLARABEL", "OSQP", "SCS")
    }
    ref = out["min_feasible", "CLARABEL"]
    flat = set(ref.flat_team_ids)
    assert len(flat) >= 3
    assert_determined_unchanged(ref, out["solver", "CLARABEL"])
    assert_least_optimal(ref, a, BASKETBALL_DEFAULT)
    for s in ("OSQP", "SCS"):
        other = out["min_feasible", s]
        assert other.status == "optimal"
        det = max(abs(ref.ratings[t] - other.ratings[t]) for t in teams if t not in flat)
        flt = max(abs(ref.ratings[t] - other.ratings[t]) for t in flat)
        assert det < 2e-2 and flt <= det + EXACT, (s, det, flt)
        loose = max(abs(out["solver", "CLARABEL"].ratings[t] - out["solver", s].ratings[t]) for t in flat)
        assert loose > 0.5, (s, loose)


# --- the prior-season solve ----------------------------------------------------------------

def game_row(gid, season, week, home, away, home_score, away_score, neutral=False):
    result, anomaly = derive_result("final", home, away, home_score, away_score, neutral)
    assert anomaly is None
    return {
        "id": gid, "season": season, "week": week, "game_date": "2000-01-01",
        "start_date": "2000-01-01T00:00:00Z", "season_type": "regular", "game_type": "STD",
        "is_rated": 1, "status": "final", "home_team_id": home, "home_team": str(home),
        "away_team_id": away, "away_team": str(away), "neutral_site": int(neutral),
        "conference_game": 0, **result,
    }


@pytest.mark.parametrize("rule", ["min_feasible", "solver"])
def test_prior_season_unbeaten_team_gets_its_least_feasible_rating_as_its_prior(store, rule):
    # Season 2023: team 1 is unbeaten (beats 2 by 9 on a neutral floor); 2 and 3 split. In 2024 team
    # 1 has no games, so at week 1 its rating is pulled only to its prior.
    prior_rows = [game_row(1, 2023, 1, 1, 2, 79, 70, True), game_row(2, 2023, 2, 2, 3, 70, 61, True),
                  game_row(3, 2023, 3, 3, 2, 70, 61, True)]
    rows = [game_row(10, 2024, 1, 2, 3, 70, 61, True), game_row(11, 2024, 1, 3, 4, 75, 70)]
    teams = [{"id": t, "season": s, "school": f"T{t}", "conference_id": 1, "conference": "X"}
             for s in (2023, 2024) for t in (1, 2, 3, 4)]
    store.upsert("teams", teams, ("id", "season"))
    store.upsert("games", prior_rows + rows, ("id",))
    config = dataclasses.replace(MF, flat_rating_rule=rule)
    out = rate_week(store, 2024, 1, config)
    prior = out.prior_solve
    assert prior is not None and set(prior.flat_team_ids) == {1, 4}  # 4 has no 2023 games
    least = prior.ratings[2] + 7.0  # median margin 9 -> k = 7/3; a neutral 9-point win demands 7
    if rule == "min_feasible":
        assert prior.ratings[1] == pytest.approx(least, abs=EXACT)
        assert prior.ratings[4] == MF.r_min
        assert out.ratings[1] == pytest.approx(least, abs=1e-4)  # only its prior term acts on it
    else:
        assert prior.ratings[1] > least + 1  # the solver's arbitrary point (CLARABEL lands high)
