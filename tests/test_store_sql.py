import json

import pytest

from database.store import build_upsert_sql, check_identifier, chunk_json


def test_upsert_sql_shape():
    sql = build_upsert_sql("t", ["id", "a", "b"], ["id"])
    assert sql == (
        "INSERT INTO t(id, a, b) SELECT value->>'$.id', value->>'$.a', value->>'$.b' "
        "FROM json_each(?1) WHERE true ON CONFLICT(id) DO UPDATE SET a = excluded.a, b = excluded.b "
        "WHERE t.a IS NOT excluded.a OR t.b IS NOT excluded.b"
    )


def test_upsert_sql_composite_key_and_key_only():
    sql = build_upsert_sql("r", ["x", "y", "v"], ["x", "y"])
    assert "ON CONFLICT(x, y) DO UPDATE SET v = excluded.v WHERE r.v IS NOT excluded.v" in sql
    assert build_upsert_sql("r", ["x"], ["x"]).endswith("ON CONFLICT(x) DO NOTHING")


@pytest.mark.parametrize("name", ["a b", "1a", "a;drop", "a-b", "", "a'", "t.c", 'a"b', None])
def test_identifier_rejected(name):
    with pytest.raises(ValueError):
        check_identifier(name)


def test_identifier_accepted():
    assert check_identifier("_a1_B") == "_a1_B"


def test_build_rejects_bad_names_and_keys():
    with pytest.raises(ValueError):
        build_upsert_sql("t; DROP TABLE x", ["a"], ["a"])
    with pytest.raises(ValueError):
        build_upsert_sql("t", ["a", "b c"], ["a"])
    with pytest.raises(ValueError):
        build_upsert_sql("t", ["a"], ["zz"])
    with pytest.raises(ValueError):
        build_upsert_sql("t", ["a"], [])


def test_chunk_by_rows():
    rows = [{"i": i} for i in range(11)]
    chunks = list(chunk_json(rows, max_rows=5))
    assert [c[1] for c in chunks] == [5, 5, 1]
    assert [r for payload, _ in chunks for r in json.loads(payload)] == rows


def test_chunk_by_bytes():
    rows = [{"v": "x" * 100} for _ in range(10)]  # ~110 bytes each
    chunks = list(chunk_json(rows, max_rows=500, max_bytes=400))
    assert len(chunks) > 1
    assert all(len(p.encode()) <= 400 for p, _ in chunks)
    assert sum(n for _, n in chunks) == 10


def test_chunk_counts_multibyte_as_bytes():
    rows = [{"v": "é" * 100} for _ in range(4)]
    for payload, _ in chunk_json(rows, max_bytes=500):
        assert len(payload.encode("utf-8")) <= 500


def test_chunk_single_oversized_row_raises():
    with pytest.raises(ValueError):
        list(chunk_json([{"v": "x" * 1000}], max_bytes=100))


def test_chunk_rejects_nan_and_empty():
    with pytest.raises(ValueError):
        list(chunk_json([{"v": float("nan")}]))
    assert list(chunk_json([])) == []
