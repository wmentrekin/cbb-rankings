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
