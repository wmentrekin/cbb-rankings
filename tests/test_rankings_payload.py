from datetime import datetime, timezone

import pytest

from artifacts.rankings import build_payload, previous_week_with_data, rank_rows, stored_week

NOW = datetime(2026, 3, 16, 10, 0, tzinfo=timezone.utc)


def add_team(store, team_id, school, conference="ACC", logo="https://logo/dark.png", season=2026):
    store.upsert("teams", [{
        "id": team_id, "season": season, "source_id": str(team_id), "school": school, "abbreviation": None,
        "conference_id": 1, "conference": conference, "primary_color": None, "secondary_color": None,
        "logo": "https://logo/light.png", "logo_dark": logo,
    }], ("id", "season"))


def add_ratings(store, week, values, season=2026, table="ratings"):
    """values: {team_id: (rating, wins, losses)}"""
    store.upsert(table, [
        {"team_id": t, "season": season, "week": week, "rating": r, "wins": w, "losses": l}
        for t, (r, w, l) in values.items()
    ], ("team_id", "season", "week"))


def test_payload_shape_rank_rounding_record_logo_and_conference(store):
    add_team(store, 1, "Duke", "ACC")
    add_team(store, 2, "Gonzaga", "WCC", logo=None)
    add_team(store, 3, "Boise State", "Mountain West")
    add_ratings(store, 5, {1: (80.126, 10, 2), 2: (90.0049, 12, 0), 3: (40.5, 5, 7)})
    payload = build_payload(store, 2026, 5, now=NOW)

    assert list(payload) == ["season", "week", "generated_at_utc", "rankings"]
    assert payload["season"] == 2026 and payload["week"] == 5
    assert payload["generated_at_utc"] == "2026-03-16T10:00:00+00:00"
    assert [list(r) for r in payload["rankings"]] == [["rank", "team", "conference", "logo", "record", "rating", "delta"]] * 3
    assert payload["rankings"] == [
        {"rank": 1, "team": "Gonzaga", "conference": "WCC", "logo": None, "record": "12-0", "rating": 90.0, "delta": 0},
        {"rank": 2, "team": "Duke", "conference": "ACC", "logo": "https://logo/dark.png", "record": "10-2", "rating": 80.13, "delta": 0},
        {"rank": 3, "team": "Boise State", "conference": "Mountain West", "logo": "https://logo/dark.png", "record": "5-7", "rating": 40.5, "delta": 0},
    ]


def test_ties_break_by_team_name_ascending_on_the_stored_rating():
    rows = [{"team": "Zed", "rating": 50.0}, {"team": "Abe", "rating": 50.0}, {"team": "Mid", "rating": 50.001}]
    assert [(r["team"], r["rank"]) for r in rank_rows(rows)] == [("Mid", 1), ("Abe", 2), ("Zed", 3)]


def test_delta_is_previous_rank_minus_current_against_the_greatest_earlier_week(store):
    for i, name in enumerate(["A", "B", "C", "D"], 1):
        add_team(store, i, name)
    add_ratings(store, 1, {1: (10, 1, 0), 2: (20, 1, 0), 3: (30, 1, 0)})        # ranks C B A (D not rated)
    add_ratings(store, 3, {1: (30, 2, 0), 2: (20, 2, 0), 3: (10, 2, 0), 4: (5, 0, 2)})  # week 2 has no ratings
    add_ratings(store, 5, {1: (30, 3, 0), 2: (10, 3, 0), 3: (20, 3, 0), 4: (5, 0, 3)})
    assert previous_week_with_data(store, 2026, 5) == 3
    assert previous_week_with_data(store, 2026, 1) is None
    wk5 = {r["team"]: r for r in build_payload(store, 2026, 5, now=NOW)["rankings"]}
    # week 3 ranks: A1 B2 C3 D4; week 5 ranks: A1 C2 B3 D4
    assert (wk5["A"]["delta"], wk5["C"]["delta"], wk5["B"]["delta"], wk5["D"]["delta"]) == (0, 1, -1, 0)
    wk3 = {r["team"]: r for r in build_payload(store, 2026, 3, now=NOW)["rankings"]}
    # week 1 ranks: C1 B2 A3; D unranked then -> 0
    assert (wk3["A"]["delta"], wk3["B"]["delta"], wk3["C"]["delta"], wk3["D"]["delta"]) == (2, 0, -2, 0)
    assert all(r["delta"] == 0 for r in build_payload(store, 2026, 1, now=NOW)["rankings"])


def test_previous_week_is_per_season(store):
    add_team(store, 1, "A")
    add_ratings(store, 9, {1: (10, 1, 0)}, season=2025)
    add_ratings(store, 2, {1: (10, 1, 0)})
    assert previous_week_with_data(store, 2026, 2) is None


def test_join_is_by_team_id_and_season(store):
    add_team(store, 1, "Old Name", season=2025)
    add_team(store, 1, "New Name", season=2026)
    add_ratings(store, 1, {1: (10, 1, 0)})
    assert [r["team"] for r in stored_week(store, 2026, 1)] == ["New Name"]


def test_builds_from_the_requested_table_only(store):
    add_team(store, 1, "A")
    add_ratings(store, 1, {1: (10, 1, 0)})
    add_ratings(store, 1, {1: (99, 5, 0)}, table="ratings_staging")
    assert build_payload(store, 2026, 1, now=NOW)["rankings"][0]["rating"] == 10.0
    assert build_payload(store, 2026, 1, table="ratings_staging", now=NOW)["rankings"][0]["rating"] == 99.0
    with pytest.raises(ValueError, match="ratings table"):
        build_payload(store, 2026, 1, table="games; DROP TABLE games")


def test_no_stored_ratings_is_an_error(store):
    with pytest.raises(ValueError, match="no stored ratings"):
        build_payload(store, 2026, 1)
