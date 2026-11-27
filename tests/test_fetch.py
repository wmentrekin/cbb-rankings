from datetime import date, datetime, timedelta, timezone

import pytest

from ingest.games import GAMES_CAP, fetch_games_range, fetch_season_games, fetch_week_games, week_window
from ingest.teams import team_row, team_rows
from ingest.weeks import SeasonRow
from tests.fakes import FakeApi, load, season_2026_api

START = datetime(2025, 10, 1, tzinfo=timezone.utc)


def synthetic(n, first_id=1, per_slot=1, start=datetime(2025, 11, 3, tzinfo=timezone.utc)):
    """SYNTHETIC rows, only fields paging reads: `per_slot` games share each half-hour."""
    return [
        {"id": first_id + i, "startDate": (start + timedelta(minutes=30 * (i // per_slot))).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
        for i in range(n)
    ]


def api_with(rows, cap=GAMES_CAP):
    return FakeApi({}, {2026: rows}, cap)


def test_cap_is_3000():
    assert GAMES_CAP == 3000


def test_single_page_when_under_the_cap():
    api = api_with(synthetic(2999))
    games = fetch_games_range(api, 2026, START, datetime(2026, 5, 1, tzinfo=timezone.utc))
    assert len(games) == 2999 and api.calls == 1


def test_pages_around_the_cap_and_dedupes_by_id():
    rows = synthetic(7000)
    api = api_with(rows)
    games = fetch_games_range(api, 2026, START, datetime(2026, 12, 1, tzinfo=timezone.utc))
    assert [g["id"] for g in games] == [r["id"] for r in rows]  # complete, no duplicates, oldest first
    assert api.calls == 3  # 3,000 + 3,000 + the rest (overlap rows re-sent and dropped)
    firsts = [r[1]["startDateRange"] for r in api.requests]
    assert firsts[0] == "2025-10-01T00:00:00Z" and firsts[1] > firsts[0] and firsts[2] > firsts[1]


def test_rows_sharing_the_cutoff_timestamp_are_not_lost():
    # 4 games per slot: the 3,000 cap cuts a slot in half (3000 % 4 == 0 -> use 7 per slot).
    rows = synthetic(6500, per_slot=7)
    assert 3000 % 7 != 0
    api = api_with(rows)
    games = fetch_games_range(api, 2026, START, datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert sorted(g["id"] for g in games) == [r["id"] for r in rows]


def test_exact_multiple_of_the_cap_ends_with_an_empty_page():
    api = api_with(synthetic(3000))
    games = fetch_games_range(api, 2026, START, datetime(2026, 12, 1, tzinfo=timezone.utc))
    assert len(games) == 3000
    assert api.calls == 2  # the full page forces one more look; the second returns only the overlap


def test_no_progress_at_the_cap_raises():
    rows = synthetic(3000, per_slot=3000)  # every game at the same instant
    with pytest.raises(RuntimeError, match="without advancing"):
        fetch_games_range(api_with(rows), 2026, datetime(2025, 11, 3, tzinfo=timezone.utc), datetime(2025, 12, 1, tzinfo=timezone.utc))


def test_fixture_paging_with_a_small_cap_matches_one_big_page():
    big = fetch_season_games(season_2026_api(), 2026)
    small_api = season_2026_api(cap=6)
    small = fetch_season_games(small_api, 2026, cap=6)
    assert {g["id"] for g in small} == {g["id"] for g in big} and len(small) == len(big) == 40
    assert small_api.calls > 1


def test_full_season_request_bounds():
    api = season_2026_api()
    fetch_season_games(api, 2026)
    (path, params), = api.requests
    assert path == "/games" and params["season"] == 2026
    assert params["startDateRange"] == "2025-10-01T00:00:00Z"
    assert params["endDateRange"] == "2026-05-31T23:00:00Z"


S = SeasonRow(2026, date(2025, 11, 3), date(2026, 3, 15))


def test_week_window_default_is_10_days_before_monday_through_sunday():
    start, end = week_window(S, 10)  # week 10: Mon 2026-01-05 .. Sun 2026-01-11
    assert start == datetime(2025, 12, 26, tzinfo=timezone.utc)
    assert end == datetime(2026, 1, 12, 6, tzinfo=timezone.utc)  # after Sunday night ET (<= 05:00Z)
    assert (end - start) < timedelta(days=18)


def test_week_fetch_is_one_call_and_includes_the_late_sunday_game():
    rows = [
        {"id": 1, "startDate": "2025-12-26T05:00:00.000Z"},  # 10 days before Monday 01-05: first day of the window
        {"id": 2, "startDate": "2025-12-25T18:00:00.000Z"},  # 11 days before: outside
        {"id": 3, "startDate": "2026-01-12T03:30:00.000Z"},  # Sunday 22:30 ET
        {"id": 4, "startDate": "2026-01-12T08:00:00.000Z"},  # Monday morning ET: after the window
    ]
    api = api_with(rows)
    games = fetch_week_games(api, S, 10)
    assert api.calls == 1
    assert {g["id"] for g in games} == {1, 3}


def test_week_fetch_on_fixture_is_one_call():
    api = season_2026_api()
    games = fetch_week_games(api, S, 1)
    assert api.calls == 1 and len(games) > 5


def test_team_rows_map_fields_and_logos():
    raw = load("cbbd_teams_2026.json")
    rows = team_rows(raw, 2026)
    assert len(rows) == len(raw)
    winthrop = next(r for r in rows if r["id"] == 354)
    assert winthrop["season"] == 2026 and winthrop["school"] == "Winthrop"
    assert winthrop["conference"] == "Big South" and winthrop["conference_id"] == 9
    assert winthrop["logo"] == f"https://cdn.collegefootballdata.com/cbb-logos/500/{winthrop['source_id']}.png"
    assert winthrop["logo_dark"] == f"https://cdn.collegefootballdata.com/cbb-logos-dark/500/{winthrop['source_id']}.png"
    assert not winthrop["primary_color"].startswith("#")


def test_team_row_logo_fallback_and_nulls():
    raw = {"id": 5, "sourceId": "99", "school": "X", "conferenceId": 1, "conference": "C", "logos": [], "primaryColor": None}
    row = team_row(raw, 2026)
    assert row["logo"].endswith("/cbb-logos/500/99.png") and row["logo_dark"].endswith("/cbb-logos-dark/500/99.png")
    assert row["primary_color"] is None and row["secondary_color"] is None and row["abbreviation"] is None
    raw["sourceId"] = "UNKNOWN#X"
    assert team_row(raw, 2026)["logo"] is None


def test_team_without_conference_is_rejected():
    with pytest.raises(ValueError, match="no conference"):
        team_row({"id": 5, "school": "X", "conferenceId": None, "conference": None}, 2026)
