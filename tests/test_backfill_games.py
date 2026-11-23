import json
from datetime import datetime, timezone

import pytest

from ingest import overrides as ov
from ingest import run, weeks
from scripts import backfill_games as bf
from tests.fakes import FakeApi, load, season_2026_api

HOME_WIN = 358658


@pytest.fixture(autouse=True)
def small_opening_day(monkeypatch):
    original = weeks.compute_anchor_monday
    monkeypatch.setattr(run, "compute_anchor_monday", lambda dates: original(dates, min_games=3))


@pytest.fixture
def overrides_file(tmp_path, monkeypatch):
    path = tmp_path / "overrides.json"
    path.write_text('{"version": 1, "overrides": []}')
    monkeypatch.setattr(ov, "DEFAULT_OVERRIDES_PATH", path)
    return path


def counts(store):
    return {t: store.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in ("teams", "games", "seasons")}


def run_cli(capsys, store, api, *argv):
    code = bf.main(list(argv), store=store, client=api)
    return code, capsys.readouterr().out


def report(out):
    start = out.index("{")
    return json.JSONDecoder().raw_decode(out[start:])[0]


def test_dry_run_is_the_default_and_writes_nothing(store, capsys, overrides_file):
    api = season_2026_api()
    code, out = run_cli(capsys, store, api, "--season", "2026")
    assert code == 0
    assert counts(store) == {"teams": 0, "games": 0, "seasons": 0}
    r = report(out)
    assert r["mode"] == "dry_run" and "nothing was written" in r["note"]
    assert "spends API calls" in out and "BACKFILL_APPLIED" not in out
    assert r["api_calls"] == api.calls == 2 and r["quota_remaining"] == 898
    assert r["plan"]["games"]["new"] == 40 and r["plan"]["teams"]["new"] == 59
    assert r["estimated_rows_written"] == 59 + 3 * 40 + 2
    assert r["games_fetched"] == 40 and r["override_entries_for_season"] == 0


def test_dry_run_on_a_populated_store_would_change_nothing(store, capsys, overrides_file):
    run.ingest_season(store, 2026, season_2026_api(), now=lambda: datetime(2026, 4, 7, tzinfo=timezone.utc))
    before = counts(store)
    code, out = run_cli(capsys, store, season_2026_api(), "--season", "2026")
    r = report(out)
    assert code == 0 and counts(store) == before
    assert all(p["new"] == 0 and p["changed"] == 0 for t, p in r["plan"].items() if t != "seasons")
    assert r["plan"]["seasons"]["changed"] == 1  # last_ingested_at only
    assert r["estimated_rows_written"] == 1


def test_apply_writes_applies_overrides_and_prints_the_marker(store, capsys, overrides_file):
    run.ingest_season(store, 2026, season_2026_api())
    g = store.query("SELECT home_team_id, away_team_id FROM games WHERE id = ?", [HOME_WIN])[0]
    overrides_file.write_text(json.dumps({"version": 1, "overrides": [{
        "kind": "score", "game_id": HOME_WIN, "season": 2026, "home_team_id": g["home_team_id"],
        "away_team_id": g["away_team_id"], "home_score": 50, "away_score": 60, "reason": "test"}]}))
    api = season_2026_api()
    code, out = run_cli(capsys, store, api, "--season", "2026", "--apply")
    assert code == 0
    assert "BACKFILL_APPLIED season=2026 " in out
    r = report(out)
    assert r["mode"] == "apply" and api.calls == 2  # the plan pass's responses were reused
    assert r["overrides"]["applied"] == [HOME_WIN] and r["overrides"]["failed"] == []
    row = store.query("SELECT * FROM games WHERE id = ?", [HOME_WIN])[0]
    assert (row["home_score"], row["away_score"], row["winner_team_id"]) == (50, 60, g["away_team_id"])


def test_apply_on_an_empty_store_writes_the_season(store, capsys, overrides_file):
    code, out = run_cli(capsys, store, season_2026_api(), "--season", "2026", "--apply")
    assert code == 0 and counts(store) == {"teams": 59, "games": 40, "seasons": 1}
    assert "BACKFILL_APPLIED season=2026 rows_written=" in out  # no week in the marker for a full season
    r = report(out)
    assert r["rows_written"]["games"] == 40 and r["estimated_rows_written"] == 181  # local counts no index rows


def test_week_mode_needs_the_season_and_reports_the_week(store, capsys, overrides_file):
    code, out = run_cli(capsys, store, season_2026_api(), "--season", "2026", "--week", "2")
    assert code == 1 and "BACKFILL_FAILED season=2026 week=2" in out and "SeasonNotIngestedError" in out
    run.ingest_season(store, 2026, season_2026_api())
    code, out = run_cli(capsys, store, season_2026_api(), "--season", "2026", "--week", "2", "--apply")
    assert code == 0 and "BACKFILL_APPLIED season=2026 week=2 " in out


def test_guard_refuses_apply_over_the_limit_and_writes_nothing(store, capsys, overrides_file):
    api = season_2026_api()
    code, out = run_cli(capsys, store, api, "--season", "2026", "--apply", "--max-rows-written", "100")
    assert code == 2 and "exceeds --max-rows-written 100" in out and "BACKFILL_APPLIED" not in out
    assert counts(store) == {"teams": 0, "games": 0, "seasons": 0}
    code, out = run_cli(capsys, store, season_2026_api(), "--season", "2026", "--apply", "--max-rows-written", "181")
    assert code == 0 and counts(store)["games"] == 40  # at the limit is allowed


def test_default_limit_is_40000():
    assert bf.DEFAULT_MAX_ROWS_WRITTEN == 40_000


def test_override_failure_exits_non_zero_but_still_applies(store, capsys, overrides_file):
    overrides_file.write_text(json.dumps({"version": 1, "overrides": [{
        "kind": "exclude", "game_id": HOME_WIN, "season": 2026, "home_team_id": 1, "away_team_id": 2, "reason": "x"}]}))
    code, out = run_cli(capsys, store, season_2026_api(), "--season", "2026", "--apply")
    assert code == 1 and counts(store)["games"] == 40
    assert report(out)["overrides"]["failed"][0]["game_id"] == HOME_WIN
    assert "BACKFILL_APPLIED" in out


def test_malformed_override_file_fails_before_any_fetch(store, capsys, overrides_file):
    overrides_file.write_text("{")
    api = season_2026_api()
    code, out = run_cli(capsys, store, api, "--season", "2026", "--apply")
    assert code == 1 and "OVERRIDE_FAILURE" in out and api.calls == 0


def test_fetch_failure_and_empty_fetch_exit_non_zero(store, capsys, overrides_file):
    class Broken:
        calls, quota_remaining = 0, None

        def get(self, path, params=None):
            raise ConnectionError("boom")

    code, out = run_cli(capsys, store, Broken(), "--season", "2026", "--apply")
    assert code == 1 and "BACKFILL_FAILED" in out and "boom" in out

    empty = season_2026_api()
    empty.games[2026] = []
    code, out = run_cli(capsys, store, empty, "--season", "2026")
    assert code == 1 and "BACKFILL_FAILED" in out  # a new season with no games cannot even get an anchor
    assert counts(store) == {"teams": 0, "games": 0, "seasons": 0}


@pytest.mark.parametrize("argv", [["--season", "1"], ["--season", "2026", "--week", "0"],
                                  ["--season", "2026", "--max-rows-written", "0"], []])
def test_usage_errors_exit_2(store, argv):
    with pytest.raises(SystemExit) as e:
        bf.main(argv, store=store, client=season_2026_api())
    assert e.value.code == 2


# ---- ingest gates, guards and override kinds in the dry run ----

def test_dry_run_reports_what_the_guards_would_do_and_writes_nothing(store, capsys, overrides_file):
    run.ingest_season(store, 2026, season_2026_api(), now=lambda: datetime(2026, 4, 7, tzinfo=timezone.utc))
    games = [g for g in load("cbbd_games_2026.json") if g["id"] != 212784]
    api = FakeApi({2026: load("cbbd_teams_2026.json")}, {2026: games})
    before = store.query("SELECT * FROM games ORDER BY id")
    code, out = run_cli(capsys, store, api, "--season", "2026")
    r = report(out)
    assert code == 0 and store.query("SELECT * FROM games ORDER BY id") == before
    assert r["anomalies"] == {"absent_from_source": 1} and r["anomaly_ids"] == {"absent_from_source": [212784]}
    assert r["plan"]["games"]["changed"] == 1 and r["estimated_rows_written"] == 3 + 1  # the neutralized game and the timestamp


def test_estimate_adds_the_restore_cost_of_each_override_kind(store, capsys, overrides_file):
    entries = [
        {"kind": "score", "game_id": HOME_WIN, "season": 2026, "home_team_id": 77, "away_team_id": 50, "home_score": 1,
         "away_score": 2, "reason": "x"},
        {"kind": "site", "game_id": 213466, "season": 2026, "home_team_id": 902, "away_team_id": 344,
         "neutral_site": False, "swap_home_away": True, "reason": "x"},
        {"kind": "exclude", "game_id": 209784, "season": 2026, "home_team_id": 1, "away_team_id": 2, "reason": "x"},
    ]
    overrides_file.write_text(json.dumps({"version": 1, "overrides": entries}))
    code, out = run_cli(capsys, store, season_2026_api(), "--season", "2026")
    r = report(out)
    assert code == 0 and r["override_entries_for_season"] == 3
    assert r["estimated_rows_written"] == 59 + 3 * 40 + 2 + (1 + 1 + 3)


def test_failed_gate_prints_the_marker_and_exits_1_even_on_a_dry_run(store, capsys, overrides_file):
    games = load("cbbd_games_2026.json") + [
        {"id": 9_100_000 + i, "startDate": "2025-11-05T00:00:00.000Z", "status": "final"} for i in range(6)]  # SYNTHETIC
    api = FakeApi({2026: load("cbbd_teams_2026.json")}, {2026: games})
    code, out = run_cli(capsys, store, api, "--season", "2026", "--apply")
    assert code == 1 and "INGEST_FAILURE: 6 of 46 fetched games were dropped or malformed" in out
    assert "BACKFILL_FAILED" in out and "BACKFILL_APPLIED" not in out
    assert counts(store) == {"teams": 0, "games": 0, "seasons": 0}  # apply wrote nothing


def test_empty_fetch_prints_the_ingest_marker(store, capsys, overrides_file):
    empty = season_2026_api()
    empty.games[2026] = []
    code, out = run_cli(capsys, store, empty, "--season", "2026")
    assert code == 1 and "INGEST_FAILURE: /games returned no games for season 2026" in out


def test_apply_reports_the_ok_flag_and_applies_a_site_override(store, capsys, overrides_file):
    overrides_file.write_text(json.dumps({"version": 1, "overrides": [{
        "kind": "site", "game_id": 213466, "season": 2026, "home_team_id": 902, "away_team_id": 344,
        "neutral_site": False, "swap_home_away": True, "reason": "Played at Washington State"}]}))
    code, out = run_cli(capsys, store, season_2026_api(), "--season", "2026", "--apply")
    r = report(out)
    assert code == 0 and r["ok"] is True and r["failures"] == [] and r["overrides"]["applied"] == [213466]
    assert store.query("SELECT home_team_id FROM games WHERE id = 213466")[0]["home_team_id"] == 344
