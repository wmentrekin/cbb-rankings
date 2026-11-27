import pytest
import requests

from ingest import http
from ingest.http import CbbdClient


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else []
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make(script, key="k"):
    sleeps = []
    session = FakeSession(script)
    client = CbbdClient(key, session=session, sleep=sleeps.append)
    return client, session, sleeps


def test_sends_bearer_header_timeouts_and_counts():
    client, session, sleeps = make([FakeResponse(200, [1], {"X-CallLimit-Remaining": "972"})])
    assert client.get("/teams", {"season": 2026}) == [1]
    req = session.requests[0]
    assert req["url"] == "https://api.collegebasketballdata.com/teams"
    assert req["headers"]["Authorization"] == "Bearer k"
    assert req["params"] == {"season": 2026}
    assert req["timeout"] == (10, 60)
    assert client.calls == 1 and client.quota_remaining == 972 and sleeps == []


def test_key_from_env_and_missing(monkeypatch):
    monkeypatch.setenv("CBBD_API_KEY", "proxy-injected")
    assert CbbdClient(session=FakeSession([])) is not None
    monkeypatch.delenv("CBBD_API_KEY")
    with pytest.raises(RuntimeError, match="CBBD_API_KEY"):
        CbbdClient(session=FakeSession([]))


def test_retries_statuses_with_backoff_then_succeeds():
    client, _, sleeps = make([FakeResponse(502), FakeResponse(503), FakeResponse(429), FakeResponse(200, ["ok"])])
    assert client.get("/x") == ["ok"]
    assert client.calls == 4  # every attempt counts against the quota
    assert sleeps == [0.0, 4.0, 8.0]


def test_retry_after_is_honoured_and_capped():
    client, _, sleeps = make(
        [FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(503, headers={"Retry-After": "3600"}), FakeResponse(200)]
    )
    client.get("/x")
    assert sleeps == [7.0, 60.0]


def test_gives_up_after_four_retries_and_raises():
    client, _, sleeps = make([FakeResponse(500)] * 5)
    with pytest.raises(requests.HTTPError):
        client.get("/x")
    assert client.calls == 5 and sleeps == [0.0, 4.0, 8.0, 16.0]


def test_connection_errors_retry_then_raise():
    client, _, _ = make([requests.ConnectionError("x"), requests.Timeout("y"), FakeResponse(200, [1])])
    assert client.get("/x") == [1]
    client, _, _ = make([requests.ConnectionError("x")] * 5)
    with pytest.raises(requests.ConnectionError):
        client.get("/x")
    assert client.calls == 5


@pytest.mark.parametrize("status", [400, 401, 404])
def test_deterministic_errors_fail_immediately(status):
    client, _, sleeps = make([FakeResponse(status)])
    with pytest.raises(requests.HTTPError):
        client.get("/x")
    assert client.calls == 1 and sleeps == []


def test_key_is_never_logged(caplog):
    caplog.set_level("DEBUG")
    client, _, _ = make([FakeResponse(502), FakeResponse(200)], key="super-secret-key")
    client.get("/x")
    assert "super-secret-key" not in caplog.text


def test_constants_match_football():
    assert (http.CONNECT_TIMEOUT, http.READ_TIMEOUT, http.RETRY_TOTAL, http.RETRY_AFTER_MAX) == (10, 60, 4, 60)


# --- 429 handling ---------------------------------------------------------------------------
def test_a_second_429_without_retry_after_fails_fast():
    client, _, sleeps = make([FakeResponse(429), FakeResponse(429), FakeResponse(200, ["never"])])
    with pytest.raises(requests.HTTPError):
        client.get("/x")
    assert client.calls == 2 and sleeps == [0.0]  # one retry, then stop: a spent quota is not retried


def test_429_with_retry_after_keeps_retrying_up_to_the_normal_limit():
    client, _, sleeps = make([FakeResponse(429, headers={"Retry-After": "2"})] * 4 + [FakeResponse(200, ["ok"])])
    assert client.get("/x") == ["ok"]
    assert client.calls == 5 and sleeps == [2.0] * 4


def test_429_then_other_errors_still_use_their_own_budget():
    client, _, _ = make([FakeResponse(429), FakeResponse(503), FakeResponse(200, [1])])
    assert client.get("/x") == [1] and client.calls == 3


# --- quota floor ----------------------------------------------------------------------------
def remaining(n, body=None, status=200):
    return FakeResponse(status, body, {"X-CallLimit-Remaining": str(n)})


def test_no_further_call_is_made_once_remaining_is_below_the_floor():
    session = FakeSession([remaining(60, [1]), remaining(49, [2]), remaining(10, [3])])
    client = CbbdClient("k", session=session, sleep=lambda s: None)
    assert client.get("/a") == [1] and client.get("/b") == [2]  # 49 is below 50, but that call's data is kept
    with pytest.raises(http.QuotaFloorError, match="49 is below the floor 50"):
        client.get("/c")
    assert client.calls == 2 and len(session.requests) == 2


def test_remaining_equal_to_the_floor_is_allowed():
    client = CbbdClient("k", session=FakeSession([remaining(50, [1]), remaining(50, [2])]), sleep=lambda s: None)
    assert client.get("/a") == [1] and client.get("/b") == [2]


def test_floor_also_stops_retries():
    session = FakeSession([remaining(40, status=503), FakeResponse(200, [1])])
    client = CbbdClient("k", session=session, sleep=lambda s: None)
    with pytest.raises(http.QuotaFloorError):
        client.get("/a")
    assert client.calls == 1


def test_floor_from_env_argument_and_default(monkeypatch):
    monkeypatch.delenv("CBBD_QUOTA_FLOOR", raising=False)
    assert CbbdClient("k", session=FakeSession([])).quota_floor == 50
    monkeypatch.setenv("CBBD_QUOTA_FLOOR", "200")
    assert CbbdClient("k", session=FakeSession([])).quota_floor == 200
    assert CbbdClient("k", session=FakeSession([]), quota_floor=5).quota_floor == 5
    monkeypatch.setenv("CBBD_QUOTA_FLOOR", "lots")
    with pytest.raises(ValueError, match="CBBD_QUOTA_FLOOR"):
        CbbdClient("k", session=FakeSession([]))


def test_missing_quota_header_never_trips_the_floor():
    client = CbbdClient("k", session=FakeSession([FakeResponse(200, [1]), FakeResponse(200, [2])]), sleep=lambda s: None)
    assert client.get("/a") == [1] and client.get("/b") == [2] and client.quota_remaining is None


def test_missing_or_unreadable_quota_header_is_flagged_once_without_failing(caplog):
    session = FakeSession([FakeResponse(200, [1]), FakeResponse(200, [2], {"X-CallLimit-Remaining": "n/a"})])
    client = CbbdClient("k", session=session, sleep=lambda s: None)
    assert not client.quota_header_missing
    with caplog.at_level("WARNING", logger="ingest.http"):
        assert client.get("/a") == [1] and client.get("/b") == [2]
    assert client.quota_header_missing and client.quota_remaining is None
    assert len([r for r in caplog.records if "quota floor cannot be enforced" in r.message]) == 1


def test_a_readable_header_is_not_flagged():
    client = CbbdClient("k", session=FakeSession([remaining(900, [1])]), sleep=lambda s: None)
    client.get("/a")
    assert not client.quota_header_missing
