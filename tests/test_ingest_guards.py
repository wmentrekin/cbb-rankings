"""Success gates, the absent-game and duplicate guards, and the rules the weekly job relies on."""
import copy
import json
import logging
from datetime import datetime, timezone

import pytest

from database.local import LocalStore
from database.store import WriteStats
from ingest import overrides as ov
from ingest import run, weeks
from ingest.rules import RATED_PLAYED_SQL
from ingest.run import SeasonNotIngestedError, ingest_season, ingest_week
from ingest.weeks import get_season_row
from tests.fakes import FakeApi, load, season_2026_api, season_2027_api, with_scores

T1 = lambda: datetime(2026, 4, 7, 10, 0, tzinfo=timezone.utc)  # noqa: E731
T2 = lambda: datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc)  # noqa: E731


@pytest.fixture(autouse=True)
def small_opening_day(monkeypatch):
    original = weeks.compute_anchor_monday
    monkeypatch.setattr(run, "compute_anchor_monday", lambda dates: original(dates, min_games=3))
    monkeypatch.delenv("INGEST_MAX_DROPPED", raising=False)


def api_26(games=None, teams=None):
    return FakeApi({2026: teams if teams is not None else load("cbbd_teams_2026.json")},
                   {2026: games if games is not None else load("cbbd_games_2026.json")})


def stored(store):
    return {r["id"]: r for r in store.query("SELECT * FROM games")}


def counts(store):
    return {t: store.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in ("teams", "games", "seasons")}


def rated_played(store):
    return {r["id"] for r in store.query(f"SELECT id FROM games WHERE {RATED_PLAYED_SQL}")}


# ---- success gate: empty and failed fetches ----

def test_a_clean_run_is_ok_and_stamped(store):
    summary = ingest_season(store, 2026, season_2026_api(), now=T1)
    assert summary.ok and summary.failures == [] and summary.as_dict()["ok"] is True
    assert get_season_row(store, 2026).last_ingested_at == "2026-04-07T10:00:00Z"


def test_empty_season_fetch_on_a_new_season_fails_and_writes_nothing(store):
    summary = ingest_season(store, 2026, api_26(games=[]), now=T1)
    assert not summary.ok and "no games" in summary.failures[0] and summary.games_fetched == 0
    assert counts(store) == {"teams": 0, "games": 0, "seasons": 0}
    assert summary.api_calls == 2


def test_empty_season_fetch_on_a_populated_season_changes_nothing(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    before = stored(store)
    summary = ingest_season(store, 2026, api_26(games=[]), now=T2)
    assert not summary.ok and summary.anomalies == {} and summary.rows_written == {}
    assert stored(store) == before  # nothing neutralized by an empty fetch
    assert get_season_row(store, 2026).last_ingested_at == "2026-04-07T10:00:00Z"  # not re-stamped


def test_empty_teams_fails_before_fetching_games_or_writing(store):
    api = api_26(teams=[])
    summary = ingest_season(store, 2026, api, now=T1)
    assert not summary.ok and "no teams" in summary.failures[0]
    assert api.calls == 1 and counts(store) == {"teams": 0, "games": 0, "seasons": 0}


def test_a_failed_fetch_raises_before_any_write(store):
    class Dies(FakeApi):
        def get(self, path, params=None):
            if path == "/games":
                raise ConnectionError("boom")
            return super().get(path, params)

    api = Dies({2026: load("cbbd_teams_2026.json")}, {2026: []})
    with pytest.raises(ConnectionError):
        ingest_season(store, 2026, api, now=T1)
    assert counts(store) == {"teams": 0, "games": 0, "seasons": 0}


def test_a_write_failure_part_way_leaves_the_season_unstamped(store):
    class FailingGames(LocalStore):
        def upsert(self, table, rows, key, **kw):
            if table == "games":
                raise RuntimeError("D1 went away")
            return super().upsert(table, rows, key, **kw)

    failing = FailingGames(":memory:")
    with pytest.raises(RuntimeError, match="D1 went away"):
        ingest_season(failing, 2026, season_2026_api(), now=T1)
    row = get_season_row(failing, 2026)
    assert row is not None and row.last_ingested_at is None  # the anchor is there, the stamp is not
    with pytest.raises(SeasonNotIngestedError, match="never fully ingested"):
        ingest_week(failing, 2026, 3, season_2026_api())


def test_ingest_week_needs_a_fully_ingested_season(store):
    api = season_2026_api()
    with pytest.raises(SeasonNotIngestedError, match="no seasons row"):
        ingest_week(store, 2026, 3, api)
    store.upsert("seasons", [{"season": 2026, "anchor_monday": "2025-11-03", "selection_sunday": "2026-03-15",
                              "last_ingested_at": None}], ("season",))
    with pytest.raises(SeasonNotIngestedError, match="never fully ingested"):
        ingest_week(store, 2026, 3, api)
    assert api.calls == 0


def test_empty_weekly_fetch_fails_inside_the_season_and_only_warns_after_it(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    summary = ingest_week(store, 2026, 10, api_26(games=[]), now=T2)
    assert not summary.ok and "no games for season 2026 week 10" in summary.failures[0]
    assert get_season_row(store, 2026).last_ingested_at == "2026-04-07T10:00:00Z"
    late = ingest_week(store, 2026, 25, api_26(games=[]), now=T2)  # Selection Sunday is in week 19
    assert late.ok and "after the last rated week" in late.warnings[0]


# ---- success gate: dropped rows ----

def bad_rows(n):
    return [{"id": 9_100_000 + i, "startDate": "2025-11-05T00:00:00.000Z", "status": "final"} for i in range(n)]  # SYNTHETIC


def test_dropped_rows_above_the_default_threshold_fail_the_run(store):
    games = load("cbbd_games_2026.json")
    ok = ingest_season(store, 2026, api_26(games + bad_rows(5)), now=T1)  # 5 = the allowance for 45 rows
    assert ok.ok and ok.anomalies == {"malformed_row": 5}
    bad = ingest_season(store, 2026, api_26(games + bad_rows(6)), now=T2)
    assert not bad.ok and "6 of 46 fetched games were dropped or malformed" in bad.failures[0] and "allowed 5" in bad.failures[0]
    assert get_season_row(store, 2026).last_ingested_at == "2026-04-07T10:00:00Z"
    assert len(stored(store)) == 40  # the good rows were still written


def test_threshold_scales_with_rows_and_is_configurable(store, monkeypatch):
    assert run.max_dropped(100) == 5 and run.max_dropped(2000) == 10 and run.max_dropped(6318) == 32
    games = load("cbbd_games_2026.json") + bad_rows(8)
    assert ingest_season(store, 2026, api_26(games), now=T1, max_dropped_rows=8).ok
    assert not ingest_season(store, 2026, api_26(games), now=T1, max_dropped_rows=7).ok
    monkeypatch.setenv("INGEST_MAX_DROPPED", "20")
    assert ingest_season(store, 2026, api_26(games), now=T1).ok
    monkeypatch.setenv("INGEST_MAX_DROPPED", "many")
    with pytest.raises(ValueError, match="INGEST_MAX_DROPPED"):
        run.max_dropped(10)


def test_both_non_di_and_unknown_kinds_count_as_dropped(store):
    games = load("cbbd_games_2026.json")
    extra = []
    for i in range(3):
        g = copy.deepcopy(next(x for x in games if x["id"] == 215243))
        g.update(id=9_200_000 + i, homeTeamId=9001, awayTeamId=9002)  # SYNTHETIC both non-D-I
        extra.append(g)
    for i, (key, value) in enumerate([("status", "weird"), ("seasonType", "summer"), ("status", "odd")]):
        g = copy.deepcopy(games[0])
        g.update(id=9_300_000 + i, **{key: value})
        extra.append(g)
    summary = ingest_season(store, 2026, api_26(games + extra), now=T1)
    assert summary.dropped_both_non_d1 == 3 and summary.anomalies == {"unknown_status": 2, "unknown_season_type": 1}
    assert not summary.ok and "6 of 46" in summary.failures[0]


# ---- finals without a result: the two real 0-0 finals ----

def test_real_zero_zero_finals_are_stored_unplayed_and_unrated_and_reported(store):
    summary = ingest_season(store, 2027, season_2027_api(), now=T1)
    assert summary.ok and summary.anomalies == {"final_tie": 2}
    assert sorted(summary.anomaly_ids["final_tie"]) == [374609, 375008]
    games = stored(store)
    for game_id in (374609, 375008):
        g = games[game_id]
        assert g["status"] == "final" and g["season_type"] == "regular" and g["game_date"] <= "2027-03-14"
        assert (g["home_score"], g["away_score"], g["margin"], g["winner_team_id"], g["alpha"]) == (None,) * 5
        assert g["is_rated"] == 0  # would be 1 on date and season type alone
        assert game_id not in rated_played(store)
    assert games[374609]["home_team"] == "North Carolina A&T" and games[375008]["away_team"] == "Paine"
    assert "final_tie" in json.dumps(summary.as_dict()["anomalies"])


def test_final_without_a_result_stays_unrated_across_reruns(store):
    ingest_season(store, 2027, season_2027_api(), now=T1)
    again = ingest_season(store, 2027, season_2027_api(), now=T1)
    assert again.rows_written["games"] == 0 and stored(store)[374609]["is_rated"] == 0


# ---- teams refreshed every run ----

def test_changed_team_count_warns_and_keeps_stored_teams(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    teams = load("cbbd_teams_2026.json")
    dropped = teams.pop()
    summary = ingest_season(store, 2026, api_26(teams=teams), now=T2)
    assert summary.ok and summary.teams_fetched == 58
    assert any("58 D-I teams in the source but 59 stored" in w for w in summary.warnings)
    assert store.query("SELECT COUNT(*) AS n FROM teams WHERE id = ?", [dropped["id"]])[0]["n"] == 1  # not deleted


def test_every_run_refreshes_teams_and_an_unchanged_count_is_quiet(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    teams = load("cbbd_teams_2026.json")
    teams[0]["conference"] = "Renamed Conference"
    api = api_26(teams=teams)
    summary = ingest_season(store, 2026, api, now=T2)
    assert summary.rows_written["teams"] == 1 and summary.warnings == [] and api.calls == 2
    assert store.query("SELECT conference FROM teams WHERE id = ?", [teams[0]["id"]])[0]["conference"] == "Renamed Conference"


# ---- deleted or re-identified games ----

def test_game_absent_from_a_complete_fetch_is_neutralized_and_kept(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    before = stored(store)
    assert 212784 in rated_played(store)
    games = [g for g in load("cbbd_games_2026.json") if g["id"] != 212784]
    summary = ingest_season(store, 2026, api_26(games), now=T2)
    assert summary.ok and summary.anomalies == {"absent_from_source": 1} and summary.anomaly_ids == {"absent_from_source": [212784]}
    gone = stored(store)[212784]
    assert (gone["is_rated"], gone["status"]) == (0, "cancelled")
    assert (gone["home_score"], gone["away_score"], gone["margin"], gone["winner_team_id"], gone["alpha"]) == (None,) * 5
    assert gone["home_team_id"] == before[212784]["home_team_id"] and gone["start_date"] == before[212784]["start_date"]  # row kept
    assert 212784 not in rated_played(store) and len(stored(store)) == 40
    assert {i: r for i, r in stored(store).items() if i != 212784} == {i: r for i, r in before.items() if i != 212784}


def test_neutralized_game_is_counted_once_and_comes_back_if_the_source_relists_it(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    original = stored(store)[212784]
    games = [g for g in load("cbbd_games_2026.json") if g["id"] != 212784]
    ingest_season(store, 2026, api_26(games), now=T2)
    again = ingest_season(store, 2026, api_26(games), now=T2)
    assert again.anomalies == {} and again.rows_written == {"teams": 0, "seasons": 0, "games": 0}
    back = ingest_season(store, 2026, season_2026_api(), now=T2)
    assert stored(store)[212784] == original and back.ok


def test_a_re_identified_game_neutralizes_the_old_id_and_adds_the_new_one(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    games = load("cbbd_games_2026.json")
    for g in games:
        if g["id"] == 212784:
            g["id"] = 9_000_999  # SYNTHETIC: upstream re-issued the game under a new id
    summary = ingest_season(store, 2026, api_26(games), now=T2)
    assert summary.anomalies == {"absent_from_source": 1}
    now_stored = stored(store)
    assert now_stored[212784]["is_rated"] == 0 and now_stored[9_000_999]["is_rated"] == 1
    pair = [i for i in rated_played(store) if now_stored[i]["game_date"] == now_stored[9_000_999]["game_date"]
            and {now_stored[i]["home_team_id"], now_stored[i]["away_team_id"]}
            == {now_stored[9_000_999]["home_team_id"], now_stored[9_000_999]["away_team_id"]}]
    assert pair == [9_000_999]  # the game counts once, under its new id


def synthetic_stored(n: int) -> dict[int, dict]:
    """SYNTHETIC stored played games of one season, enough of them to test the percentage gate."""
    return {i: {"id": i, "start_date": "2025-12-01T00:00:00.000Z", "status": "final", "is_rated": 1,
                "winner_team_id": 1, "home_score": 70, "away_score": 60, "margin": 10, "alpha": 1.0}
            for i in range(1, n + 1)}


def absent_count(stored_n: int, missing: int, limit=None):
    stored_games = synthetic_stored(stored_n)
    raw = [{"id": i} for i in stored_games if i > missing]  # the first `missing` ids are not fetched
    summary = run.IngestSummary(season=2026)
    result = run._absent_games(summary, stored_games, raw, 2026, limit)
    return summary, result


@pytest.mark.parametrize("stored_n, missing, allowed", [
    (6300, 0, 31), (6300, 1, 31), (6300, 31, 31),      # 0.5% of 6,300 is 31.5: 31 is within it, 32 is not
    (400, 5, 5), (400, 6, 5),                           # small seasons: the floor of 5
])
def test_absent_threshold_boundary(stored_n, missing, allowed):
    summary, result = absent_count(stored_n, missing)
    if missing <= allowed:
        assert result is not None and len(result) == missing and summary.failures == []
        assert summary.anomalies.get("absent_from_source", 0) == missing
    else:
        assert result is None and len(summary.failures) == 1


@pytest.mark.parametrize("percent, ok", [(0, True), (1, False), (5, False), (9, False), (11, False)])
def test_percent_missing_from_a_full_size_season(percent, ok):
    stored_n = 6300
    missing = stored_n * percent // 100
    summary, result = absent_count(stored_n, missing)
    assert (result is not None) is ok and (summary.failures == []) is ok
    if not ok:
        message = summary.failures[0]
        assert f"{missing} of the {stored_n} games" in message and "nothing was neutralized" in message
        assert "first ids [1, 2, 3" in message and "--allow-absent" in message
        assert summary.anomalies == {}  # nothing neutralized, nothing counted as an anomaly


def test_one_missing_game_in_a_real_size_season_is_still_neutralized():
    summary, result = absent_count(6300, 1)
    assert summary.failures == [] and [r["id"] for r in result] == [1] and result[0]["is_rated"] == 0


def test_the_allowance_can_be_set_by_argument_and_env(monkeypatch):
    summary, result = absent_count(6300, 630, limit=630)  # 10% accepted explicitly
    assert summary.failures == [] and len(result) == 630
    summary, result = absent_count(6300, 631, limit=630)
    assert result is None
    monkeypatch.setenv("INGEST_MAX_ABSENT", "12")
    assert absent_count(6300, 12)[1] is not None and absent_count(6300, 13)[1] is None
    assert absent_count(6300, 13, limit=13)[1] is not None  # the argument wins
    monkeypatch.setenv("INGEST_MAX_ABSENT", "many")
    with pytest.raises(ValueError, match="INGEST_MAX_ABSENT"):
        run.max_absent(100)


def test_already_neutralized_games_are_not_counted_as_absent_again():
    stored_games = synthetic_stored(100)
    for g in stored_games.values():
        g.update(is_rated=0, status="cancelled", winner_team_id=None)
    summary = run.IngestSummary(season=2026)
    assert run._absent_games(summary, stored_games, [], 2026) == [] and summary.failures == []


def test_a_truncated_fetch_fails_and_changes_no_game_row(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    before = stored(store)
    seasons_before = store.query("SELECT * FROM seasons")
    games = load("cbbd_games_2026.json")[:30]  # 10 of the 40 stored are missing: above the allowance of 5
    games[0] = {**games[0], "homePoints": 99, "awayPoints": 1}  # a fetched game that changed: NOT written either
    summary = ingest_season(store, 2026, api_26(games), now=T2)
    assert not summary.ok and "above the allowed 5" in summary.failures[0] and "nothing was neutralized" in summary.failures[0]
    assert "first ids [" in summary.failures[0] and "10 of the 40" in summary.failures[0]
    assert summary.anomalies == {} and stored(store) == before and "games" not in summary.rows_written
    assert store.query("SELECT * FROM seasons") == seasons_before  # last_ingested_at not moved
    assert get_season_row(store, 2026).last_ingested_at == "2026-04-07T10:00:00Z"


def test_exactly_the_allowance_is_still_neutralized_and_ok(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    games = load("cbbd_games_2026.json")[:35]  # 5 of 40 missing: the floor of 5
    summary = ingest_season(store, 2026, api_26(games), now=T2)
    assert summary.ok and summary.anomalies == {"absent_from_source": 5}
    neutral = [r for r in stored(store).values() if r["status"] == "cancelled" and not r["is_rated"]]
    assert len(neutral) >= 5 and len(stored(store)) == 40  # rows kept


def test_allow_absent_accepts_a_large_legitimate_removal(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    games = load("cbbd_games_2026.json")[:30]
    refused = ingest_season(store, 2026, api_26(games), now=T2)
    assert not refused.ok
    accepted = ingest_season(store, 2026, api_26(games), now=T2, max_absent_games=10)
    assert accepted.ok and accepted.anomalies == {"absent_from_source": 10}
    assert get_season_row(store, 2026).last_ingested_at == "2026-04-14T10:00:00Z"
    assert len(stored(store)) == 40 and sum(1 for r in stored(store).values() if r["status"] == "cancelled") >= 10


def test_the_full_real_replays_neutralize_nothing(store):
    for season, api in ((2026, season_2026_api()), (2027, season_2027_api())):
        summary = ingest_season(store, season, api, now=T1)
        again = ingest_season(store, season, api, now=T2)
        assert summary.ok and again.ok and "absent_from_source" not in again.anomalies


def test_cli_allow_absent_flag_reaches_the_ingest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CBB_STORE", "local")
    monkeypatch.setenv("CBB_LOCAL_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(run, "CbbdClient", lambda: season_2026_api())
    assert run.main(["season", "2026"]) == 0
    monkeypatch.setattr(run, "CbbdClient", lambda: api_26(load("cbbd_games_2026.json")[:30]))
    assert run.main(["season", "2026"]) == 1
    assert "INGEST_FAILURE: 10 of the 40 games" in capsys.readouterr().out
    assert run.main(["season", "2026", "--allow-absent", "10"]) == 0
    with pytest.raises(SystemExit):
        run.main(["season", "2026", "--allow-absent", "-1"])


def test_stored_games_outside_the_fetch_window_are_not_absent(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    outside = {**stored(store)[212784], "id": 9_500_000, "start_date": "2025-09-20T00:00:00.000Z",
               "game_date": "2025-09-20"}  # before October 1
    store.upsert("games", [outside], ("id",))
    summary = ingest_season(store, 2026, season_2026_api(), now=T2)
    assert summary.anomalies == {} and stored(store)[9_500_000]["is_rated"] == 1


def test_week_ingest_never_neutralizes(store):
    ingest_season(store, 2026, season_2026_api(), now=T1)
    games = [g for g in load("cbbd_games_2026.json") if g["id"] != 212784]
    summary = ingest_week(store, 2026, 2, api_26(games), now=T2)
    assert summary.anomalies == {} and stored(store)[212784]["is_rated"] == 1


# ---- duplicate pair (real ids 372907 and 378701, scores filled in SYNTHETICALLY) ----

def duplicate_api(first_score=(70, 65), second_score=(61, 66)):
    api = season_2027_api()
    with_scores(api.games[2027], 372907, *first_score)  # SYNTHETIC result: the source still lists these two as scheduled
    with_scores(api.games[2027], 378701, *second_score)  # SYNTHETIC result
    return api


def test_real_pair_still_scheduled_is_not_a_double_count(store):
    summary = ingest_season(store, 2027, season_2027_api(), now=T1)
    assert summary.anomalies == {"final_tie": 2}
    assert stored(store)[372907]["is_rated"] == 1 and stored(store)[378701]["is_rated"] == 1  # neither is played


def test_same_pair_same_date_rated_twice_keeps_the_larger_id(store):
    summary = ingest_season(store, 2027, duplicate_api(), now=T1)
    assert summary.ok and summary.anomalies["duplicate_rated_pair"] == 1
    assert summary.anomaly_ids["duplicate_rated_pair"] == [372907]
    first, second = stored(store)[372907], stored(store)[378701]
    assert first["game_date"] == second["game_date"] == "2026-11-23"
    assert {first["home_team_id"], first["away_team_id"]} == {second["home_team_id"], second["away_team_id"]} == {151, 262}
    assert (first["is_rated"], second["is_rated"]) == (0, 1)
    assert first["winner_team_id"] == 151 and first["status"] == "final"  # stays final, just unrated
    assert {372907, 378701} & rated_played(store) == {378701}


def test_duplicate_guard_is_stable_across_reruns_and_follows_the_data(store):
    ingest_season(store, 2027, duplicate_api(), now=T1)
    again = ingest_season(store, 2027, duplicate_api(), now=T1)
    assert again.rows_written["games"] == 0 and again.anomalies["duplicate_rated_pair"] == 1
    # the source deletes the later row: the earlier one is no longer a duplicate
    api = duplicate_api()
    api.games[2027] = [g for g in api.games[2027] if g["id"] != 378701]
    healed = ingest_season(store, 2027, api, now=T1)
    assert healed.anomalies.get("duplicate_rated_pair") is None and healed.anomalies["absent_from_source"] == 1
    assert stored(store)[372907]["is_rated"] == 1 and stored(store)[378701]["is_rated"] == 0


def test_different_dates_or_teams_are_not_duplicates(store):
    api = duplicate_api()
    for g in api.games[2027]:
        if g["id"] == 378701:
            g["startDate"] = "2026-11-25T02:30:00.000Z"  # SYNTHETIC: a real rematch two days later
    summary = ingest_season(store, 2027, api, now=T1)
    assert "duplicate_rated_pair" not in summary.anomalies and {372907, 378701} <= rated_played(store)


def test_week_ingest_runs_the_duplicate_guard_against_stored_games(store):
    first = season_2027_api()
    with_scores(first.games[2027], 372907, 70, 65)  # SYNTHETIC; only the first of the pair is played so far
    ingest_season(store, 2027, first, now=T1)
    assert stored(store)[372907]["is_rated"] == 1
    summary = ingest_week(store, 2027, 4, duplicate_api(), now=T2)  # week 4 starts Monday 2026-11-23
    assert summary.anomalies["duplicate_rated_pair"] == 1
    assert (stored(store)[372907]["is_rated"], stored(store)[378701]["is_rated"]) == (0, 1)


def test_exclude_override_plus_duplicate_guard_unrate_independently(store, tmp_path, monkeypatch):
    ingest_season(store, 2027, duplicate_api(), now=T1)
    path = tmp_path / "o.json"
    path.write_text(json.dumps({"version": 1, "overrides": [{
        "kind": "exclude", "game_id": 378701, "season": 2027, "home_team_id": 262, "away_team_id": 151, "reason": "test"}]}))
    assert ov.apply_overrides(store, 2027, path=path).ok
    assert {372907, 378701} & rated_played(store) == set()  # the operator excluded the kept row: neither counts


# ---- Selection Sunday and re-rating ----

def test_changing_selection_sunday_re_rates_the_whole_season_on_a_full_ingest(store, monkeypatch):
    from datetime import date
    ingest_season(store, 2026, season_2026_api(), now=T1)
    before = {i: r["is_rated"] for i, r in stored(store).items()}
    new = date(2026, 2, 15)  # a Sunday a month earlier
    monkeypatch.setattr(run, "load_selection_sunday", lambda season: new)
    summary = ingest_season(store, 2026, season_2026_api(), now=T2)
    assert summary.ok and summary.selection_sunday == "2026-02-15"
    assert any("changed from 2026-03-15 to 2026-02-15" in w and "re-rated" in w for w in summary.warnings)
    after = stored(store)
    # every game, played or not, follows the date rule, so the whole season was re-rated
    assert {i: r["is_rated"] for i, r in after.items()} == {
        i: int(r["season_type"] == "regular" and r["game_date"] <= "2026-02-15") for i, r in after.items()}
    changed = [i for i in after if after[i]["is_rated"] != before[i]]
    assert changed and summary.rows_written["games"] == len(changed)
    assert get_season_row(store, 2026).selection_sunday == date(2026, 2, 15)
    # and back again
    monkeypatch.undo()
    back = ingest_season(store, 2026, season_2026_api(), now=T2)
    assert {i: r["is_rated"] for i, r in stored(store).items()} == before and back.ok


def test_week_ingest_refuses_a_changed_selection_sunday(store, monkeypatch):
    from datetime import date
    ingest_season(store, 2026, season_2026_api(), now=T1)
    monkeypatch.setattr(run, "load_selection_sunday", lambda season: date(2026, 2, 15))
    api = season_2026_api()
    summary = ingest_week(store, 2026, 5, api, now=T2)
    assert not summary.ok and "run `python -m ingest.run season 2026`" in summary.failures[0]
    assert api.calls == 0 and get_season_row(store, 2026).selection_sunday == date(2026, 3, 15)


# ---- summary extras ----

def test_low_quota_and_retried_writes_are_reported_as_warnings(store):
    api = season_2026_api()
    api.quota_remaining = 51
    api.quota_floor = 50
    summary = ingest_season(store, 2026, api, now=T1)  # the run ends at 49, below the floor
    assert any("49 is below the floor 50" in w for w in summary.warnings) and summary.ok

    class Retrying(LocalStore):
        def execute(self, sql, params=()):
            stats = super().execute(sql, params)
            return WriteStats(stats.rows_written, 0, 1)

    flaky = Retrying(":memory:")
    s = ingest_season(flaky, 2026, season_2026_api(), now=T1)
    assert s.retried_writes > 0 and any("under-counted" in w for w in s.warnings)


def test_failure_is_logged_with_the_marker(store, caplog):
    with caplog.at_level(logging.ERROR):
        ingest_season(store, 2026, api_26(games=[]), now=T1)
    assert "INGEST_FAILURE: /games returned no games for season 2026" in caplog.text


# ---- CLI ----

@pytest.fixture
def cli(tmp_path, monkeypatch):
    path = tmp_path / "overrides.json"
    path.write_text('{"version": 1, "overrides": []}')
    monkeypatch.setattr(ov, "DEFAULT_OVERRIDES_PATH", path)
    monkeypatch.setenv("CBB_STORE", "local")
    monkeypatch.setenv("CBB_LOCAL_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("CBBD_API_KEY", "x")
    return path


def test_cli_applies_overrides_after_the_ingest(cli, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run, "CbbdClient", lambda: season_2026_api())
    assert run.main(["season", "2026"]) == 0
    capsys.readouterr()
    cli.write_text(json.dumps({"version": 1, "overrides": [{
        "kind": "site", "game_id": 213466, "season": 2026, "home_team_id": 902, "away_team_id": 344,
        "neutral_site": False, "swap_home_away": True, "reason": "Played at Washington State"}]}))
    assert run.main(["season", "2026"]) == 0  # the ingest reverts the swap, the CLI restores it
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["overrides"]["applied"] == [213466]
    row = LocalStore(str(tmp_path / "t.db")).query("SELECT home_team_id, alpha FROM games WHERE id = 213466")[0]
    assert (row["home_team_id"], row["alpha"]) == (344, 0.8)
    assert run.main(["week", "2026", "3"]) == 0  # the week path restores them too
    assert json.loads(capsys.readouterr().out)["overrides"]["unchanged"] == [213466]


def test_cli_exit_codes_and_markers(cli, monkeypatch, capsys, caplog):
    monkeypatch.setattr(run, "CbbdClient", lambda: api_26(games=[]))
    assert run.main(["season", "2026"]) == 1
    out = capsys.readouterr().out
    assert "INGEST_FAILURE: /games returned no games for season 2026" in out and '"ok": false' in out

    monkeypatch.setattr(run, "CbbdClient", lambda: season_2026_api())
    cli.write_text(json.dumps({"version": 1, "overrides": [{
        "kind": "exclude", "game_id": 358658, "season": 2026, "home_team_id": 1, "away_team_id": 2, "reason": "x"}]}))
    with caplog.at_level(logging.ERROR):
        assert run.main(["season", "2026"]) == 1
    assert "OVERRIDE_FAILURE: game_id=358658" in caplog.text
    capsys.readouterr()

    cli.write_text("{")
    calls = []
    monkeypatch.setattr(run, "CbbdClient", lambda: calls.append(1) or season_2026_api())
    assert run.main(["season", "2026"]) == 1 and calls == []  # a broken file fails before any API call
    assert "OVERRIDE_FAILURE" in capsys.readouterr().out


def test_cli_turns_an_exception_into_the_marker(cli, monkeypatch, capsys):
    class Boom:
        calls, quota_remaining = 0, None

        def get(self, *a, **k):
            raise ConnectionError("down")

    monkeypatch.setattr(run, "CbbdClient", Boom)
    assert run.main(["season", "2026"]) == 1
    assert "INGEST_FAILURE: ConnectionError: down" in capsys.readouterr().out


# ---- quota header ----

class HeaderlessApi(FakeApi):
    quota_header_missing = True


def test_a_quota_that_could_not_be_read_is_a_warning_once_per_run(store):
    api = HeaderlessApi({2026: load("cbbd_teams_2026.json")}, {2026: load("cbbd_games_2026.json")})
    summary = ingest_season(store, 2026, api, now=T1)
    assert summary.ok
    assert len([w for w in summary.warnings if "no readable X-CallLimit-Remaining" in w]) == 1
    assert not any("quota" in w for w in ingest_season(store, 2026, season_2026_api(), now=T2).warnings)
