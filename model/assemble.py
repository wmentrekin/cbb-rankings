"""Turn stored game rows into the model's inputs. Pure: no database, no solver.

Rules (football's `process_game_data`, with the intentional differences listed in README.md):

* played = `winner_team_id` is not null; everything else is ignored.
* a side is D-I when its id is in `team_ids`; a game with no D-I side is ignored (ingest drops
  those already).
* D-I vs D-I played game -> `Game(winner, loser, margin, alpha)`.
* D-I team LOST to a non-D-I team -> `LowerLoss(team, margin, alpha)`. A D-I win over a non-D-I
  team is not a game for the optimization.
* records count every played game involving a D-I team, including those against non-D-I
  opponents: {team_id: (wins, losses)}; every D-I team has an entry, (0, 0) if it has no games.
* margin and alpha are read from the stored columns, never recomputed.

Output order is by game id, so results do not depend on the order rows arrive in.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Collection, Iterable, Mapping, NamedTuple

import networkx as nx
import pandas as pd

REQUIRED_COLUMNS = ("id", "home_team_id", "away_team_id", "winner_team_id", "margin", "alpha")


class Game(NamedTuple):
    """A played D-I vs D-I game."""

    winner: int
    loser: int
    margin: float
    alpha: float


class LowerLoss(NamedTuple):
    """A D-I team's loss to a non-D-I opponent."""

    team: int
    margin: float
    alpha: float


@dataclass(frozen=True)
class Assembled:
    games: list[Game]
    lower_losses: list[LowerLoss]
    records: dict[int, tuple[int, int]]
    connectivity: float  # share of D-I teams in the largest played-game component; diagnostic only
    n_ignored_unplayed: int
    n_ignored_no_d1: int


def _null(value: Any) -> bool:
    return value is None or bool(pd.isna(value))


def _records_of(rows: Iterable[Mapping[str, Any]] | pd.DataFrame) -> list[dict[str, Any]]:
    if isinstance(rows, pd.DataFrame):
        missing = [c for c in REQUIRED_COLUMNS if c not in rows.columns]
        if missing:
            raise ValueError(f"game rows are missing columns {missing}")
        out = rows.astype(object).to_dict("records")
    else:
        out = [dict(r) for r in rows]
        for r in out:
            missing = [c for c in REQUIRED_COLUMNS if c not in r]
            if missing:
                raise ValueError(f"game row {r.get('id')!r} is missing columns {missing}")
    return out


def connectivity_index(pairs: Iterable[tuple[int, int]], team_ids: Collection[int]) -> float:
    """Largest connected component's share of the teams (0.0 with no teams)."""
    teams = set(team_ids)
    if not teams:
        return 0.0
    graph = nx.Graph()
    graph.add_nodes_from(teams)
    graph.add_edges_from((a, b) for a, b in pairs if a in teams and b in teams)
    return len(max(nx.connected_components(graph), key=len)) / len(teams)


def assemble(rows: Iterable[Mapping[str, Any]] | pd.DataFrame, team_ids: Collection[int]) -> Assembled:
    d1 = {int(t) for t in team_ids}
    records = {t: [0, 0] for t in sorted(d1)}
    games: list[Game] = []
    lower: list[LowerLoss] = []
    pairs: list[tuple[int, int]] = []
    unplayed = no_d1 = 0

    seen: set[int] = set()
    for row in sorted(_records_of(rows), key=lambda r: int(r["id"])):
        if int(row["id"]) in seen:
            raise ValueError(f"duplicate game id {row['id']}")
        seen.add(int(row["id"]))
        if _null(row["winner_team_id"]):
            unplayed += 1
            continue
        home, away, winner = int(row["home_team_id"]), int(row["away_team_id"]), int(row["winner_team_id"])
        if winner not in (home, away):
            raise ValueError(f"game {row['id']}: winner {winner} is neither side ({home}, {away})")
        if _null(row["margin"]) or _null(row["alpha"]):
            raise ValueError(f"game {row['id']} has a winner but no margin or alpha")
        margin, alpha = float(row["margin"]), float(row["alpha"])
        loser = away if winner == home else home
        home_d1, away_d1 = home in d1, away in d1
        if not home_d1 and not away_d1:
            no_d1 += 1
            continue
        if winner in d1:
            records[winner][0] += 1
        if loser in d1:
            records[loser][1] += 1
        if home_d1 and away_d1:
            games.append(Game(winner, loser, margin, alpha))
            pairs.append((winner, loser))
        elif loser in d1:
            lower.append(LowerLoss(loser, margin, alpha))
    return Assembled(
        games=games,
        lower_losses=lower,
        records={t: (w, l) for t, (w, l) in records.items()},
        connectivity=connectivity_index(pairs, d1),
        n_ignored_unplayed=unplayed,
        n_ignored_no_d1=no_d1,
    )
