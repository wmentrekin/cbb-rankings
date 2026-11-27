import sqlite3

import pytest

from database.local import LocalStore, MIGRATIONS_DIR
from database.store import WriteStats

TABLES = {"seasons", "teams", "games", "ratings", "ratings_staging", "d1_migrations"}


def game(i, **over):
    row = dict(
        id=i, source_id=str(i), season=1900, week=1, game_date="1900-01-01",
        start_date="1900-01-01T05:00:00.000Z", season_type="regular", tournament=None,
        game_type="STD", game_notes=None, is_rated=1, status="final", home_team_id=1,
        home_team="A", home_score=70, away_team_id=2, away_team="B", away_score=60,
        neutral_site=0, conference_game=1, periods=2, margin=10, winner_team_id=1, alpha=1.5,
    )
    row.update(over)
    return row


def test_migrations_apply_and_are_tracked(store):
    names = {r["name"] for r in store.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names
    applied = [r["name"] for r in store.query("SELECT name FROM d1_migrations ORDER BY id")]
    assert applied == sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    assert store.apply_migrations() == []  # idempotent
    assert len(store.query("SELECT * FROM d1_migrations")) == len(applied)


def test_migrations_persist_across_opens(tmp_path):
    path = str(tmp_path / "x.db")
    LocalStore(path).close()
    again = LocalStore(path)
    assert again.apply_migrations() == []


def test_failed_migration_rolls_back(tmp_path):
    (tmp_path / "0001_bad.sql").write_text("CREATE TABLE t(a INTEGER) STRICT; SELECT * FROM nope;")
    with pytest.raises(sqlite3.Error):
        LocalStore(":memory:", migrations_dir=tmp_path)


def test_check_constraints(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert("games", [game(1, status="weird")], ("id",))
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert("games", [game(1, is_rated=2)], ("id",))


def test_types_round_trip(store):
    store.upsert("games", [game(1, tournament="NIT", alpha=None, margin=-3, home_score=0)], ("id",))
    (row,) = store.query("SELECT * FROM games")
    assert row["id"] == 1 and type(row["id"]) is int
    assert row["home_score"] == 0 and type(row["home_score"]) is int
    assert row["margin"] == -3
    assert row["alpha"] is None
    assert row["game_notes"] is None
    assert row["tournament"] == "NIT"
    assert row["source_id"] == "1" and type(row["source_id"]) is str
    types = store.query("SELECT typeof(id) a, typeof(is_rated) b, typeof(alpha) c FROM games")[0]
    assert types == {"a": "integer", "b": "integer", "c": "null"}


def test_real_and_unicode_round_trip(store):
    store.upsert("ratings", [dict(team_id=1, season=1900, week=1, rating=12.5, wins=3, losses=0)],
                 ("team_id", "season", "week"))
    store.upsert("games", [game(2, home_team="Hawaiʻi “quoted”,")], ("id",))
    assert store.query("SELECT typeof(rating) t, rating FROM ratings") == [{"t": "real", "rating": 12.5}]
    assert store.query("SELECT home_team FROM games")[0]["home_team"] == "Hawaiʻi “quoted”,"


def test_noop_upsert_writes_nothing_and_change_updates(store):
    rows = [game(i) for i in range(1, 11)]
    assert store.upsert("games", rows, ("id",)).rows_written == 10
    assert store.upsert("games", rows, ("id",)).rows_written == 0
    rows[3] = game(4, home_score=99, margin=39)
    assert store.upsert("games", rows, ("id",)).rows_written == 1
    assert store.query("SELECT home_score FROM games WHERE id = 4") == [{"home_score": 99}]
    # NULL <-> value changes are detected by IS NOT
    rows[3] = game(4, home_score=None, margin=None)
    assert store.upsert("games", rows, ("id",)).rows_written == 1
    assert store.upsert("games", rows, ("id",)).rows_written == 0


def test_composite_key(store):
    def r(team, week, rating):
        return dict(team_id=team, season=1900, week=week, rating=rating, wins=1, losses=0)

    key = ("team_id", "season", "week")
    store.upsert("ratings", [r(1, 1, 1.0), r(1, 2, 2.0), r(2, 1, 3.0)], key)
    assert store.upsert("ratings", [r(1, 2, 2.5)], key).rows_written == 1
    assert store.query("SELECT COUNT(*) n FROM ratings")[0]["n"] == 3
    assert store.query("SELECT rating FROM ratings WHERE team_id=1 AND week=2")[0]["rating"] == 2.5


def test_key_only_upsert_is_do_nothing(store):
    store.execute("CREATE TABLE k(a INTEGER PRIMARY KEY, b TEXT) STRICT")
    assert store.upsert("k", [dict(a=1), dict(a=2)], ("a",)).rows_written == 2
    assert store.upsert("k", [dict(a=1), dict(a=3)], ("a",)).rows_written == 1


def test_chunked_upsert_writes_all_rows(store):
    rows = [game(i) for i in range(1, 251)]
    assert store.upsert("games", rows, ("id",), chunk_rows=40).rows_written == 250
    assert store.upsert("games", rows, ("id",), chunk_rows=40, chunk_bytes=3000).rows_written == 0
    assert store.query("SELECT COUNT(*) n FROM games")[0]["n"] == 250


def test_upsert_empty_and_bad_rows(store):
    assert store.upsert("games", [], ("id",)) == WriteStats()
    with pytest.raises(ValueError):
        store.upsert("games", [game(1), {"id": 2}], ("id",))


def test_execute_batch_is_atomic(store):
    good = ("INSERT INTO seasons(season, anchor_monday, selection_sunday) VALUES (?, 'a', 'b')", [1])
    bad = ("INSERT INTO nope VALUES (1)", [])
    with pytest.raises(sqlite3.Error):
        store.execute_batch([good, bad])
    assert store.query("SELECT COUNT(*) n FROM seasons")[0]["n"] == 0
    assert store.execute_batch([good]).rows_written == 1


def test_query_rows_shape(store):
    store.upsert("games", [game(1), game(2)], ("id",))
    columns, rows = store.query_rows("SELECT id, home_team FROM games ORDER BY id")
    assert columns == ["id", "home_team"]
    assert rows == [[1, "A"], [2, "A"]]
    assert store.query_rows("SELECT id FROM games WHERE id = ?", [2]) == (["id"], [[2]])


def test_delete_via_execute(store):
    store.upsert("games", [game(1), game(2)], ("id",))
    assert store.execute("DELETE FROM games WHERE id >= ?", [2]).rows_written > 0
    assert store.query("SELECT id FROM games") == [{"id": 1}]
