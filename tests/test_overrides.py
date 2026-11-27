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


def good_site():
    return {"kind": "site", "game_id": 3, "season": 2026, "home_team_id": 2, "away_team_id": 3,
            "neutral_site": True, "swap_home_away": False, "reason": "r"}


def test_one_score_or_site_entry_plus_one_exclude_per_game(tmp_path):
    entries = [good(), {**good_exclude(), "game_id": 1}, {**good_site(), "game_id": 9}, {**good_exclude(), "game_id": 9}]
    assert load_overrides(write(tmp_path, entries)) == entries
    for second, label in (({**good(), "home_score": 71}, "score/site"), ({**good_site(), "game_id": 1}, "score/site"),
                          ({**good_exclude(), "game_id": 1}, "exclude")):
        first = [good(), {**good_exclude(), "game_id": 1}] if label == "exclude" else [good()]
        with pytest.raises(OverrideFileError, match=f"duplicate game_id.*at most one {label}"):
            load_overrides(write(tmp_path, first + [second]))


@pytest.mark.parametrize("bad, fragment", [
    ({k: v for k, v in good_site().items() if k != "neutral_site"}, "missing key(s) ['neutral_site']"),
    ({**good_site(), "neutral_site": 1}, "neutral_site must be true or false"),
    ({**good_site(), "swap_home_away": "yes"}, "swap_home_away must be true or false"),
    ({**good_site(), "home_score": 1}, "unknown key(s) ['home_score']"),
    ({**good_site(), "reason": ""}, "reason must be a non-empty string"),
    ({**good_exclude(), "neutral_site": True}, "unknown key(s) ['neutral_site']"),
])
def test_site_entry_rejections(tmp_path, bad, fragment):
    with pytest.raises(OverrideFileError, match="overrides\\[0\\]") as err:
        load_overrides(write(tmp_path, [bad]))
    assert fragment in str(err.value)


def test_swap_home_away_is_optional(tmp_path):
    entry_ = {k: v for k, v in good_site().items() if k != "swap_home_away"}
    assert load_overrides(write(tmp_path, [entry_])) == [entry_]


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


# ---- site (real rows: Chaminade, a non-D-I team the source lists as home at other schools' arenas) ----

SWAP_GAME, NEUTRAL_GAME = 213466, 358311  # source: home Chaminade at Washington State's arena; neutral Maui game marked not neutral


def site_entry(store, game_id, **changes):
    return entry(store, game_id, "site", neutral_site=False, **changes)


def test_site_override_swaps_sides_and_rederives_with_real_rows(ingested, tmp_path):
    before = game(ingested, SWAP_GAME)
    assert (before["home_team_id"], before["home_score"], before["away_team_id"], before["away_score"]) == (902, 85, 344, 90)
    assert (before["winner_team_id"], before["alpha"], before["neutral_site"]) == (344, 1.2, 0)
    s = apply_overrides(ingested, 2026, path=write(tmp_path, [site_entry(ingested, SWAP_GAME, swap_home_away=True)]))
    assert s.applied == [SWAP_GAME] and s.ok
    after = game(ingested, SWAP_GAME)
    assert (after["home_team_id"], after["home_team"], after["home_score"]) == (344, before["away_team"], 90)
    assert (after["away_team_id"], after["away_team"], after["away_score"]) == (902, before["home_team"], 85)
    assert (after["winner_team_id"], after["margin"], after["alpha"]) == (344, 5, 0.8)  # now a home win
    unchanged = {c: before[c] for c in before if not c.startswith(("home_", "away_")) and c not in ("alpha",)}
    assert {c: after[c] for c in unchanged} == unchanged


def test_site_override_sets_neutral_without_swapping(ingested, tmp_path):
    before = game(ingested, NEUTRAL_GAME)
    assert (before["neutral_site"], before["alpha"], before["winner_team_id"]) == (0, 1.2, 295)
    entry_ = entry(ingested, NEUTRAL_GAME, "site", neutral_site=True, reason="Maui Invitational, Lahaina Civic Center")
    s = apply_overrides(ingested, 2026, path=write(tmp_path, [entry_]))
    assert s.applied == [NEUTRAL_GAME]
    after = game(ingested, NEUTRAL_GAME)
    assert after == {**before, "neutral_site": 1, "alpha": 1.0}


def test_site_override_is_idempotent_even_after_the_swap(ingested, tmp_path):
    path = write(tmp_path, [site_entry(ingested, SWAP_GAME, swap_home_away=True),
                            entry(ingested, NEUTRAL_GAME, "site", neutral_site=True)])
    apply_overrides(ingested, 2026, path=path)
    changes = ingested._conn.total_changes
    second = apply_overrides(ingested, 2026, path=path)
    assert second.applied == [] and sorted(second.unchanged) == sorted([SWAP_GAME, NEUTRAL_GAME]) and second.ok
    assert second.rows_written == 0 and ingested._conn.total_changes == changes


def test_site_override_safety_checks(ingested, tmp_path, caplog):
    wrong = site_entry(ingested, SWAP_GAME, swap_home_away=True, home_team_id=1)
    no_swap_flag_but_swapped = entry(ingested, NEUTRAL_GAME, "site", neutral_site=True)
    no_swap_flag_but_swapped.update(home_team_id=295, away_team_id=902)  # the stored pair reversed, no swap requested
    postponed = site_entry(ingested, POSTPONED, swap_home_away=True, season=2025)
    before = {g: game(ingested, g) for g in (SWAP_GAME, NEUTRAL_GAME, POSTPONED)}
    with caplog.at_level(logging.ERROR):
        s = apply_overrides(ingested, None, path=write(tmp_path, [wrong, no_swap_flag_but_swapped, postponed]))
    reasons = {f["game_id"]: f["reason"] for f in s.failed}
    assert set(reasons) == {SWAP_GAME, NEUTRAL_GAME, POSTPONED} and not s.ok
    assert "home_team_id mismatch" in reasons[SWAP_GAME] and "season mismatch" in reasons[POSTPONED]
    assert {g: game(ingested, g) for g in before} == before
    assert "OVERRIDE_FAILURE: game_id=213466" in caplog.text


def test_site_override_on_a_game_without_a_result_keeps_it_unplayed(ingested, tmp_path):
    before = game(ingested, POSTPONED)
    s = apply_overrides(ingested, 2026, path=write(tmp_path, [site_entry(ingested, POSTPONED, swap_home_away=True)]))
    assert s.applied == [POSTPONED]
    after = game(ingested, POSTPONED)
    assert after["home_team_id"] == before["away_team_id"] and after["winner_team_id"] is None and after["alpha"] is None


def test_site_or_score_combine_with_exclude_in_either_order(ingested, tmp_path):
    for order in (("site", "exclude"), ("exclude", "site")):
        run.ingest_season(ingested, 2026, season_2026_api())
        entries = {"site": site_entry(ingested, SWAP_GAME, swap_home_away=True), "exclude": entry(ingested, SWAP_GAME, "exclude")}
        s = apply_overrides(ingested, 2026, path=write(tmp_path, [entries[k] for k in order]))
        assert s.ok and s.applied == [SWAP_GAME, SWAP_GAME]
        row = game(ingested, SWAP_GAME)
        assert (row["home_team_id"], row["is_rated"]) == (344, 0)
    # a site swap leaves the exclude entry's team ids valid in either orientation
    again = apply_overrides(ingested, 2026, path=write(tmp_path, [entries["site"], entries["exclude"]]))
    assert again.ok and again.applied == []


def test_overrides_are_restored_only_when_applied_after_a_reingest(ingested, tmp_path):
    """The ordering rule: a re-ingest overwrites corrections with the source, `apply_overrides` puts them back."""
    path = write(tmp_path, [
        site_entry(ingested, SWAP_GAME, swap_home_away=True),
        entry(ingested, NEUTRAL_GAME, "site", neutral_site=True),
        entry(ingested, HOME_WIN, home_score=50, away_score=60),
        entry(ingested, NEUTRAL, "exclude"),
    ])
    ids = (SWAP_GAME, NEUTRAL_GAME, HOME_WIN, NEUTRAL)
    source = {g: game(ingested, g) for g in ids}
    apply_overrides(ingested, 2026, path=path)
    corrected = {g: game(ingested, g) for g in ids}
    assert all(corrected[g] != source[g] for g in ids)

    run.ingest_season(ingested, 2026, season_2026_api())  # the weekly full-season ingest
    assert {g: game(ingested, g) for g in ids} == source  # reverted: ingest alone is not enough

    s = apply_overrides(ingested, 2026, path=path)  # ... so overrides run after it
    assert sorted(s.applied) == sorted(ids) and s.ok
    assert {g: game(ingested, g) for g in ids} == corrected


def test_a_game_neutralized_as_absent_fails_a_score_entry_but_not_exclude_or_site(store, tmp_path, caplog):
    run.ingest_season(store, 2026, season_2026_api())
    from tests.fakes import FakeApi, load
    api = FakeApi({2026: load("cbbd_teams_2026.json")}, {2026: [g for g in load("cbbd_games_2026.json") if g["id"] != HOME_WIN]})
    run.ingest_season(store, 2026, api)  # the source dropped the game: it is now cancelled and unrated
    assert (game(store, HOME_WIN)["status"], game(store, HOME_WIN)["is_rated"]) == ("cancelled", 0)
    path = write(tmp_path, [entry(store, HOME_WIN, "score")])
    with caplog.at_level(logging.ERROR):
        summary = apply_overrides(store, 2026, path=path)
    assert [f["game_id"] for f in summary.failed] == [HOME_WIN] and "only a final game can be re-scored" in summary.failed[0]["reason"]
    others = [entry(store, HOME_WIN, "exclude"), entry(store, HOME_WIN, "site", neutral_site=False)]
    assert apply_overrides(store, 2026, path=write(tmp_path, [others[0]])).ok
    assert apply_overrides(store, 2026, path=write(tmp_path, [others[1]])).ok
