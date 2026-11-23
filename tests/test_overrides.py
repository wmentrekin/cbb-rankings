import json
import logging

import pytest

from ingest import overrides as ov
from ingest import run, weeks
from ingest.overrides import OverrideFileError, apply_overrides, load_overrides
from tests.fakes import season_2026_api


@pytest.fixture(autouse=True)
def small_opening_day(monkeypatch):
    original = weeks.compute_anchor_monday
    monkeypatch.setattr(run, "compute_anchor_monday", lambda dates: original(dates, min_games=3))


@pytest.fixture
def ingested(store):
    run.ingest_season(store, 2026, season_2026_api())
    return store


def game(store, game_id):
    return store.query("SELECT * FROM games WHERE id = ?", [game_id])[0]


def entry(store, game_id, kind="score", **changes):
    g = game(store, game_id)
    e = {"kind": kind, "game_id": game_id, "season": 2026, "home_team_id": g["home_team_id"],
         "away_team_id": g["away_team_id"], "reason": "test"}
    if kind == "score":
        e.update(home_score=70, away_score=60)
    e.update(changes)
    return e


def write(tmp_path, entries, **top):
    path = tmp_path / "o.json"
    path.write_text(json.dumps({"version": 1, "overrides": entries, **top}))
    return path


HOME_WIN, AWAY_WIN, NEUTRAL, POSTPONED = 358658, 210456, 209784, 213650


# ---- file validation ----

def test_shipped_file_is_an_empty_shell():
    assert load_overrides() == []
    assert json.loads(ov.DEFAULT_OVERRIDES_PATH.read_text()) == {"version": 1, "overrides": []}


def good():
    return {"kind": "score", "game_id": 1, "season": 2026, "home_team_id": 2, "away_team_id": 3,
            "home_score": 70, "away_score": 60, "reason": "r"}


def good_exclude():
    return {"kind": "exclude", "game_id": 2, "season": 2026, "home_team_id": 2, "away_team_id": 3, "reason": "r"}


def test_valid_file_with_both_kinds_loads(tmp_path):
    entries = [good(), {**good_exclude()}, {**good(), "game_id": 9, "periods": 3}]
    assert load_overrides(write(tmp_path, entries)) == entries


@pytest.mark.parametrize("bad, fragment", [
    ({**good(), "kind": "delete"}, "kind must be one of"),
    ({k: v for k, v in good().items() if k != "reason"}, "missing key(s) ['reason']"),
    ({**good(), "reason": "  "}, "reason must be a non-empty string"),
    ({**good(), "reason": 5}, "reason must be a non-empty string"),
    ({**good(), "extra": 1}, "unknown key(s) ['extra']"),
    ({**good_exclude(), "home_score": 1}, "unknown key(s) ['home_score']"),
    ({k: v for k, v in good().items() if k != "away_score"}, "missing key(s) ['away_score']"),
    ({**good(), "home_score": 70.0}, "home_score must be an integer"),
    ({**good(), "home_score": True}, "home_score must be an integer"),
    ({**good(), "game_id": "1"}, "game_id must be an integer"),
    ({**good(), "away_score": 70}, "a tie is not a result"),
    ({**good(), "home_score": -1}, "home_score must be >= 0"),
    ({**good(), "periods": 1}, "periods must be >= 2"),
    ({**good(), "away_team_id": 2}, "home_team_id and away_team_id are the same"),
    ("not an object", "is not an object"),
])
def test_entry_rejections(tmp_path, bad, fragment):
    with pytest.raises(OverrideFileError, match="overrides\\[0\\]") as err:
        load_overrides(write(tmp_path, [bad]))
    assert fragment in str(err.value)


def test_duplicate_game_ids_rejected_across_kinds(tmp_path):
    with pytest.raises(OverrideFileError, match="duplicate game_id"):
        load_overrides(write(tmp_path, [good(), {**good_exclude(), "game_id": 1}]))


def test_file_level_rejections(tmp_path):
    p = tmp_path / "o.json"
    for text, fragment in [
        ("{", "cannot read as JSON"),
        ("[]", "exactly 'version' and 'overrides'"),
        ('{"version": 1}', "exactly 'version' and 'overrides'"),
        ('{"version": 1, "overrides": [], "x": 1}', "exactly 'version' and 'overrides'"),
        ('{"version": 2, "overrides": []}', "version must be 1"),
        ('{"version": 1, "overrides": {}}', "'overrides' must be a list"),
    ]:
        p.write_text(text)
        with pytest.raises(OverrideFileError, match=fragment):
            load_overrides(p)
    with pytest.raises(OverrideFileError, match="cannot read"):
        load_overrides(tmp_path / "missing.json")


def test_all_problems_are_listed_together(tmp_path):
    with pytest.raises(OverrideFileError) as err:
        load_overrides(write(tmp_path, [{**good(), "reason": ""}, {**good(), "kind": "x"}]))
    assert "overrides[0]" in str(err.value) and "overrides[1]" in str(err.value)


def test_malformed_file_is_a_hard_error_with_the_marker(ingested, tmp_path, caplog):
    path = tmp_path / "o.json"
    path.write_text("{")
    with caplog.at_level(logging.ERROR), pytest.raises(OverrideFileError):
        apply_overrides(ingested, 2026, path=path)
    assert "OVERRIDE_FAILURE" in caplog.text


# ---- score overrides ----

def test_score_override_rederives_for_home_win_away_win_and_neutral(ingested, tmp_path):
    # flip each game's result and check winner, margin, alpha
    h, a, n = (game(ingested, g) for g in (HOME_WIN, AWAY_WIN, NEUTRAL))
    entries = [
        entry(ingested, HOME_WIN, home_score=50, away_score=60),   # was a home win, now away win
        entry(ingested, AWAY_WIN, home_score=75, away_score=70),   # was an away win, now home win
        entry(ingested, NEUTRAL, home_score=60, away_score=64),    # neutral, other side wins
    ]
    s = apply_overrides(ingested, 2026, path=write(tmp_path, entries))
    assert s.applied == [HOME_WIN, AWAY_WIN, NEUTRAL] and s.failed == [] and s.unchanged == []

    g = game(ingested, HOME_WIN)
    assert (g["home_score"], g["away_score"], g["margin"], g["winner_team_id"], g["alpha"]) == (50, 60, 10, h["away_team_id"], 1.2)
    g = game(ingested, AWAY_WIN)
    assert (g["home_score"], g["away_score"], g["margin"], g["winner_team_id"], g["alpha"]) == (75, 70, 5, a["home_team_id"], 0.8)
    g = game(ingested, NEUTRAL)
    assert (g["margin"], g["winner_team_id"], g["alpha"]) == (4, n["away_team_id"], 1.0)
    # everything else on the row is untouched
    for before, gid in ((h, HOME_WIN), (a, AWAY_WIN), (n, NEUTRAL)):
        after = game(ingested, gid)
        for col in before:
            if col not in {"home_score", "away_score", "margin", "winner_team_id", "alpha"}:
                assert after[col] == before[col], col


def test_score_override_sets_periods_only_when_given(ingested, tmp_path):
    before = game(ingested, HOME_WIN)["periods"]
    apply_overrides(ingested, 2026, path=write(tmp_path, [entry(ingested, HOME_WIN)]))
    assert game(ingested, HOME_WIN)["periods"] == before
    apply_overrides(ingested, 2026, path=write(tmp_path, [entry(ingested, HOME_WIN, periods=4)]))
    assert game(ingested, HOME_WIN)["periods"] == 4


def test_score_override_fixes_a_final_stored_without_a_result(ingested, tmp_path):
    ingested.execute("UPDATE games SET home_score = NULL, away_score = NULL, margin = NULL, winner_team_id = NULL, alpha = NULL WHERE id = ?", [HOME_WIN])
    s = apply_overrides(ingested, 2026, path=write(tmp_path, [entry(ingested, HOME_WIN)]))
    assert s.applied == [HOME_WIN]
    assert game(ingested, HOME_WIN)["winner_team_id"] == game(ingested, HOME_WIN)["home_team_id"]


def test_score_override_on_a_game_that_is_not_final_fails(ingested, tmp_path, caplog):
    before = game(ingested, POSTPONED)
    with caplog.at_level(logging.ERROR):
        s = apply_overrides(ingested, 2026, path=write(tmp_path, [entry(ingested, POSTPONED)]))
    assert s.failed and "stored status is 'postponed'" in s.failed[0]["reason"]
    assert game(ingested, POSTPONED) == before


# ---- exclude ----

def test_exclude_unrates_and_keeps_scores(ingested, tmp_path):
    before = game(ingested, HOME_WIN)
    assert before["is_rated"] == 1
    s = apply_overrides(ingested, 2026, path=write(tmp_path, [entry(ingested, HOME_WIN, "exclude")]))
    assert s.applied == [HOME_WIN]
    after = game(ingested, HOME_WIN)
    assert after == {**before, "is_rated": 0}


# ---- safety checks ----

@pytest.mark.parametrize("changes, fragment", [
    ({"game_id": 1}, "no stored game"),
    ({"season": 2025}, "season mismatch"),
    ({"home_team_id": 1}, "home_team_id mismatch"),
    ({"away_team_id": 1}, "away_team_id mismatch"),
])
def test_safety_check_mismatch_fails_that_entry_only(ingested, tmp_path, caplog, changes, fragment):
    bad = {**entry(ingested, AWAY_WIN), **changes}
    good_entry = entry(ingested, NEUTRAL, home_score=1, away_score=2)
    untouched = game(ingested, AWAY_WIN)
    with caplog.at_level(logging.ERROR):
        s = apply_overrides(ingested, None, path=write(tmp_path, [bad, good_entry]))
    assert [f["game_id"] for f in s.failed] == [bad["game_id"]] and fragment in s.failed[0]["reason"]
    assert s.applied == [NEUTRAL]  # the other entry still applied
    if "game_id" not in changes:
        assert game(ingested, AWAY_WIN) == untouched
    assert f"OVERRIDE_FAILURE: game_id={bad['game_id']}" in caplog.text
    assert not s.ok


def test_other_seasons_are_skipped_not_failed(ingested, tmp_path):
    e = entry(ingested, HOME_WIN, season=2027)
    s = apply_overrides(ingested, 2026, path=write(tmp_path, [e]))
    assert s.skipped_other_season == [HOME_WIN] and s.ok and s.applied == []


# ---- idempotence and re-ingest ----

def test_second_apply_writes_nothing(ingested, tmp_path):
    path = write(tmp_path, [entry(ingested, HOME_WIN, home_score=50, away_score=60), entry(ingested, NEUTRAL, "exclude")])
    first = apply_overrides(ingested, 2026, path=path)
    assert first.rows_written > 0
    changes = ingested._conn.total_changes
    second = apply_overrides(ingested, 2026, path=path)
    assert second.applied == [] and sorted(second.unchanged) == sorted([HOME_WIN, NEUTRAL])
    assert second.rows_written == 0 and ingested._conn.total_changes == changes


def test_overrides_survive_reingest_when_reapplied(ingested, tmp_path):
    path = write(tmp_path, [entry(ingested, HOME_WIN, home_score=50, away_score=60), entry(ingested, NEUTRAL, "exclude")])
    original = {g: game(ingested, g) for g in (HOME_WIN, NEUTRAL)}
    apply_overrides(ingested, 2026, path=path)
    corrected = {g: game(ingested, g) for g in (HOME_WIN, NEUTRAL)}
    assert corrected != original

    run.ingest_season(ingested, 2026, season_2026_api())  # the source wins again
    assert {g: game(ingested, g) for g in (HOME_WIN, NEUTRAL)} == original

    s = apply_overrides(ingested, 2026, path=path)
    assert s.applied == [HOME_WIN, NEUTRAL]
    assert {g: game(ingested, g) for g in (HOME_WIN, NEUTRAL)} == corrected
