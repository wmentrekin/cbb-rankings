"""End-to-end parity of the production model with the goldens of the REAL football code.

Path under test: raw football-shaped fixture -> `tests/football_adapter.py` (this repository's
`games` rows; derived columns from `ingest.derive`) -> `model.assemble` -> prior-season solve ->
`model.solve` with FOOTBALL_COMPAT (OSQP, the solver that produced the goldens; margin basis
"alpha", football's actual behavior), and the same through `rate_week` against a local sqlite
store, so the SQL read path is covered too.

Tolerances (requirements.yaml): objective and each of the four terms within 1e-6 relative
(1e-9 absolute floor); ratings within 1e-3 absolute for the teams the reference README marks
unique (`non_unique_teams` are the zero-loss teams at lambda 0: a flat direction); r_lower
where determined (an active lower-division hinge: all five scenarios and the prior season).
"""
import pytest

from ingest.derive import derive_result
from model.assemble import assemble
from model.config import FOOTBALL_COMPAT
from model.pipeline import rate_week
from model.solve import solve
from tests.football_adapter import build
from tests.reference import football_reference as ref
from tests.reference.fixture_io import load_fixture, load_goldens

GOLD = load_goldens()
IDS, TEAM_ROWS, GAME_ROWS = build()
NAME = {v: k for k, v in IDS.items()}
SCENARIOS = list(GOLD["scenarios"])
YEAR, PRIOR_YEAR = 2024, GOLD["prior_season"]["year"]
REL, ABS_FLOOR, RATING_ABS = 1e-6, 1e-9, 1e-3


def approx(x):
    return pytest.approx(x, rel=REL, abs=ABS_FLOOR)


def d1_ids(season):
    return [t["id"] for t in TEAM_ROWS if t["season"] == season]


def rated_rows(season, week=None):
    """Football's `rankings_games` + `week <= W` filter, applied to the converted rows."""
    return [g for g in GAME_ROWS
            if g["season"] == season and g["is_rated"] and (week is None or g["week"] <= week)]


def prior_solve():
    a = assemble(rated_rows(PRIOR_YEAR), d1_ids(PRIOR_YEAR))
    return solve(a.games, a.lower_losses, a.records, d1_ids(PRIOR_YEAR), None, 1, FOOTBALL_COMPAT)


def assert_solution(result, gold_main, skip_names=(), gold_ratings=None):
    assert result.objective == approx(gold_main["objective"])
    assert result.margin_term == approx(gold_main["terms"]["margin"])
    assert result.loss_rate_term == approx(gold_main["terms"]["loss_rate"])
    assert result.lower_term == approx(gold_main["terms"]["fcs_loss"])
    assert result.prior_term == approx(gold_main["terms"]["prior"])
    assert result.r_lower == pytest.approx(gold_main["r_fcs"], abs=RATING_ABS)
    assert result.solver == gold_main["solver"] == "OSQP"
    assert result.status == "optimal"
    if gold_ratings is not None:
        names = {NAME[t] for t in result.ratings}
        assert names | {"fcs"} == set(gold_ratings)  # the dummy is a separate variable, never a key
        for team, value in gold_ratings.items():
            if team == "fcs" or team in skip_names:
                continue
            assert result.ratings[IDS[team]] == pytest.approx(value, abs=RATING_ABS), team


def test_adapter_derivations_equal_football_goldens():
    """The fixture's derived columns (from ingest.derive) equal the goldens' football values."""
    games_df, _ = load_fixture()
    gold = GOLD["fixture_derived"]
    by_id = {g["id"]: g for g in GAME_ROWS}
    checked = 0
    for r in games_df.itertuples():
        g = by_id[int(r.id)]
        expected = gold[str(int(r.id))]
        if expected["winner"] is None:
            assert g["winner_team_id"] is None and g["margin"] is None and g["alpha"] is None
            continue
        assert NAME[g["winner_team_id"]] == expected["winner"]
        assert g["margin"] == expected["margin"]
        assert g["alpha"] == expected["alpha"]
        checked += 1
    assert checked > 100
    # And the production derivation is the same function the reference's copy agrees with.
    assert derive_result("final", 1, 2, 7, 3, False)[0]["alpha"] == ref.compute_alpha("h", "h", False)


def test_prior_season_matches_football():
    gold = GOLD["prior_season"]
    result = prior_solve()
    assert_solution(result, {"objective": gold["objective"], "terms": gold["terms"],
                             "r_fcs": gold["r_fcs"], "solver": gold["solver"]}, gold_ratings=gold["ratings"])
    assert result.lam == 0.0


@pytest.mark.parametrize("name", SCENARIOS)
def test_assembly_matches_football(name):
    gold = GOLD["scenarios"][name]
    a = assemble(rated_rows(gold["year"], gold["week"]), d1_ids(gold["year"]))
    g = gold["assembled"]
    assert sorted(NAME[t] for t in a.records) == sorted(g["teams"])
    # football tuples: (team1, team2, k, winner, margin, alpha, week, season)
    gold_games = sorted((w, (t1 if t2 == w else t2), m, al) for t1, t2, _k, w, m, al, _wk, _s in g["games"])
    assert sorted((NAME[x.winner], NAME[x.loser], x.margin, x.alpha) for x in a.games) == gold_games
    gold_lower = sorted((t, m, al) for t, m, al, _wk, _s in g["fcs_losses"])
    assert sorted((NAME[x.team], x.margin, x.alpha) for x in a.lower_losses) == gold_lower
    assert {NAME[t]: wl for t, wl in a.records.items()} == {t: (w, l) for t, (w, l, ties) in g["records"].items()}
    assert all(ties == 0 for _, _, ties in g["records"].values())
    # Intentional difference (reference README quirk 4): connectivity here counts played games only.
    pairs = [(x.winner, x.loser) for x in a.games]
    assert a.connectivity == ref.compute_connectivity_index(pairs, d1_ids(gold["year"]))


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenario_matches_football(name):
    gold = GOLD["scenarios"][name]
    ids = d1_ids(gold["year"])
    a = assemble(rated_rows(gold["year"], gold["week"]), ids)
    result = solve(a.games, a.lower_losses, a.records, ids, prior_solve().ratings, gold["week"], FOOTBALL_COMPAT)
    assert result.lam == pytest.approx(gold["lambda"], rel=1e-15)
    assert_solution(result, gold["main"], skip_names=gold["non_unique_teams"], gold_ratings=gold["ratings"])


@pytest.mark.parametrize("name", SCENARIOS)
def test_rate_week_through_the_store_matches_football(store, name):
    store.upsert("teams", TEAM_ROWS, ("id", "season"))
    store.upsert("games", GAME_ROWS, ("id",))
    gold = GOLD["scenarios"][name]
    out = rate_week(store, gold["year"], gold["week"], FOOTBALL_COMPAT)
    assert_solution(out.solve, gold["main"], skip_names=gold["non_unique_teams"], gold_ratings=gold["ratings"])
    assert {NAME[t]: wl for t, wl in out.records.items()} == {t: (w, l) for t, (w, l, _) in gold["records"].items()}
    assert set(out.ratings) == set(d1_ids(gold["year"]))
    assert out.warnings == []
    if gold["lambda"] > 0:
        assert out.prior_solve is not None
        assert out.prior_solve.objective == approx(GOLD["prior_season"]["objective"])
    else:  # lambda 0: the prior-season solve is skipped, it cannot affect the result
        assert out.prior_solve is None
