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


def season_2027_api(extra_games: list | None = None, cap: int = GAMES_CAP) -> FakeApi:
    """The 2026-27 fixture plus the real rows trimmed from the probe that exercise the gates: two
    0-0 finals against non-D-I schools (374609, 375008) and the second of the Loyola Chicago /
    Seattle U pair (378701; the first, 372907, is already in the base fixture). The teams are the
    base fixture plus North Carolina A&T and Bethune-Cookman."""
    teams = load("cbbd_teams_2027.json") + load("cbbd_teams_2027_extra.json")
    games = load("cbbd_games_2027.json") + load("cbbd_games_2027_anomalies.json") + (extra_games or [])
    return FakeApi({2027: teams}, {2027: games}, cap)


def with_scores(games: list, game_id: int, home: int, away: int) -> None:
    """SYNTHETIC: the source has no result for these games yet; fill one in to build a test case."""
    for g in games:
        if g["id"] == game_id:
            g.update(status="final", homePoints=home, awayPoints=away)
