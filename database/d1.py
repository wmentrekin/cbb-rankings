"""Cloudflare D1 storage client over the REST API.

Endpoints (https://developers.cloudflare.com/api/resources/d1/subresources/database/):
  POST /accounts/{a}/d1/database/{d}/query   rows as dicts; `{"batch": [...]}` is atomic
  POST /accounts/{a}/d1/database/{d}/raw     rows as arrays, for bulk reads

Limits this client is built around: 30 s per request (so writes are chunked, see
store.DEFAULT_CHUNK_ROWS), 1,200 API requests per 5 minutes per user, and a 2 MB request body
(see store.DEFAULT_CHUNK_BYTES).

The Authorization header is always sent as `Bearer <CLOUDFLARE_API_TOKEN>`. In the cloud
session a proxy overwrites it, so the variable may hold a placeholder. The token is never logged
or included in errors.
"""
from __future__ import annotations

import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import requests

from database.store import Store, WriteStats

log = logging.getLogger(__name__)

API_BASE = "https://api.cloudflare.com/client/v4"
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
MAX_RETRIES = 5  # retries after the first attempt
BACKOFF_BASE = 1.0  # seconds; full jitter over base * 2**attempt
BACKOFF_CAP = 30.0
RETRY_AFTER_MAX = 60.0

CODE_RATE_LIMITED = 7429
CODE_INTERNAL = 7500

# A 7500 is D1's generic error code: it carries SQL errors (constraint, syntax) as well as
# transient overload. Retry only when the message says overload/transient, and never when it
# carries an SQLite error.
_TRANSIENT_RE = re.compile(
    r"overload|too many requests|queued|network connection lost|temporarily|try again|"
    r"timed out|timeout|reset because|object to be reset|internal error",
    re.IGNORECASE,
)
_SQL_ERROR_RE = re.compile(r"SQLITE_|constraint|syntax|no such (table|column)|near \"", re.IGNORECASE)


class D1Error(RuntimeError):
    """A failed D1 request. `code` is the API error code (or None); `status` the HTTP status."""

    def __init__(self, message: str, *, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class D1Config:
    account_id: str
    database_id: str
    api_token: str = field(repr=False)

    @classmethod
    def from_env(cls) -> "D1Config":
        names = ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_D1_DATABASE_ID", "CLOUDFLARE_API_TOKEN")
        missing = [n for n in names if not os.environ.get(n)]
        if missing:
            raise D1Error(f"missing environment variables: {', '.join(missing)}")
        return cls(*(os.environ[n] for n in names))


def is_transient_error(code: int | None, message: str) -> bool:
    """Whether a D1 error body describes a condition worth retrying."""
    if code == CODE_RATE_LIMITED:
        return True
    if code == CODE_INTERNAL:
        return bool(_TRANSIENT_RE.search(message)) and not _SQL_ERROR_RE.search(message)
    return False


class D1Client(Store):
    def __init__(
        self,
        config: D1Config,
        *,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_retries: int = MAX_RETRIES,
    ):
        """`session` is anything with requests.Session's `post(url, json=, headers=, timeout=)`."""
        self._config = config
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._jitter = jitter
        self._max_retries = max_retries
        self._base = f"{API_BASE}/accounts/{config.account_id}/d1/database/{config.database_id}"

    @classmethod
    def from_env(cls, **kwargs) -> "D1Client":
        return cls(D1Config.from_env(), **kwargs)

    # -- reads -------------------------------------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        result = self._request("query", {"sql": sql, "params": list(params)})
        return result[0].get("results") or []

    def query_rows(self, sql: str, params: Sequence[Any] = ()) -> tuple[list[str], list[list]]:
        result = self._request("raw", {"sql": sql, "params": list(params)})
        results = result[0].get("results") or {}
        return list(results.get("columns") or []), list(results.get("rows") or [])

    # -- writes ------------------------------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> WriteStats:
        result = self._request("query", {"sql": sql, "params": list(params)})
        return _stats(result)

    def execute_batch(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> WriteStats:
        """Atomic: D1 rolls back the whole batch if one statement fails.

        Not retry-safe for non-idempotent statements: a read timeout after the batch committed
        would be retried and applied twice. Prefer idempotent (upsert/DELETE) statements.
        """
        if not statements:
            return WriteStats()
        body = {"batch": [{"sql": sql, "params": list(params)} for sql, params in statements]}
        return _stats(self._request("query", body))

    # -- transport ---------------------------------------------------------------------------

    def _request(self, path: str, body: dict) -> list[dict]:
        url = f"{self._base}/{path}"
        headers = {
            "Authorization": f"Bearer {self._config.api_token}",
            "Content-Type": "application/json",
        }
        attempt = 0
        while True:
            retry_after = None
            try:
                resp = self._session.post(
                    url, json=body, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
                )
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
                failure: D1Error = D1Error(f"D1 request failed: {type(exc).__name__}")
                retryable = True
            else:
                ok, failure, retryable = self._interpret(resp)
                if ok is not None:
                    return ok
                retry_after = _retry_after(resp)
            if not retryable or attempt >= self._max_retries:
                raise failure
            delay = self._jitter(0, min(BACKOFF_CAP, BACKOFF_BASE * 2**attempt))
            if retry_after is not None:
                delay = retry_after
            log.warning("D1 %s failed (%s); retry %d/%d in %.1fs", path, failure, attempt + 1, self._max_retries, delay)
            self._sleep(delay)
            attempt += 1

    @staticmethod
    def _interpret(resp: Any) -> tuple[list[dict] | None, D1Error | None, bool]:
        """Return (result, None, False) on success, else (None, error, retryable)."""
        status = resp.status_code
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and payload.get("success") is True and status < 300:
            result = payload.get("result")
            if isinstance(result, list) and result:
                return result, None, False
            return None, D1Error("D1 response had no result", status=status), False
        if errors:
            code = errors[0].get("code")
            message = "; ".join(str(e.get("message", "")) for e in errors)
            err = D1Error(f"D1 error {code}: {message}", code=code, status=status)
            if code in (CODE_RATE_LIMITED, CODE_INTERNAL):
                return None, err, is_transient_error(code, message)
            return None, err, status == 429 or status >= 500
        err = D1Error(f"D1 HTTP {status}", status=status)
        return None, err, status == 429 or status >= 500


def _retry_after(resp: Any) -> float | None:
    value = (getattr(resp, "headers", None) or {}).get("Retry-After")
    try:
        return min(max(float(value), 0.0), RETRY_AFTER_MAX) if value is not None else None
    except ValueError:
        return None


def _stats(result: list[dict]) -> WriteStats:
    written = read = 0
    for item in result:
        meta = item.get("meta") or {}
        written += int(meta.get("rows_written") or 0)
        read += int(meta.get("rows_read") or 0)
    return WriteStats(rows_written=written, rows_read=read)
