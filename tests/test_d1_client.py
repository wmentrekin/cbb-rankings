import pytest
import requests

from database.d1 import D1Client, D1Config, D1Error, is_transient_error
from database.store import WriteStats


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def ok(results, rows_written=0, rows_read=0):
    return FakeResponse(200, {"success": True, "errors": [], "result": [
        {"success": True, "results": results, "meta": {"rows_written": rows_written, "rows_read": rows_read}}]})


def err(status, code, message):
    return FakeResponse(status, {"success": False, "errors": [{"code": code, "message": message}], "result": None})


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(dict(url=url, json=json, headers=headers, timeout=timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(*responses, **kw):
    sleeps = []
    session = FakeSession(*responses)
    c = D1Client(D1Config("acct", "db", "tok"), session=session, sleep=sleeps.append,
                 jitter=lambda lo, hi: hi, **kw)
    return c, session, sleeps


def test_query_request_and_parsing():
    c, s, _ = client(ok([{"a": 1}, {"a": 2}]))
    assert c.query("SELECT a FROM t WHERE x = ?", [5]) == [{"a": 1}, {"a": 2}]
    call = s.calls[0]
    assert call["url"] == "https://api.cloudflare.com/client/v4/accounts/acct/d1/database/db/query"
    assert call["json"] == {"sql": "SELECT a FROM t WHERE x = ?", "params": [5]}
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["timeout"] == (10, 60)


def test_query_rows_raw_parsing():
    raw = FakeResponse(200, {"success": True, "errors": [], "result": [
        {"success": True, "results": {"columns": ["a", "b"], "rows": [[1, "x"], [2, None]]}, "meta": {}}]})
    c, s, _ = client(raw)
    assert c.query_rows("SELECT a, b FROM t") == (["a", "b"], [[1, "x"], [2, None]])
    assert s.calls[0]["url"].endswith("/db/raw")


def test_execute_returns_meta_totals_and_batch_body():
    resp = FakeResponse(200, {"success": True, "errors": [], "result": [
        {"success": True, "results": [], "meta": {"rows_written": 3, "rows_read": 4}},
        {"success": True, "results": [], "meta": {"rows_written": 2, "rows_read": 1}}]})
    c, s, _ = client(resp)
    assert c.execute_batch([("A", [1]), ("B", [])]) == WriteStats(5, 5)
    assert s.calls[0]["json"] == {"batch": [{"sql": "A", "params": [1]}, {"sql": "B", "params": []}]}
    assert c.execute_batch([]) == WriteStats()


def test_upsert_sends_one_json_param_per_chunk():
    c, s, _ = client(ok([], 2), ok([], 1))
    rows = [{"id": i, "v": "a"} for i in range(3)]
    stats = c.upsert("t", rows, ("id",), chunk_rows=2)
    assert stats.rows_written == 3
    assert [len(call["json"]["params"]) for call in s.calls] == [1, 1]
    assert "json_each(?1)" in s.calls[0]["json"]["sql"]
    assert isinstance(s.calls[0]["json"]["params"][0], str)


def test_retry_on_429_then_success_honors_retry_after_cap():
    c, s, sleeps = client(FakeResponse(429, None, {"Retry-After": "3"}),
                          FakeResponse(429, None, {"Retry-After": "500"}), ok([{"a": 1}]))
    assert c.query("SELECT 1") == [{"a": 1}]
    assert sleeps == [3.0, 60.0]
    assert len(s.calls) == 3


def test_retry_on_5xx_and_connection_errors_with_backoff():
    c, s, sleeps = client(FakeResponse(502, None), requests.ConnectionError("boom"),
                          requests.ReadTimeout("slow"), ok([]))
    c.query("SELECT 1")
    assert sleeps == [1.0, 2.0, 4.0]  # jitter fixed to the upper bound: base * 2**attempt


def test_transient_7429_retries():
    c, _, sleeps = client(err(429, 7429, "Too many requests"), ok([]))
    c.query("SELECT 1")
    assert len(sleeps) == 1


def test_transient_7500_retries():
    c, _, sleeps = client(err(500, 7500, "D1 DB is overloaded. Requests queued for too long."), ok([]))
    c.query("SELECT 1")
    assert len(sleeps) == 1


@pytest.mark.parametrize("message", [
    "UNIQUE constraint failed: games.id: SQLITE_CONSTRAINT",
    "near \"SELEC\": syntax error: SQLITE_ERROR",
    "no such table: nope: SQLITE_ERROR",
])
@pytest.mark.parametrize("status", [400, 500])
def test_sql_error_7500_raises_immediately(message, status):
    c, s, sleeps = client(err(status, 7500, message), ok([]))
    with pytest.raises(D1Error) as exc:
        c.query("SELECT 1")
    assert exc.value.code == 7500 and message in str(exc.value)
    assert len(s.calls) == 1 and sleeps == []


def test_other_4xx_raises_without_retry():
    c, s, _ = client(err(403, 10000, "Authentication error"))
    with pytest.raises(D1Error) as exc:
        c.query("SELECT 1")
    assert exc.value.code == 10000 and exc.value.status == 403
    assert len(s.calls) == 1


def test_exhausted_retries_raise_d1error():
    c, s, sleeps = client(*[err(429, 7429, "rate limited")] * 3, max_retries=2)
    with pytest.raises(D1Error) as exc:
        c.query("SELECT 1")
    assert exc.value.code == 7429
    assert len(s.calls) == 3 and len(sleeps) == 2


def test_exhausted_connection_errors_raise_d1error():
    c, _, _ = client(*[requests.ConnectionError("x")] * 2, max_retries=1)
    with pytest.raises(D1Error):
        c.query("SELECT 1")


def test_token_not_in_errors_or_repr():
    c, _, _ = client(err(403, 10000, "nope"))
    assert "tok" not in repr(c._config)
    with pytest.raises(D1Error) as exc:
        c.query("SELECT 1")
    assert "tok" not in str(exc.value)


def test_is_transient_error():
    assert is_transient_error(7429, "anything")
    assert is_transient_error(7500, "Network connection lost")
    assert not is_transient_error(7500, "SQLITE_ERROR: Network connection lost? no")
    assert not is_transient_error(1234, "overloaded")


def test_config_from_env(monkeypatch):
    for n in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_D1_DATABASE_ID", "CLOUDFLARE_API_TOKEN"):
        monkeypatch.delenv(n, raising=False)
    with pytest.raises(D1Error, match="CLOUDFLARE_API_TOKEN"):
        D1Config.from_env()
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_D1_DATABASE_ID", "d")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "proxy-injected")
    assert D1Config.from_env().database_id == "d"


# --- retried writes ---------------------------------------------------------------------------
def test_write_retried_after_a_timeout_is_flagged_in_the_stats():
    c, _, _ = client(requests.ReadTimeout("slow"), requests.ConnectionError("reset"), ok([], rows_written=0))
    stats = c.execute("UPDATE t SET a = 1")
    assert stats == WriteStats(rows_written=0, rows_read=0, retried_writes=2)  # the first attempt may have committed


def test_status_retries_and_clean_writes_are_not_flagged():
    c, _, _ = client(err(429, 7429, "Too many requests"), ok([], rows_written=3))
    assert c.execute("UPDATE t SET a = 1").retried_writes == 0
    c, _, _ = client(ok([], rows_written=3))
    assert c.execute_batch([("A", [])]).retried_writes == 0


def test_retried_writes_add_up_across_upsert_chunks():
    c, _, _ = client(requests.ConnectionError("x"), ok([], 2), ok([], 1))
    stats = c.upsert("t", [{"id": i, "v": "a"} for i in range(3)], ("id",), chunk_rows=2)
    assert (stats.rows_written, stats.retried_writes) == (3, 1)


def test_reads_do_not_report_anything_extra():
    c, _, _ = client(requests.ConnectionError("x"), ok([{"a": 1}]))
    assert c.query("SELECT 1") == [{"a": 1}]
