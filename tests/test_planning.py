from datetime import datetime, timezone

import pytest

from database.local import LocalStore
from ingest import run, weeks
from ingest.planning import CachingClient, DryRunWriteError, PlanningStore
from tests.fakes import season_2026_api


@pytest.fixture(autouse=True)
def small_opening_day(monkeypatch):
    original = weeks.compute_anchor_monday
    monkeypatch.setattr(run, "compute_anchor_monday", lambda dates: original(dates, min_games=3))


def snapshot(store):
    return {t: store.query(f"SELECT * FROM {t} ORDER BY 1, 2") for t in ("teams", "games", "seasons")}


def test_plan_on_an_empty_store_counts_new_rows_and_writes_nothing(store):
    planner = PlanningStore(store)
    summary = run.ingest_season(planner, 2026, season_2026_api())
    assert snapshot(store) == {"teams": [], "games": [], "seasons": []}
    assert planner.plans["teams"].new == 59 and planner.plans["games"].new == 40
    # the seasons row is planned twice (anchor, then timestamp): one insert, one change
    assert (planner.plans["seasons"].new, planner.plans["seasons"].changed) == (1, 1)
    assert planner.plans["games"].estimated_rows_written == 120  # 3 per new game
    assert planner.plans["teams"].estimated_rows_written == 59
    assert planner.estimated_rows_written == 59 + 120 + 2 == summary.total_rows_written


def test_plan_matches_a_real_run_and_is_zero_on_a_populated_store(store):
    run.ingest_season(store, 2026, season_2026_api(), now=lambda: datetime(2026, 4, 7, tzinfo=timezone.utc))
    before = snapshot(store)
    planner = PlanningStore(store)
    run.ingest_season(planner, 2026, season_2026_api(), now=lambda: datetime(2026, 4, 7, tzinfo=timezone.utc))
    assert planner.estimated_rows_written == 0
    assert all(p.new == 0 and p.changed == 0 for p in planner.plans.values())
    assert planner.plans["games"].unchanged == 40
    assert snapshot(store) == before


def test_plan_counts_changed_rows_and_index_writes(store):
    run.ingest_season(store, 2026, season_2026_api())
    store.execute("UPDATE games SET home_score = 1 WHERE id = 358658")  # not indexed
    store.execute("UPDATE games SET is_rated = 0 WHERE id = 209784")  # indexed
    planner = PlanningStore(store)
    run.ingest_season(planner, 2026, season_2026_api())
    games = planner.plans["games"]
    assert (games.new, games.changed, games.unchanged) == (0, 2, 38)
    assert games.estimated_rows_written == 1 + 3


def test_planning_store_refuses_other_writes(store):
    planner = PlanningStore(store)
    with pytest.raises(DryRunWriteError):
        planner.execute("DELETE FROM games")
    with pytest.raises(DryRunWriteError):
        planner.execute_batch([("DELETE FROM games", [])])


def test_a_repeated_key_is_compared_with_the_planned_row(store):
    planner = PlanningStore(store)
    row = {"season": 2026, "anchor_monday": "2025-11-03", "selection_sunday": "2026-03-15", "last_ingested_at": None}
    planner.upsert("seasons", [row], ("season",))
    planner.upsert("seasons", [row], ("season",))
    assert (planner.plans["seasons"].new, planner.plans["seasons"].unchanged) == (1, 1)


def test_caching_client_spends_each_request_once():
    api = season_2026_api()
    cached = CachingClient(api)
    first = cached.get("/teams", {"season": 2026})
    first.clear()  # mutating a response must not poison the cache
    assert len(cached.get("/teams", {"season": 2026})) == 59
    assert cached.calls == api.calls == 1 and cached.quota_remaining == 899
