"""The derivations copy against values produced by football's real game_derivations functions."""
import pandas as pd
import pytest

from tests.reference import football_reference as ref
from tests.reference.fixture_io import load_fixture, load_goldens

GOLD = load_goldens()


@pytest.mark.parametrize("case", GOLD["derivation_cases"],
                         ids=lambda c: f"{c['home_score']}-{c['away_score']}-{'neutral' if c['neutral_site'] else 'home'}")
def test_single_game_derivations(case):
    winner = ref.compute_winner(case["home_team"], case["away_team"], case["home_score"], case["away_score"])
    assert winner == case["winner"]
    assert ref.compute_margin(case["home_score"], case["away_score"]) == case["margin"]
    assert ref.compute_alpha(case["home_team"], winner, case["neutral_site"]) == case["alpha"]


def test_sentinel_and_tie_behaviour_is_football_s():
    # 0-0 is "unplayed" (no winner); any other tie is scored as an away win
    assert ref.compute_winner("H", "A", 0, 0) is None
    assert ref.compute_winner("H", "A", 24, 24) == "A"
    assert ref.compute_alpha("H", "A", False) == 1.2
    assert ref.compute_alpha("H", "H", False) == 0.8
    assert ref.compute_alpha("H", "A", True) == 1.0


def test_fixture_derived_columns_match_football():
    games, _ = load_fixture()
    derived = ref.add_derived_columns(games)
    assert len(derived) == len(GOLD["fixture_derived"])
    for _, row in derived.iterrows():
        gold = GOLD["fixture_derived"][str(int(row["id"]))]
        assert (None if pd.isnull(row["winner"]) else row["winner"]) == gold["winner"]
        assert int(row["margin"]) == gold["margin"]
        assert float(row["alpha"]) == gold["alpha"]
