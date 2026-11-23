import json
from datetime import date

import pytest

from ingest import weeks
from ingest.weeks import (
    AnchorError, SeasonConfigError, SeasonRow, compute_anchor_monday, last_week, load_selection_sunday,
    resolve_week, season_for_date, week_bounds, week_for_date,
)
from tests.fakes import load

S2026 = SeasonRow(2026, date(2025, 11, 3), date(2026, 3, 15))
S2027 = SeasonRow(2027, date(2026, 11, 2), date(2027, 3, 14))


def et_dates(name):
    from ingest.derive import eastern_date
    return [eastern_date(g["startDate"]) for g in load(name)]


# --- anchor rule ---------------------------------------------------------------------------
def test_anchor_monday_must_be_a_monday():
    with pytest.raises(ValueError):
        SeasonRow(2026, date(2025, 11, 4), date(2026, 3, 15))


def probe_dates(first_day_counts):
    return [d for d, n in first_day_counts.items() for _ in range(n)]


def test_anchor_2025_26_from_probe_counts():
    # Games per Eastern date from the probe's 2025-26 data: opener on Monday 11-03 with 169.
    dates = probe_dates({date(2025, 11, 3): 169, date(2025, 11, 4): 36, date(2025, 11, 9): 33})
    assert compute_anchor_monday(dates) == date(2025, 11, 3)


def test_anchor_2026_27_ignores_the_sunday_opener():
    # Probe: 2 games on Sunday 2026-11-01, 182 on Monday 2026-11-02.
    dates = probe_dates({date(2026, 11, 1): 2, date(2026, 11, 2): 182, date(2026, 11, 3): 29})
    assert compute_anchor_monday(dates) == date(2026, 11, 2)


def test_anchor_on_real_fixture_dates():
    assert compute_anchor_monday(et_dates("cbbd_games_2026.json"), min_games=3) == date(2025, 11, 3)
    assert compute_anchor_monday(et_dates("cbbd_games_2027.json"), min_games=3) == date(2026, 11, 2)


def test_anchor_main_opening_on_a_midweek_day_uses_that_weeks_monday():
    assert compute_anchor_monday(probe_dates({date(2026, 10, 30): 3, date(2026, 11, 4): 25})) == date(2026, 11, 2)


def test_anchor_unknown_until_a_busy_day_exists():
    with pytest.raises(AnchorError):
        compute_anchor_monday([date(2026, 11, 1)] * 5)


# --- week functions ------------------------------------------------------------------------
def test_week_1_folds_in_the_sunday_opener():
    assert week_for_date(S2027, date(2026, 11, 1)) == 1  # before the anchor
    assert week_for_date(S2027, date(2026, 10, 20)) == 1
    assert week_for_date(S2027, date(2026, 11, 2)) == 1
    assert week_for_date(S2027, date(2026, 11, 8)) == 1
    assert week_for_date(S2027, date(2026, 11, 9)) == 2
    assert week_bounds(S2027, 1) == (date(2026, 11, 2), date(2026, 11, 8))


def test_week_boundaries_are_monday_to_sunday():
    assert [week_for_date(S2026, date(2025, 11, d)) for d in (3, 9, 10, 16, 17)] == [1, 1, 2, 2, 3]
    assert week_bounds(S2026, 2) == (date(2025, 11, 10), date(2025, 11, 16))
    with pytest.raises(ValueError):
        week_bounds(S2026, 0)


def test_weeks_across_new_year():
    # Week 8 is Mon 12-22 .. Sun 12-28 (the holiday week); week 9 is 12-29 .. 01-04.
    assert week_bounds(S2026, 8) == (date(2025, 12, 22), date(2025, 12, 28))
    assert week_bounds(S2026, 9) == (date(2025, 12, 29), date(2026, 1, 4))
    assert [week_for_date(S2026, d) for d in (date(2025, 12, 28), date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 4), date(2026, 1, 5))] == [8, 9, 9, 9, 10]


def test_holiday_week_with_no_games_still_numbered():
    # Week 8 contains no fixture game on 12-24..12-28 but numbering is date-based, not data-based.
    dates = et_dates("cbbd_games_2026.json")
    assert not any(week_for_date(S2026, d) == 8 and d > date(2025, 12, 23) for d in dates)
    assert week_bounds(S2026, 8)[1] - week_bounds(S2026, 8)[0] == date(2025, 12, 28) - date(2025, 12, 22)
    assert week_for_date(S2026, date(2025, 12, 29)) - week_for_date(S2026, date(2025, 12, 21)) == 2


def test_selection_sunday_week_is_the_last_week():
    assert last_week(S2026) == 19  # probe: Selection Sunday 2026-03-15 falls in week 19
    assert week_bounds(S2026, 19) == (date(2026, 3, 9), date(2026, 3, 15))
    assert week_for_date(S2026, date(2026, 3, 16)) == 20


def test_season_for_date():
    assert [season_for_date(d) for d in (date(2025, 11, 3), date(2025, 12, 31), date(2026, 1, 1), date(2026, 4, 6), date(2026, 8, 1))] == [2026, 2026, 2026, 2026, 2027]


# --- resolve_week --------------------------------------------------------------------------
def test_resolve_week_monday_publishes_the_week_that_ended_yesterday():
    assert resolve_week(S2026, date(2025, 11, 10)) == 1  # Monday after week 1
    assert resolve_week(S2026, date(2025, 11, 17)) == 2


@pytest.mark.parametrize("day", range(10, 17))
def test_resolve_week_midweek_is_stable_through_the_week(day):
    assert resolve_week(S2026, date(2025, 11, day)) == 1  # Mon 10 .. Sun 16


def test_resolve_week_before_week_1_has_ended():
    assert resolve_week(S2026, date(2025, 11, 3)) is None  # opening Monday: nothing ended yet
    assert resolve_week(S2026, date(2025, 11, 9)) is None  # Sunday of week 1
    assert resolve_week(S2026, date(2025, 10, 1)) is None
    assert resolve_week(S2026, date(2025, 8, 1)) is None


def test_resolve_week_through_selection_sunday_then_stops():
    assert resolve_week(S2026, date(2026, 3, 16)) == 19  # Monday after Selection Sunday
    assert resolve_week(S2026, date(2026, 3, 22)) == 19  # still the same week all week
    assert resolve_week(S2026, date(2026, 3, 23)) is None  # week 19 already published
    assert resolve_week(S2026, date(2026, 6, 1)) is None


def test_resolve_week_across_new_year_and_with_sunday_opener_season():
    assert resolve_week(S2026, date(2025, 12, 29)) == 8
    assert resolve_week(S2026, date(2026, 1, 5)) == 9
    assert resolve_week(S2027, date(2026, 11, 9)) == 1
    assert resolve_week(S2027, date(2026, 11, 1)) is None


# --- selection_sunday file -----------------------------------------------------------------
def test_selection_sunday_known_and_provisional(caplog):
    assert load_selection_sunday(2026) == date(2026, 3, 15)
    assert not [r for r in caplog.records if "PROVISIONAL" in r.message]
    assert load_selection_sunday(2027) == date(2027, 3, 14)
    assert "PROVISIONAL" in caplog.text and weeks.is_provisional(2027) and not weeks.is_provisional(2026)


def test_missing_season_names_the_file_and_the_fix():
    with pytest.raises(SeasonConfigError, match=r"season 2031 is not in seasons.json: add"):
        load_selection_sunday(2031)


def test_malformed_entry(tmp_path):
    path = tmp_path / "seasons.json"
    path.write_text(json.dumps({"2030": {"selection_sunday": "soon"}}))
    with pytest.raises(SeasonConfigError):
        load_selection_sunday(2030, path)


def test_today_et_uses_eastern_calendar_date():
    from datetime import datetime, timezone
    assert weeks.today_et(datetime(2026, 3, 16, 3, 0, tzinfo=timezone.utc)) == date(2026, 3, 15)
