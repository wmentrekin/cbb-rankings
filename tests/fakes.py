"""Test doubles and fixture loaders. The JSON fixtures are hand-trimmed copies of REAL CBBD
responses saved by the source probe (docs/source-probe.md): the same field names and values,
minus fields the ingest does not read (Elo, venue, attendance, ...) and with each team's `logos`
list cut to the two 500 px URLs. Ids, dates, scores and statuses are untouched."""
from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

from ingest.games import GAMES_CAP

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


class FakeApi:
    """Stands in for `CbbdClient`: serves /teams and /games like the real API (inclusive date
    filters, oldest first, at most `cap` rows) and counts calls."""

    def __init__(self, teams: dict[int, list], games: dict[int, list], cap: int = GAMES_CAP):
        self.teams, self.games, self.cap = teams, games, cap
        self.calls = 0
        self.quota_remaining = 900
        self.requests: list[tuple[str, dict]] = []

    def get(self, path, params=None):
        self.calls += 1
        self.quota_remaining -= 1
        self.requests.append((path, dict(params or {})))
        season = params["season"]
        if path == "/teams":
            return copy.deepcopy(self.teams[season])
        assert path == "/games", path
        lo = datetime.fromisoformat(params["startDateRange"])
        hi = datetime.fromisoformat(params["endDateRange"])
        rows = [g for g in self.games[season] if lo <= datetime.fromisoformat(g["startDate"]) <= hi]
        rows.sort(key=lambda g: (g["startDate"], g["id"]))
        return copy.deepcopy(rows[: self.cap])


def season_2026_api(cap: int = GAMES_CAP) -> FakeApi:
    return FakeApi({2026: load("cbbd_teams_2026.json")}, {2026: load("cbbd_games_2026.json")}, cap)
