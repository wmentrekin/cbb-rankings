"""Pure per-game derivations and classification. No I/O.

Alpha, winner and margin reproduce the football pipeline's `game_derivations.py` for played
games (football keys the winner by name, here it is the team id):

    margin = abs(home - away)
    alpha  = 1.0 if neutral_site else (0.8 if the home team won else 1.2)

The alpha constants live in `AlphaConfig`; the model task reads the same object so the two
cannot drift.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Container
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

STATUSES = frozenset({"scheduled", "in_progress", "final", "postponed", "cancelled"})
SEASON_TYPES = frozenset({"regular", "postseason", "preseason"})

# Anomaly codes counted in the ingest summary.
FINAL_SCORE_MISSING = "final_score_missing"
FINAL_TIE = "final_tie"
MALFORMED_ROW = "malformed_row"
UNKNOWN_STATUS = "unknown_status"
UNKNOWN_SEASON_TYPE = "unknown_season_type"
NON_D1_HAS_CONFERENCE = "non_d1_side_has_conference"
D1_MISSING_CONFERENCE = "d1_side_missing_conference"


@dataclass(frozen=True)
class AlphaConfig:
    """Home/away multipliers, football's values by default."""

    neutral: float = 1.0
    home_win: float = 0.8
    away_win: float = 1.2


DEFAULT_ALPHA = AlphaConfig()


@dataclass
class GameOutcome:
    """Result of `derive_game`: the row to store (None if dropped) and what was noticed."""

    row: dict[str, Any] | None
    dropped_both_non_d1: bool = False
    anomalies: Counter = field(default_factory=Counter)
    messages: list[str] = field(default_factory=list)


def eastern_date(start_date: str) -> date:
    """US/Eastern calendar date of an ISO 8601 UTC timestamp such as 2025-11-04T00:00:00.000Z."""
    return datetime.fromisoformat(start_date).astimezone(EASTERN).date()


def is_rated(season_type: str, game_date: date, selection_sunday: date) -> bool:
    """Regular-season games (conference tournaments and in-season events included) on or before
    Selection Sunday. Postseason games (NCAA, NIT, Crown) are never rated."""
    return season_type == "regular" and game_date <= selection_sunday


def compute_alpha(home_won: bool, neutral_site: bool, cfg: AlphaConfig = DEFAULT_ALPHA) -> float:
    return cfg.neutral if neutral_site else (cfg.home_win if home_won else cfg.away_win)


def _valid_score(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def derive_result(
    status: str,
    home_id: int,
    away_id: int,
    home_score: Any,
    away_score: Any,
    neutral_site: bool,
    cfg: AlphaConfig = DEFAULT_ALPHA,
) -> tuple[dict[str, Any], str | None]:
    """Scores, margin, winner and alpha for one game, plus an anomaly code or None.

    played = status "final" with both scores present and unequal. Anything else stores NULL
    scores, margin, winner and alpha (never 0-0). A final that fails the score checks is an
    anomaly and is stored as not played.
    """
    unplayed = {"home_score": None, "away_score": None, "margin": None, "winner_team_id": None, "alpha": None}
    if status != "final":
        return unplayed, None
    if not (_valid_score(home_score) and _valid_score(away_score)):
        return unplayed, FINAL_SCORE_MISSING
    if home_score == away_score:
        return unplayed, FINAL_TIE
    home_won = home_score > away_score
    return {
        "home_score": home_score,
        "away_score": away_score,
        "margin": abs(home_score - away_score),
        "winner_team_id": home_id if home_won else away_id,
        "alpha": compute_alpha(home_won, neutral_site, cfg),
    }, None


_REQUIRED = ("id", "startDate", "seasonType", "gameType", "status", "homeTeamId", "homeTeam", "awayTeamId", "awayTeam")


def derive_game(
    raw: dict[str, Any],
    *,
    season: int,
    d1_ids: Container[int],
    selection_sunday: date,
    week_of: Callable[[date], int],
    alpha_cfg: AlphaConfig = DEFAULT_ALPHA,
) -> GameOutcome:
    """Map one API game to a `games` row.

    A side is non-D-I when its team id is not in `d1_ids` (that season's teams). A game with
    both sides non-D-I is dropped (`dropped_both_non_d1`). Malformed rows are skipped with an
    anomaly; nothing here raises for bad data.
    """
    out = GameOutcome(row=None)

    def note(code: str, msg: str) -> None:
        out.anomalies[code] += 1
        out.messages.append(f"{code}: game {raw.get('id')}: {msg}")

    missing = [k for k in _REQUIRED if raw.get(k) is None]
    if missing:
        note(MALFORMED_ROW, f"missing {missing}")
        return out
    if raw["status"] not in STATUSES:
        note(UNKNOWN_STATUS, f"status {raw['status']!r}")
        return out
    if raw["seasonType"] not in SEASON_TYPES:
        note(UNKNOWN_SEASON_TYPE, f"seasonType {raw['seasonType']!r}")
        return out
    try:
        game_date = eastern_date(raw["startDate"])
    except (ValueError, TypeError):
        note(MALFORMED_ROW, f"unparseable startDate {raw['startDate']!r}")
        return out

    home_id, away_id = raw["homeTeamId"], raw["awayTeamId"]
    home_d1, away_d1 = home_id in d1_ids, away_id in d1_ids
    if not home_d1 and not away_d1:
        out.dropped_both_non_d1 = True
        return out
    # Cross-check of the classification (the rule itself never branches on conference).
    for side, is_d1 in (("home", home_d1), ("away", away_d1)):
        has_conf = raw.get(f"{side}Conference") is not None
        if not is_d1 and has_conf:
            note(NON_D1_HAS_CONFERENCE, f"{side} team {raw[side + 'TeamId']} is not in teams but has conference {raw[side + 'Conference']!r}")
        elif is_d1 and not has_conf:
            note(D1_MISSING_CONFERENCE, f"{side} team {raw[side + 'TeamId']} is in teams but has no conference")

    neutral = bool(raw.get("neutralSite"))
    result, code = derive_result(
        raw["status"], home_id, away_id, raw.get("homePoints"), raw.get("awayPoints"), neutral, alpha_cfg
    )
    if code:
        note(code, f"final game stored as not played (scores {raw.get('homePoints')!r}-{raw.get('awayPoints')!r})")

    period_points = raw.get("homePeriodPoints")
    out.row = {
        "id": raw["id"],
        "source_id": raw.get("sourceId"),
        "season": season,
        "week": week_of(game_date),
        "game_date": game_date.isoformat(),
        "start_date": raw["startDate"],
        "season_type": raw["seasonType"],
        "tournament": raw.get("tournament"),
        "game_type": raw["gameType"],
        "game_notes": raw.get("gameNotes"),
        "is_rated": int(is_rated(raw["seasonType"], game_date, selection_sunday)),
        "status": raw["status"],
        "home_team_id": home_id,
        "home_team": raw["homeTeam"],
        "home_score": result["home_score"],
        "away_team_id": away_id,
        "away_team": raw["awayTeam"],
        "away_score": result["away_score"],
        "neutral_site": int(neutral),
        "conference_game": int(bool(raw.get("conferenceGame"))),
        "periods": len(period_points) if isinstance(period_points, list) and period_points else None,
        "margin": result["margin"],
        "winner_team_id": result["winner_team_id"],
        "alpha": result["alpha"],
    }
    return out
