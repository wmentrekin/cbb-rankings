"""The one definition of a played, rated game and of a record, shared by ingest, the model and
the weekly orchestration. Import these; do not restate the conditions.

* Played game: `winner_team_id IS NOT NULL`. Ingest stores a NULL result (scores, margin,
  winner, alpha) for every game that is not a clean final, so this test, not `status`, is the
  one to use (a final with missing or equal scores keeps `status = 'final'`).
* Rated game: `is_rated = 1 AND winner_team_id IS NOT NULL`. This is what the model rates.
* Record (W-L): counted over rated, played games only, so an `exclude` override or the
  duplicate guard removes a game from both the rating and the record.
* Non-D-I side: team id not in `teams` for the season (unchanged; records still count games
  against non-D-I opponents).
"""
from __future__ import annotations

from typing import Any, Mapping

PLAYED_SQL = "winner_team_id IS NOT NULL"
RATED_PLAYED_SQL = "is_rated = 1 AND winner_team_id IS NOT NULL"
RECORD_SQL = RATED_PLAYED_SQL  # W-L counts exactly the rated, played games


def is_played(game: Mapping[str, Any]) -> bool:
    return game.get("winner_team_id") is not None


def is_rated_played(game: Mapping[str, Any]) -> bool:
    return bool(game.get("is_rated")) and is_played(game)
