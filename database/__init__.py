"""Storage: Cloudflare D1 over REST in production, sqlite3 locally. Use `get_store()`."""
from __future__ import annotations

import os

from database.store import Store, WriteStats

__all__ = ["Store", "WriteStats", "get_store"]


def get_store() -> Store:
    """Pick the store from CBB_STORE: `d1` (default) or `local` (path in CBB_LOCAL_DB)."""
    kind = os.environ.get("CBB_STORE", "d1").strip().lower()
    if kind == "d1":
        from database.d1 import D1Client

        return D1Client.from_env()
    if kind == "local":
        from database.local import LocalStore

        return LocalStore(os.environ.get("CBB_LOCAL_DB", "local.db"))
    raise ValueError(f"CBB_STORE must be 'd1' or 'local', got {kind!r}")
