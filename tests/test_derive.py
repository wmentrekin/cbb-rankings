import copy
from datetime import date

import pytest

from ingest import derive
from ingest.derive import (
    DEFAULT_ALPHA, AlphaConfig, derive_game, derive_result, eastern_date, is_rated,
)
from tests.fakes import load

GAMES = {g["id"]: g for g in load("cbbd_games_2026.json")}
D1 = {t["id"] for t in load("cbbd_teams_2026.json")}
SELECTION_SUNDAY = date(2026, 3, 15)


def derive_one(raw, **kw):
    kw.setdefault("d1_ids", D1)
    kw.setdefault("selection_sunday", SELECTION_SUNDAY)
    kw.setdefault("week_of", lambda d: 1)
    return derive_game(raw, season=2026, **kw)


# --- football parity -----------------------------------------------------------------------
# Copied verbatim in logic from cfb-rankings database/game_derivations.py (last changed in
# 799faf2; repository at 8ab8201). Football keys the winner by team name; here by id.
def football_margin(home_score, away_score):
    return abs(home_score - away_score)


def football_winner(home_team, away_team, home_score, away_score):
    if home_score == 0 and away_score == 0:
        return None
    return home_team if home_score > away_score else away_team


def football_alpha(home_team, winner, neutral_site):
    return 1.0 if neutral_site else (0.8 if home_team == winner else 1.2)


@pytest.mark.parametrize("home,away,neutral", [(80, 70, False), (70, 80, False), (80, 70, True), (70, 80, True), (1, 0, False), (0, 1, False)])
def test_winner_margin_alpha_match_football(home, away, neutral):
    result, code = derive_result("final", 11, 22, home, away, neutral)
    winner_name = football_winner("home", "away", home, away)
    assert code is None
    assert result["winner_team_id"] == (11 if winner_name == "home" else 22)
    assert result["margin"] == football_margin(home, away)
    assert result["alpha"] == football_alpha("home", winner_name, neutral)


def test_alpha_constants():
    assert (DEFAULT_ALPHA.neutral, DEFAULT_ALPHA.home_win, DEFAULT_ALPHA.away_win) == (1.0, 0.8, 1.2)
    result, _ = derive_result("final", 1, 2, 70, 60, False, AlphaConfig(neutral=1.0, home_win=0.5, away_win=2.0))
    assert result["alpha"] == 0.5


# --- played / unplayed ---------------------------------------------------------------------
@pytest.mark.parametrize("status", ["scheduled", "in_progress", "postponed", "cancelled"])
@pytest.mark.parametrize("scores", [(0, 0), (None, None), (0, None), (55, 40)])
def test_non_final_stores_nulls_never_zero(status, scores):
    result, code = derive_result(status, 1, 2, *scores, False)
    assert code is None
    assert result == {"home_score": None, "away_score": None, "margin": None, "winner_team_id": None, "alpha": None}


@pytest.mark.parametrize(
    "scores,code",
    [((None, 70), derive.FINAL_SCORE_MISSING), ((70, None), derive.FINAL_SCORE_MISSING), ((-1, 70), derive.FINAL_SCORE_MISSING),
     ((70, 70), derive.FINAL_TIE), ((0, 0), derive.FINAL_TIE)],
)
def test_bad_final_is_flagged_and_stored_not_played(scores, code):
    result, got = derive_result("final", 1, 2, *scores, False)
    assert got == code
    assert result["home_score"] is None and result["winner_team_id"] is None and result["alpha"] is None


def test_bad_final_row_counts_anomaly_and_keeps_row():
    raw = copy.deepcopy(GAMES[212784])
    raw["awayPoints"] = raw["homePoints"]
    out = derive_one(raw)
    assert out.anomalies == {derive.FINAL_TIE: 1}
    assert out.row["status"] == "final" and out.row["winner_team_id"] is None and out.row["margin"] is None
    assert "game 212784" in out.messages[0]


# --- real fixture rows ---------------------------------------------------------------------
def test_di_vs_di_home_win_neutral_site():
    row = derive_one(GAMES[212784]).row  # Winthrop 81 Queens 74, neutral
    assert (row["home_score"], row["away_score"], row["margin"], row["winner_team_id"], row["alpha"]) == (81, 74, 7, 354, 1.0)
    assert row["neutral_site"] == 1 and row["periods"] == 2 and row["source_id"] == "401823449"


def test_away_win_alpha_12_and_overtime_periods():
    row = derive_one(GAMES[212802]).row  # Kent State 97, Troy 103 in OT, [34,53,10]
    assert row["winner_team_id"] == GAMES[212802]["awayTeamId"] and row["alpha"] == 1.2 and row["periods"] == 3
    assert derive_one(GAMES[213325]).row["periods"] == 5  # triple overtime


def test_home_win_alpha_08():
    row = derive_one(GAMES[209847]).row
    assert row["winner_team_id"] == GAMES[209847]["homeTeamId"] and row["alpha"] == 0.8


def test_di_beats_non_di_and_loses_to_non_di():
    win = derive_one(GAMES[215243])  # East Texas A&M 119 Arlington Baptist 60
    assert win.row["winner_team_id"] == 76 and win.row["alpha"] == 0.8 and not win.anomalies
    loss = derive_one(GAMES[212745])  # Boise State 78 Hawai'i Pacific 79
    assert loss.row["winner_team_id"] == 721 and loss.row["alpha"] == 1.2 and loss.row["margin"] == 1
    neutral_loss = derive_one(GAMES[215445])  # Delaware State 58 Virginia St 62, neutral
    assert neutral_loss.row["winner_team_id"] == 977 and neutral_loss.row["alpha"] == 1.0


def test_non_di_home_team_is_kept():
    out = derive_one(GAMES[213466])  # Chaminade (not D-I) hosts Washington State
    assert out.row["home_team_id"] == 902 and out.row["winner_team_id"] == 344 and not out.dropped_both_non_d1


def test_postponed_cancelled_and_replay():
    postponed = derive_one(GAMES[213650]).row
    assert postponed["status"] == "postponed" and postponed["home_score"] is None and postponed["periods"] is None
    assert derive_one(GAMES[213591]).row["status"] == "cancelled"
    replay = derive_one(GAMES[358658]).row
    assert replay["id"] != postponed["id"] and replay["status"] == "final" and replay["margin"] == 56


def test_scheduled_zero_zero_and_null_points_from_2027():
    for raw in load("cbbd_games_2027.json"):
        if raw["status"] in ("scheduled", "in_progress", "postponed"):
            d1 = {t["id"] for t in load("cbbd_teams_2027.json")}
            out = derive_game(raw, season=2027, d1_ids=d1, selection_sunday=date(2027, 3, 14), week_of=lambda d: 1)
            row = out.row
            assert row is not None and row["home_score"] is None and row["away_score"] is None
            assert row["margin"] is None and row["winner_team_id"] is None and row["alpha"] is None


# --- classification ------------------------------------------------------------------------
def test_both_non_di_dropped_synthetic():
    raw = copy.deepcopy(GAMES[215243])  # SYNTHETIC: no such game exists in the data
    raw["id"], raw["homeTeamId"], raw["awayTeamId"] = 9_000_001, 9001, 9002
    out = derive_one(raw)
    assert out.row is None and out.dropped_both_non_d1 and not out.anomalies


def test_conference_cross_checks_are_logged_not_fatal():
    raw = copy.deepcopy(GAMES[215243])
    raw["awayConference"], raw["awayConferenceId"] = "Odd", 99  # non-D-I side with a conference
    raw["homeConference"] = None  # D-I side without
    out = derive_one(raw)
    assert out.row is not None
    assert out.anomalies == {derive.NON_D1_HAS_CONFERENCE: 1, derive.D1_MISSING_CONFERENCE: 1}


def test_real_mixed_game_has_no_conference_anomaly():
    assert not derive_one(GAMES[215243]).anomalies


def test_classification_uses_the_season_team_set():
    # Same game, but if the home team is absent from the season's teams it is non-D-I.
    out = derive_one(GAMES[212784], d1_ids=D1 - {354})
    assert out.row is not None and out.anomalies[derive.NON_D1_HAS_CONFERENCE] == 1
    out = derive_one(GAMES[212784], d1_ids=D1 - {354, 362})
    assert out.dropped_both_non_d1


@pytest.mark.parametrize(
    "patch,code",
    [({"status": "tbd"}, derive.UNKNOWN_STATUS), ({"seasonType": "offseason"}, derive.UNKNOWN_SEASON_TYPE),
     ({"startDate": "nonsense"}, derive.MALFORMED_ROW), ({"homeTeamId": None}, derive.MALFORMED_ROW), ({"gameType": None}, derive.MALFORMED_ROW)],
)
def test_malformed_rows_are_skipped_with_an_anomaly(patch, code):
    raw = {**copy.deepcopy(GAMES[212784]), **patch}
    out = derive_one(raw)
    assert out.row is None and out.anomalies == {code: 1}


# --- is_rated and dates --------------------------------------------------------------------
def test_is_rated_around_selection_sunday():
    sunday, monday = date(2026, 3, 15), date(2026, 3, 16)
    assert is_rated("regular", sunday, sunday)
    assert not is_rated("regular", monday, sunday)
    assert is_rated("regular", date(2026, 3, 1), sunday)
    assert not is_rated("postseason", date(2026, 3, 17), sunday)
    assert not is_rated("postseason", date(2026, 3, 1), sunday)  # even a postseason row dated early


def test_is_rated_on_fixture_rows():
    rated = {i: derive_one(g).row["is_rated"] for i, g in GAMES.items()}
    assert rated[372449] == 1  # conference tournament final on Selection Sunday 2026-03-15
    assert rated[372151] == 1 and rated[372345] == 1  # conference tournament, in-season event too
    assert rated[212765] == 1 and rated[213650] == 1  # in-season event; postponed rows are eligible
    for gid in (372546, 372511, 372510, 372598, 372613, 372612, 372614):
        assert rated[gid] == 0  # NIT, NCAA, Crown
    # A regular-season row dated after Selection Sunday is not rated (defensive guard).
    late = {**copy.deepcopy(GAMES[372449]), "startDate": "2026-03-16T20:00:00.000Z"}
    assert derive_one(late).row["is_rated"] == 0


def test_eastern_date_conversion():
    assert eastern_date("2025-11-04T00:00:00.000Z") == date(2025, 11, 3)  # 7 pm ET opening night
    assert eastern_date("2025-11-27T04:59:00.000Z") == date(2025, 11, 26)  # 6:59 pm HST game
    assert eastern_date("2025-11-03T13:00:00.000Z") == date(2025, 11, 3)
    # Late Sunday night in Eastern is Monday in UTC; DST changes the offset.
    assert eastern_date("2026-03-16T03:30:00.000Z") == date(2026, 3, 15)
    assert eastern_date("2025-11-03T05:00:00.000Z") == date(2025, 11, 3)  # startTimeTbd placeholder (EST)
    assert eastern_date("2026-03-30T04:00:00.000Z") == date(2026, 3, 30)  # midnight EDT
    assert eastern_date("2026-03-30T03:59:00.000Z") == date(2026, 3, 29)


def test_game_date_on_fixture_rows():
    assert derive_one(GAMES[212802]).row["game_date"] == "2025-11-03"  # UTC date is 11-04
    assert derive_one(GAMES[212802]).row["start_date"] == "2025-11-04T00:00:00.000Z"
    assert derive_one(GAMES[358331]).row["game_date"] == "2025-11-26"


def test_no_time_game_05z_keeps_its_date():
    tbd = [g for g in load("cbbd_games_2027.json") if g["startTimeTbd"]]
    assert tbd and all(g["startDate"].endswith("T05:00:00.000Z") for g in tbd)
    d1 = {t["id"] for t in load("cbbd_teams_2027.json")}
    for g in tbd:
        row = derive_game(g, season=2027, d1_ids=d1, selection_sunday=date(2027, 3, 14), week_of=lambda d: 1).row
        assert row["game_date"] == g["startDate"][:10]


def test_week_of_receives_the_eastern_date():
    seen = []
    derive_one(GAMES[212802], week_of=lambda d: seen.append(d) or 1)
    assert seen == [date(2025, 11, 3)]


# --- timestamps without an offset, finals without a result ---------------------------------
@pytest.mark.parametrize("value", ["2025-11-04T00:00:00", "2025-11-04T00:00:00.000", "2025-11-04"])
def test_eastern_date_rejects_naive_timestamps_and_bare_dates(value):
    with pytest.raises(ValueError, match="UTC offset"):
        derive.eastern_date(value)


def test_eastern_date_accepts_z_and_explicit_offsets():
    assert derive.eastern_date("2025-11-04T00:00:00.000Z") == date(2025, 11, 3)
    assert derive.eastern_date("2025-11-04T00:00:00+00:00") == date(2025, 11, 3)
    assert derive.eastern_date("2025-11-03T19:00:00-05:00") == date(2025, 11, 3)


def test_naive_start_date_makes_a_malformed_row_not_a_silent_local_date():
    raw = copy.deepcopy(next(g for g in load("cbbd_games_2026.json") if g["id"] == 212784))
    raw["startDate"] = "2025-11-04T00:00:00"
    out = derive.derive_game(
        raw, season=2026, d1_ids={t["id"] for t in load("cbbd_teams_2026.json")},
        selection_sunday=date(2026, 3, 15), week_of=lambda d: 1,
    )
    assert out.row is None and out.anomalies == {"malformed_row": 1}


@pytest.mark.parametrize("scores", [(0, 0), (None, 70), (70, 70)])
def test_final_without_a_result_is_unrated_even_in_the_rated_window(scores):
    raw = copy.deepcopy(next(g for g in load("cbbd_games_2026.json") if g["id"] == 212784))
    raw["homePoints"], raw["awayPoints"] = scores
    out = derive.derive_game(
        raw, season=2026, d1_ids={t["id"] for t in load("cbbd_teams_2026.json")},
        selection_sunday=date(2026, 3, 15), week_of=lambda d: 1,
    )
    assert out.row["status"] == "final" and out.row["winner_team_id"] is None and out.row["is_rated"] == 0
    assert sum(out.anomalies.values()) == 1
