import copy
from datetime import datetime, timezone

import pytest

from ingest import run, weeks
from ingest.run import SeasonNotIngestedError, ingest_season, ingest_week
from ingest.weeks import SeasonConfigError, get_season_row
from tests.fakes import FakeApi, load, season_2026_api

T1 = lambda: datetime(2026, 4, 7, 10, 0, tzinfo=timezone.utc)  # noqa: E731
T2 = lambda: datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc)  # noqa: E731


@pytest.fixture(autouse=True)
def small_opening_day(monkeypatch):
    """The trimmed fixtures hold a handful of games per day, not the 20+ of a real opening day."""
    original = weeks.compute_anchor_monday
    monkeypatch.setattr(run, "compute_anchor_monday", lambda dates: original(dates, min_games=3))


def games_by_id(store):
    return {r["id"]: r for r in store.query("SELECT * FROM games")}


def test_ingest_season_stores_teams_games_and_season_row(store):
    api = season_2026_api()
    summary = ingest_season(store, 2026, api, now=T1)

    assert summary.api_calls == api.calls == 2  # teams + one games page (fixture is under the cap)
    assert summary.quota_remaining == 898
    assert summary.teams_fetched == 59 and summary.games_fetched == 40
    assert summary.rows_upserted == {"teams": 59, "seasons": 2, "games": 40}
    assert summary.rows_written == {"teams": 59, "seasons": 2, "games": 40}  # seasons: insert, then the timestamp update
    assert summary.total_rows_written == 101
    assert summary.dropped_both_non_d1 == 0 and summary.anomalies == {} and summary.warnings == []

    season = get_season_row(store, 2026)
    assert (season.anchor_monday.isoformat(), season.selection_sunday.isoformat()) == ("2025-11-03", "2026-03-15")
    assert season.last_ingested_at == "2026-04-07T10:00:00Z"
    assert store.query("SELECT COUNT(*) AS n FROM teams WHERE season = 2026")[0]["n"] == 59


def test_game_rows_weeks_rating_flags_and_classification(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    games = games_by_id(store)
    assert games[212802]["week"] == 1 and games[212802]["game_date"] == "2025-11-03"  # UTC date is 11-04
    assert games[209847]["week"] == 1  # Sunday 11-09
    assert games[212992]["week"] == 2  # Monday 11-10
    assert games[210475]["week"] == 9  # 2026-01-01
    assert games[372449]["week"] == 19 and games[372449]["is_rated"] == 1  # Selection Sunday final
    assert games[372546]["week"] == 20 and games[372546]["is_rated"] == 0  # NIT
    assert games[372511]["is_rated"] == 0 and games[372598]["is_rated"] == 0  # NCAA, Crown
    assert games[372345]["is_rated"] == 1  # conference tournament
    # played / unplayed
    assert games[213650]["status"] == "postponed" and games[213650]["home_score"] is None and games[213650]["alpha"] is None
    assert games[358658]["winner_team_id"] == 77 and games[358658]["margin"] == 56 and games[358658]["alpha"] == 0.8
    assert games[213591]["status"] == "cancelled" and games[213591]["margin"] is None
    # non-D-I opponents are stored with their ids and are not in teams
    assert games[212745]["away_team_id"] == 721 and games[212745]["winner_team_id"] == 721
    assert store.query("SELECT 1 FROM teams WHERE id = 721") == []
    # exact column types survive the JSON path (STRICT table would reject floats for ints)
    assert isinstance(games[212784]["margin"], int) and isinstance(games[212784]["alpha"], float)


def test_rerun_writes_nothing(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    again = ingest_season(store, 2026, season_2026_api(), now=T1)
    assert again.rows_written == {"teams": 0, "seasons": 0, "games": 0}
    assert again.rows_upserted["games"] == 40
    # With a new timestamp only the seasons row (last_ingested_at) changes.
    later = ingest_season(store, 2026, season_2026_api(), now=T2)
    assert later.rows_written == {"teams": 0, "seasons": 1, "games": 0}
    assert store.query("SELECT COUNT(*) AS n FROM games")[0]["n"] == 40


def test_changed_result_is_updated(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    teams, games = load("cbbd_teams_2026.json"), load("cbbd_games_2026.json")
    for g in games:
        if g["id"] == 212784:
            g["homePoints"], g["awayPoints"] = 70, 80  # a late correction flips the winner
    run_ = ingest_season(store, 2026, FakeApi({2026: teams}, {2026: games}), now=T1)
    assert run_.rows_written["games"] == 1
    row = games_by_id(store)[212784]
    assert row["winner_team_id"] == 362 and row["alpha"] == 1.0 and row["margin"] == 10


def test_anchor_is_never_recomputed_once_stored(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    store.execute("UPDATE seasons SET anchor_monday = '2025-10-27' WHERE season = 2026")
    summary = ingest_season(store, 2026, season_2026_api(), now=T1)
    assert get_season_row(store, 2026).anchor_monday.isoformat() == "2025-10-27"
    assert summary.anchor_monday == "2025-10-27"
    assert games_by_id(store)[212784]["week"] == 2  # renumbered from the stored anchor, not recomputed
    ingest_week(store, 2026, 3, season_2026_api(), now=T1)
    assert get_season_row(store, 2026).anchor_monday.isoformat() == "2025-10-27"


def test_both_non_di_games_are_dropped_and_counted(store):
    games = load("cbbd_games_2026.json")
    extra = copy.deepcopy(next(g for g in games if g["id"] == 215243))
    extra.update(id=9_000_001, homeTeamId=9001, awayTeamId=9002)  # SYNTHETIC: none exist in the data
    api = FakeApi({2026: load("cbbd_teams_2026.json")}, {2026: games + [extra]})
    summary = ingest_season(store, 2026, api, now=T1)
    assert summary.dropped_both_non_d1 == 1 and summary.games_fetched == 41
    assert 9_000_001 not in games_by_id(store) and len(games_by_id(store)) == 40


def test_one_bad_row_does_not_stop_the_ingest(store):
    games = load("cbbd_games_2026.json")
    bad_tie = next(copy.deepcopy(g) for g in games if g["id"] == 212784)
    bad_tie["awayPoints"] = bad_tie["homePoints"]
    bad_missing = next(copy.deepcopy(g) for g in games if g["id"] == 209784)
    bad_missing["homePoints"] = None
    broken = {"id": 9_000_002, "startDate": "2025-11-05T00:00:00.000Z", "status": "final"}  # SYNTHETIC malformed row
    others = [g for g in games if g["id"] not in (212784, 209784)]
    api = FakeApi({2026: load("cbbd_teams_2026.json")}, {2026: others + [bad_tie, bad_missing]})
    api.games[2026].append(broken)
    summary = ingest_season(store, 2026, api, now=T1)
    assert summary.anomalies == {"final_tie": 1, "final_score_missing": 1, "malformed_row": 1}
    stored = games_by_id(store)
    assert stored[212784]["winner_team_id"] is None and stored[212784]["home_score"] is None
    assert stored[209784]["margin"] is None and 9_000_002 not in stored
    assert len(stored) == 40


def test_missing_selection_sunday_fails_before_any_api_call(store, monkeypatch):
    api = season_2026_api()
    monkeypatch.setattr(run, "load_selection_sunday", lambda season: weeks.load_selection_sunday(2031))
    with pytest.raises(SeasonConfigError, match="add"):
        ingest_season(store, 2026, api)
    assert api.calls == 0


def test_provisional_season_warns_and_sunday_opener_folds_into_week_1(store, caplog):
    api = FakeApi({2027: load("cbbd_teams_2027.json")}, {2027: load("cbbd_games_2027.json")})
    summary = ingest_season(store, 2027, api, now=T1)
    assert summary.anchor_monday == "2026-11-02" and summary.selection_sunday == "2027-03-14"
    assert summary.warnings and "provisional" in summary.warnings[0] and "PROVISIONAL" in caplog.text
    games = games_by_id(store)
    assert games[377909]["game_date"] == "2026-11-01" and games[377909]["week"] == 1
    assert games[377910]["game_date"] == "2026-11-01" and games[377910]["week"] == 1  # 04:59Z is 23:59 ET on the Sunday
    assert games[374556]["game_date"] == "2026-11-02" and games[374556]["week"] == 1
    scheduled = games[375019]
    assert scheduled["status"] == "scheduled" and scheduled["home_score"] is None and scheduled["alpha"] is None
    tbd = games[372907]
    assert tbd["game_date"] == "2026-11-23" and tbd["start_date"] == "2026-11-23T05:00:00.000Z"


def test_ingest_week_needs_the_season_row(store):
    api = season_2026_api()
    with pytest.raises(SeasonNotIngestedError, match="ingest.run season 2026"):
        ingest_week(store, 2026, 3, api)
    assert api.calls == 0


def test_ingest_week_is_one_call_when_teams_are_stored(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    api = season_2026_api()
    summary = ingest_week(store, 2026, 10, api, now=T2)
    assert api.calls == summary.api_calls == 1 and summary.teams_fetched == 0 and summary.week == 10
    (path, params), = api.requests
    assert path == "/games" and params["startDateRange"] == "2025-12-26T00:00:00Z"
    assert summary.rows_written["games"] == 0  # same data as the full ingest: nothing changes


def test_ingest_week_fetches_teams_only_when_absent(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    store.execute("DELETE FROM teams")
    api = season_2026_api()
    summary = ingest_week(store, 2026, 1, api, now=T2)
    assert api.calls == 2 and summary.teams_fetched == 59 and summary.rows_written["teams"] == 59


def test_ingest_week_window_picks_up_late_corrections_and_new_games(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    games = load("cbbd_games_2026.json")
    for g in games:
        if g["id"] == 212992:  # Monday 2025-11-10 game, week 2
            g["homePoints"] = 101
    new = copy.deepcopy(next(g for g in games if g["id"] == 209847))
    new.update(id=9_000_003, startDate="2025-11-12T00:00:00.000Z")  # SYNTHETIC extra game in week 2
    api = FakeApi({2026: load("cbbd_teams_2026.json")}, {2026: games + [new]})
    summary = ingest_week(store, 2026, 2, api, now=T2)
    assert summary.rows_written["games"] == 2
    stored = games_by_id(store)
    assert stored[212992]["home_score"] == 101 and stored[9_000_003]["week"] == 2


def test_summary_as_dict_is_json_serialisable():
    import json
    s = run.IngestSummary(season=2026, rows_written={"games": 3, "teams": 1})
    assert json.loads(json.dumps(s.as_dict()))["total_rows_written"] == 4


def test_cli_main_runs_against_the_local_store(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CBB_STORE", "local")
    monkeypatch.setenv("CBB_LOCAL_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("CBBD_API_KEY", "x")
    monkeypatch.setattr(run, "CbbdClient", lambda: season_2026_api())
    assert run.main(["season", "2026"]) == 0
    assert '"total_rows_written": 101' in capsys.readouterr().out
