"""The fixture keeps covering the cases B3-T1 requires (guards against regenerating it into something blind)."""
import numpy as np

from tests.reference import football_reference as ref
from tests.reference.fixture_io import load_fixture, load_goldens

GAMES, TEAMS = load_fixture()
GOLD = load_goldens()
CURRENT = GAMES[GAMES["season"] == 2024]


def assembled(week):
    return ref.get_data_by_year_up_to_week(GAMES, TEAMS, 2024, week)


def test_size_and_seasons():
    for season in (2023, 2024):
        assert 60 <= (GAMES["season"] == season).sum() <= 100
        assert 12 <= (TEAMS["season"] == season).sum() <= 16


def test_week_below_and_at_the_fade_cutoff():
    assert {s["week"] for s in GOLD["scenarios"].values()} >= {3, 7, 15}
    assert GOLD["scenarios"]["w03"]["lambda"] > 0 and GOLD["scenarios"]["w07"]["lambda"] == 0


def test_team_missing_from_prior_season_and_team_with_no_games_early():
    prior = GOLD["prior_season"]["ratings"]
    teams, _, _, records, _ = assembled(3)
    assert [t for t in teams if t not in prior] == ["Oakhurst"]
    assert [t for t, r in records.items() if sum(r) == 0] == ["Larkspur"]


def test_site_mix_rematch_and_unplayed_sentinel():
    g = ref.add_derived_columns(CURRENT)
    assert set(g["alpha"]) == {0.8, 1.0, 1.2}
    _, games, _, _, _ = assembled(15)
    assert any(game[2] == 1 for game in games)  # second meeting of the same pair
    assert ((g["home_score"] == 0) & (g["away_score"] == 0)).sum() >= 2
    w3 = ref.add_derived_columns(CURRENT[CURRENT["week"] <= 3])
    assert ((w3["home_score"] == 0) & (w3["away_score"] == 0)).sum() == 1  # inside the week-3 window
    assert "postseason" in set(CURRENT["season_type"])  # excluded by the rankings_games view


def test_margin_cap_binds_in_every_scenario_from_week_5_and_tiny_margins():
    # The cap binds above median * (20 / 7)^2 points: the 180-point game (week 5) must exceed it.
    for s in GOLD["scenarios"].values():
        if s["week"] < 5:
            continue
        _, games, _, _, _ = assembled(s["week"])
        k = ref.compute_k_margin(games, 7.0)
        assert k == s["main"]["k_margin"]
        margins = [x[4] for x in games]
        assert any(k * np.sqrt(m) > 20 for m in margins), s["week"]
        assert any(k * np.sqrt(m) < 20 for m in margins)
    assert min(margins) <= 2


def test_lower_division_losses_and_wins():
    _, _, fcs_losses, records, _ = assembled(15)
    assert len(fcs_losses) >= 2
    assert len({m for _, m, _, _, _ in fcs_losses}) >= 2
    assert {a for _, _, a, _, _ in fcs_losses} == {0.8, 1.0, 1.2}
    teams = set(TEAMS[TEAMS["season"] == 2024]["school"])
    g = CURRENT[CURRENT["season_type"] == "regular"]
    wins_over_lower = [
        r for _, r in g.iterrows()
        if (r["home_team"] in teams) != (r["away_team"] in teams)
        and (r["home_team"] in teams) == (r["home_score"] > r["away_score"]) and r["home_score"] != r["away_score"]
    ]
    assert wins_over_lower


def test_undefeated_and_winless_teams():
    _, _, _, records, _ = assembled(10)
    assert records["Ashford"][1] == 0 and records["Ashford"][0] > 0
    assert records["Northgate"][0] == 0 and records["Northgate"][1] > 0
