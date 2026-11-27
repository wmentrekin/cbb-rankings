"""Retrying GET helper for the CollegeBasketballData API, with a call counter.

Modeled on the football pipeline's `cfbd_http.py` (same timeouts, retry count, statuses and
Retry-After cap) but with an explicit retry loop so that every HTTP request sent, retries
included, is counted. The monthly quota (about 1,000 calls) is shared with the football
pipeline, so callers read `client.calls` and report it.

Only idempotent GETs are retried, and only for failures that say "try again": connection
errors, timeouts and 429/500/502/503/504. Any other error status fails on the first attempt.
After the retries are used up the last failure is raised (`requests.HTTPError` for a status,
the original exception for a network failure). A 429 is retried only once unless the response
carries a Retry-After header: a spent monthly quota should fail fast, not burn more calls.

Quota floor: `X-CallLimit-Remaining` is read after every response. Once it is below the floor
(env `CBBD_QUOTA_FLOOR`, default 50) no further request is sent and `QuotaFloorError` is raised,
because the quota is shared with a live football pipeline. A response without a readable header
sets `quota_header_missing` (logged once); the floor cannot protect the quota while it is
unknown, so the ingest summary carries a warning, but the run does not fail.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

import requests

BASE_URL = "https://api.collegebasketballdata.com"
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
RETRY_TOTAL = 4  # retries after the first attempt: up to 5 requests per call
RETRY_BACKOFF = 2.0  # sleeps 0, 4, 8, 16 s before retries 1-4, like urllib3's backoff_factor=2
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_AFTER_MAX = 60  # never sleep longer than this for a Retry-After header
QUOTA_HEADER = "X-CallLimit-Remaining"
QUOTA_FLOOR_ENV = "CBBD_QUOTA_FLOOR"
DEFAULT_QUOTA_FLOOR = 50

log = logging.getLogger(__name__)


class QuotaFloorError(RuntimeError):
    """The remaining API quota is below the floor, so no further call is made."""


def quota_floor_from_env() -> int:
    raw = os.environ.get(QUOTA_FLOOR_ENV, "").strip()
    if not raw:
        return DEFAULT_QUOTA_FLOOR
    if not raw.isdigit():
        raise ValueError(f"{QUOTA_FLOOR_ENV} must be a non-negative integer, got {raw!r}")
    return int(raw)


class CbbdClient:
    """GET JSON from the API. `calls` counts every HTTP request sent, retries included."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = BASE_URL,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        quota_floor: int | None = None,
    ):
        # In cloud agent sessions the key variable may hold the placeholder "proxy-injected"
        # and a proxy supplies the real header; it is sent like any other key.
        key = api_key if api_key is not None else os.environ.get("CBBD_API_KEY", "")
        if not key:
            raise RuntimeError("CBBD_API_KEY is not set")
        self._headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        self._base_url = base_url.rstrip("/")
        self._session = session or requests.Session()
        self._sleep = sleep
        self.quota_floor = quota_floor_from_env() if quota_floor is None else quota_floor
        self.calls = 0
        self.quota_remaining: int | None = None  # last X-CallLimit-Remaining seen
        self.quota_header_missing = False  # some response had no readable X-CallLimit-Remaining

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET `path` and return the decoded JSON body."""
        url = f"{self._base_url}{path}"
        rate_limited = 0
        for attempt in range(RETRY_TOTAL + 1):
            retry_after = None
            self._check_quota(path)
            try:
                self.calls += 1
                response = self._session.get(
                    url,
                    headers=self._headers,
                    params=params,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == RETRY_TOTAL:
                    raise
                log.warning("GET %s failed (%s); retry %d/%d", path, type(exc).__name__, attempt + 1, RETRY_TOTAL)
            else:
                self._note_quota(response)
                retry_after = _retry_after(response)
                if response.status_code == 429:
                    rate_limited += 1
                # A second 429 without Retry-After is a spent quota, not a blip: stop here.
                spent = response.status_code == 429 and retry_after is None and rate_limited > 1
                if response.status_code not in RETRY_STATUSES or attempt == RETRY_TOTAL or spent:
                    response.raise_for_status()
                    return response.json()
                log.warning("GET %s returned %d; retry %d/%d", path, response.status_code, attempt + 1, RETRY_TOTAL)
            self._sleep(retry_after if retry_after is not None else _backoff(attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    def _check_quota(self, path: str) -> None:
        if self.quota_remaining is not None and self.quota_remaining < self.quota_floor:
            raise QuotaFloorError(
                f"CBBD quota remaining {self.quota_remaining} is below the floor {self.quota_floor} "
                f"({QUOTA_FLOOR_ENV}); not calling {path}. The quota is shared with the football pipeline."
            )

    def _note_quota(self, response: requests.Response) -> None:
        value = response.headers.get(QUOTA_HEADER)
        if value is not None and value.strip().isdigit():
            self.quota_remaining = int(value)
        elif not self.quota_header_missing:
            self.quota_header_missing = True
            log.warning("%s is missing or unreadable (%r): the quota floor cannot be enforced", QUOTA_HEADER, value)


def _backoff(attempt: int) -> float:
    """Sleep before retry number attempt+1: 0, 4, 8, 16 seconds."""
    return 0.0 if attempt == 0 else RETRY_BACKOFF * 2**attempt


def _retry_after(response: requests.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return min(max(float(raw), 0.0), RETRY_AFTER_MAX)
    except ValueError:
        return None  # an HTTP-date form: fall back to the backoff schedule
