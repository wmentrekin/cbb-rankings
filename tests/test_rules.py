import pytest

from ingest import rules, run, weeks
from ingest.derive import derive_result
from ingest.run import ingest_season
from tests.fakes import season_2026_api


@pytest.fixture(autouse=True)
def small_opening_day(monkeypatch):
    original = weeks.compute_anchor_monday
    monkeypatch.setattr(run, "compute_anchor_monday", lambda dates: original(dates, min_games=3))


def test_played_and_rated_played_helpers():
    assert rules.is_played({"winner_team_id": 5}) and not rules.is_played({"winner_team_id": None})
    assert rules.is_rated_played({"is_rated": 1, "winner_team_id": 5})
    assert not rules.is_rated_played({"is_rated": 0, "winner_team_id": 5})
    assert not rules.is_rated_played({"is_rated": 1, "winner_team_id": None})


def test_sql_predicates_agree_with_the_python_helpers(store):
    ingest_season(store, 2026, season_2026_api())
    rows = store.query("SELECT * FROM games")
    for sql, helper in ((rules.PLAYED_SQL, rules.is_played), (rules.RATED_PLAYED_SQL, rules.is_rated_played)):
        by_sql = {r["id"] for r in store.query(f"SELECT id FROM games WHERE {sql}")}
        assert by_sql == {r["id"] for r in rows if helper(r)} and by_sql
    assert rules.RECORD_SQL == rules.RATED_PLAYED_SQL


def test_every_unplayed_status_has_a_null_winner():
    for status in ("scheduled", "in_progress", "postponed", "cancelled"):
        assert derive_result(status, 1, 2, 70, 60, False)[0]["winner_team_id"] is None
