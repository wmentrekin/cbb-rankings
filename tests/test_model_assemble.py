import random

import pandas as pd
import pytest

from ingest.derive import derive_result
from model.assemble import Game, LowerLoss, assemble

D1 = [1, 2, 3]
LOWER = 900


def row(gid, home, away, hs, as_, neutral=False, status="final"):
    result, _ = derive_result(status, home, away, hs, as_, neutral)
    return {"id": gid, "home_team_id": home, "away_team_id": away, **result}


ROWS = [
    row(1, 1, 2, 80, 70),  # D-I vs D-I, home win: alpha 0.8, margin 10
    row(2, 3, 1, 60, 65),  # away win: alpha 1.2
    row(3, 2, LOWER, 50, 60),  # D-I loss to non-D-I: lower-division loss, margin 10, alpha 1.2
    row(4, LOWER, 3, 40, 70),  # D-I win over non-D-I: record only
    row(5, 1, 3, 0, 0, status="scheduled"),  # unplayed
    row(6, LOWER, LOWER + 1, 70, 60),  # no D-I side
]


def test_assembly_of_every_game_kind():
    a = assemble(ROWS, D1)
    assert a.games == [Game(1, 2, 10, 0.8), Game(1, 3, 5, 1.2)]
    assert a.lower_losses == [LowerLoss(2, 10, 1.2)]
    assert a.records == {1: (2, 0), 2: (0, 2), 3: (1, 1)}  # wins/losses include games vs non-D-I
    assert (a.n_ignored_unplayed, a.n_ignored_no_d1) == (1, 1)


def test_lower_division_win_is_record_only():
    a = assemble([row(1, 1, LOWER, 80, 60)], D1)
    assert a.games == [] and a.lower_losses == []
    assert a.records == {1: (1, 0), 2: (0, 0), 3: (0, 0)}


def test_output_does_not_depend_on_row_order():
    expected = assemble(ROWS, D1)
    for seed in range(5):
        shuffled = ROWS[:]
        random.Random(seed).shuffle(shuffled)
        assert assemble(shuffled, D1) == expected


def test_dataframe_input_with_null_results():
    frame = pd.DataFrame(ROWS)  # None results become NaN, ids may become floats
    assert assemble(frame, D1) == assemble(ROWS, D1)


def test_empty_input_gives_zero_records_and_zero_connectivity_structure():
    a = assemble([], D1)
    assert a.games == [] and a.lower_losses == []
    assert a.records == {1: (0, 0), 2: (0, 0), 3: (0, 0)}
    assert a.connectivity == pytest.approx(1 / 3)  # three isolated teams: largest component is one team
    assert assemble([], []).connectivity == 0.0


def test_connectivity_is_the_largest_played_component_share():
    a = assemble([row(1, 1, 2, 70, 60)], D1)
    assert a.connectivity == pytest.approx(2 / 3)
    full = assemble([row(1, 1, 2, 70, 60), row(2, 2, 3, 70, 60)], D1)
    assert full.connectivity == 1.0


def test_unplayed_games_do_not_connect_teams():
    assert assemble([row(1, 1, 2, 0, 0, status="scheduled")], D1).connectivity == pytest.approx(1 / 3)


def test_stored_alpha_is_used_not_recomputed():
    odd = row(1, 1, 2, 80, 70)
    odd["alpha"] = 0.5
    assert assemble([odd], D1).games == [Game(1, 2, 10, 0.5)]


def test_malformed_input_raises():
    with pytest.raises(ValueError, match="duplicate"):
        assemble([ROWS[0], ROWS[0]], D1)
    with pytest.raises(ValueError, match="neither side"):
        assemble([{**ROWS[0], "winner_team_id": 3}], D1)
    with pytest.raises(ValueError, match="missing columns"):
        assemble([{"id": 1}], D1)
    with pytest.raises(ValueError, match="no margin or alpha"):
        assemble([{**ROWS[0], "margin": None}], D1)
