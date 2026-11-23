"""Adapter from the football-shaped fixture to this repository's row shapes (test code only).

Football keys teams by name; this model keys by integer id. The mapping is stable: one id per
distinct name over the whole fixture (sorted names, starting at 1000, so an id can never be
mistaken for a fixture row number). The D-I set of a season is the fixture's `teams` rows for
that season (football's FBS list); every other name is a non-D-I opponent.

Game rows follow the `games` table. The football fixture has raw scores only, so the derived
columns are produced by `ingest.derive.derive_result` (the production derivation), not copied
from the goldens; the tests assert they equal the goldens' football-derived values. A 0-0
fixture row is football's "unplayed" sentinel: it becomes status `scheduled` with NULL results.
"""
from __future__ import annotations

from ingest.derive import derive_result
from tests.reference.fixture_io import load_fixture

FIRST_ID = 1000


def build():
    games_df, teams_df = load_fixture()
    names = sorted(set(games_df["home_team"]) | set(games_df["away_team"]) | set(teams_df["school"]))
    ids = {name: FIRST_ID + i for i, name in enumerate(names)}
    team_rows = [
        {"id": ids[r.school], "season": int(r.season), "school": r.school, "conference_id": 1,
         "conference": r.conference}
        for r in teams_df.itertuples()
    ]
    game_rows = []
    for r in games_df.itertuples():
        hs, as_ = int(r.home_score), int(r.away_score)
        status = "scheduled" if (hs == 0 and as_ == 0) else "final"
        home_id, away_id = ids[r.home_team], ids[r.away_team]
        result, anomaly = derive_result(status, home_id, away_id, hs, as_, bool(r.neutral_site))
        assert anomaly is None, (r.id, anomaly)
        game_rows.append({
            "id": int(r.id), "season": int(r.season), "week": int(r.week),
            "game_date": "2000-01-01", "start_date": "2000-01-01T00:00:00Z",
            "season_type": r.season_type, "game_type": "STD",
            "is_rated": int(r.season_type == "regular"), "status": status,
            "home_team_id": home_id, "home_team": r.home_team,
            "away_team_id": away_id, "away_team": r.away_team,
            "neutral_site": int(r.neutral_site), "conference_game": 0, **result,
        })
    return ids, team_rows, game_rows
