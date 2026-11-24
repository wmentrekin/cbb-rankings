"""Frozen copy of the cfb-rankings optimization, for parity testing.

Provenance, edit list and known quirks: see README.md in this directory.
Source: cfb-rankings @ 40a6b4f -- model/params.py, model/margin_scale.py,
model/model.py, model/prior_model.py, model/process_data.py, model/connectivity.py,
database/game_derivations.py, database/get_games.py:97-108.  Do not "fix" anything
here; the port decides deliberately what to change.
"""
from dataclasses import dataclass
from typing import Optional

import cvxpy as cp
import networkx as nx
import numpy as np
import pandas as pd


# --- model/params.py (lines 12-56; module docstring dropped) ---

@dataclass(frozen=True)
class ModelParams:
    # Lower bound on a team's rating inside the solve. None = no bound, which is production.
    # A bound of 0.01 was the model until the 2026 retune: about a quarter of FBS sat on it
    # every week, all tied, in no meaningful order (a loss asks the loser to sit a set gap
    # below the winner, and a team that lost to low-rated teams "wants" to be below zero).
    r_min: Optional[float] = None
    # What the lowest team's PUBLISHED rating is shifted to. The solve is anchored at zero by
    # the loss-rate penalty, so its raw ratings run negative at the bottom; every rating is
    # moved up by the same amount so the lowest reads `display_min`. A uniform shift changes no
    # gap and no rank. None = publish raw. The prior model's output is never shifted: it is fed
    # back into the next season's solve, which works on the raw scale.
    display_min: Optional[float] = 0.01
    r_max: float = 100.0         # maximum team rating
    r_fcs_min: float = 0.01      # minimum rating of the stand-in FCS team
    r_fcs_max: float = 15.0      # maximum rating of the stand-in FCS team
    gamma_loss: float = 0.5      # weight of the loss-rate penalty
    gamma_fcs: float = 5.0       # weight of the FCS-loss penalty
    gamma_margin: float = 0.05   # weight of the margin penalty
    target_gap: float = 7.0      # rating points a median-margin win should demand
    max_gap: float = 20.0        # most rating points any one margin can demand
    # False: a result only penalises a winner that is not far ENOUGH above the loser (a hinge).
    # True: it also penalises one that is too far above, pinning the pair to the demanded gap.
    two_sided_margin: bool = False


DEFAULT_PARAMS = ModelParams()

# The model as it was published before the 2026 retune (apart from the margin scale, which
# scripts/compare_margin_scale.py swaps separately): a hard floor and no shift.
LEGACY_FLOOR_PARAMS = ModelParams(r_min=0.01, display_min=None)


def shift_to_display(ratings: dict, params: ModelParams) -> dict:
    """Move every rating by one constant so the lowest TEAM reads params.display_min.

    The stand-in FCS team ("fcs") moves with the rest but is not what sets the shift.
    """
    if params.display_min is None:
        return ratings
    team_values = [value for name, value in ratings.items() if name != "fcs"]
    if not team_values:
        return ratings
    offset = params.display_min - min(team_values)
    return {name: value + offset for name, value in ratings.items()}


# --- model/margin_scale.py (lines 21-50; module docstring dropped) ---

# Positions in a game tuple built by model.process_data.process_game_data.
GAME_MARGIN_INDEX = 4
GAME_ALPHA_INDEX = 5

# Used when there are no games to take a median over.
FALLBACK_MEDIAN_MARGIN = 1.0
MIN_SQRT_MEDIAN = 1e-6


def median_margin(games) -> float:
    """Median margin of victory over `games`; FALLBACK_MEDIAN_MARGIN when there are none.

    Missing (None / NaN) and negative margins are left out, as the inline code intended.
    """
    margins = []
    for game in games:
        margin = game[GAME_MARGIN_INDEX]
        if margin is None:
            continue
        margin = float(margin)
        if margin >= 0:  # False for NaN as well
            margins.append(margin)
    if not margins:
        return FALLBACK_MEDIAN_MARGIN
    return float(np.median(margins))


def compute_k_margin(games, target_gap: float) -> float:
    """Rating points per sqrt(point of margin), scaled so the median game demands `target_gap`."""
    return target_gap / max(float(np.sqrt(median_margin(games))), MIN_SQRT_MEDIAN)


# --- database/game_derivations.py (lines 10-49, docstrings dropped) ---

def compute_margin(home_score, away_score):
    return abs(home_score - away_score)


def compute_winner(home_team, away_team, home_score, away_score):
    if home_score == 0 and away_score == 0:
        return None
    return home_team if home_score > away_score else away_team


def compute_alpha(home_team, winner, neutral_site):
    return 1.0 if neutral_site else (0.8 if home_team == winner else 1.2)


def add_derived_columns(games_df):
    """database/get_games.py:97-108 (ingest derivation of margin/winner/alpha)."""
    games_df = games_df.copy()
    games_df["margin"] = games_df.apply(
        lambda row: compute_margin(row["home_score"], row["away_score"]), axis=1,
    )
    games_df["winner"] = games_df.apply(
        lambda row: compute_winner(row["home_team"], row["away_team"], row["home_score"], row["away_score"]),
        axis=1,
    )
    games_df["alpha"] = games_df.apply(
        lambda row: compute_alpha(row["home_team"], row["winner"], row["neutral_site"]), axis=1,
    )
    return games_df


# --- model/connectivity.py (lines 4-30; the diagnostic block at 25-28 removed) ---

def compute_connectivity_index(games, teams):
    teams = list(set(teams))
    team_set = set(teams)

    G = nx.Graph()
    G.add_nodes_from(teams)

    for i, j in games:
        if i in team_set and j in team_set:  # Only connect FBS <-> FBS
            G.add_edge(i, j)

    largest_cc = max(nx.connected_components(G), key=len)

    return len(largest_cc) / len(teams)


# --- model/process_data.py ---

def process_game_data(games_df, fbs_teams_df):
    """model/process_data.py:7-102, verbatim apart from comments."""
    teams = fbs_teams_df['school'].tolist()
    games = []
    fcs_losses = []

    game_counter = {}

    game_pairs = []

    records = {}
    for team in teams:
        records[team] = [0, 0, 0]

    for _, row in games_df.iterrows():
        team1, team2 = row['home_team'], row['away_team']
        game_pairs.append((team1, team2))
        team1_score, team2_score = row['home_score'], row['away_score']
        if pd.isnull(team1_score) or pd.isnull(team2_score) or pd.isnull(row['winner']):
            continue  # Skip games without scores or not yet played

        i, j = team1, team2
        key = tuple(sorted([i, j]))  # unordered pair
        game_counter[key] = game_counter.get(key, 0)
        k = game_counter[key]
        game_counter[key] += 1

        team1_is_fbs = team1 in teams
        team2_is_fbs = team2 in teams

        margin = row["margin"]
        alpha = row["alpha"]
        winner = row["winner"]
        if team1_is_fbs and team2_is_fbs:
            if winner == team1:
                records[team1][0] += 1
                records[team2][1] += 1
            elif winner == team2:
                records[team2][0] += 1
                records[team1][1] += 1
            else:
                records[team1][2] += 1
                records[team2][2] += 1
        elif team1_is_fbs and not team2_is_fbs:
            if winner == team1:
                records[team1][0] += 1
            elif winner == team2:
                records[team1][1] += 1
            else:
                records[team1][2] += 1
        elif team2_is_fbs and not team1_is_fbs:
            if winner == team2:
                records[team2][0] += 1
            elif winner == team1:
                records[team2][1] += 1
            else:
                records[team2][2] += 1

        # Track FCS losses
        if team1_is_fbs and not team2_is_fbs and team1_score < team2_score:
            fcs_losses.append((team1, row.get('margin'), row.get('alpha'), row.get("week"), row.get("season")))
        elif team2_is_fbs and not team1_is_fbs and team2_score < team1_score:
            fcs_losses.append((team2, row.get('margin'), row.get('alpha'), row.get("week"), row.get("season")))
        elif team1_is_fbs and team2_is_fbs:
            games.append((
                team1,
                team2,
                k,
                winner,
                margin,
                alpha,
                row.get("week"),
                row.get("season")
            ))

    connectivity = compute_connectivity_index(game_pairs, teams)

    return teams, games, fcs_losses, records, connectivity


def get_games_by_year_week(all_games_df, year, week=None):
    """In-memory stand-in for the SQL at model/process_data.py:119-125 (unchanged since 8ab8201).

    `rankings_games` is `SELECT * FROM games WHERE season_type = 'regular'`
    (database/migrations/0001_rankings_games_view.sql); the year/week predicates
    are the query's. No ORDER BY in football's query: row order is the input order.
    """
    df = all_games_df[all_games_df["season_type"] == "regular"]
    if week:
        return df[(df["season"] == year) & (df["week"] <= week)]
    return df[df["season"] == year]


def get_fbs_teams_by_year(all_teams_df, year):
    """In-memory stand-in for `SELECT * FROM teams WHERE season = year` (process_data.py:143)."""
    return all_teams_df[all_teams_df["season"] == year]


def get_data_by_year_up_to_week(all_games_df, all_teams_df, year, week=None):
    """model/process_data.py:148-165."""
    games_df = get_games_by_year_week(add_derived_columns(all_games_df), year, week)
    if week:
        games_df = games_df[games_df["week"] <= week]
    fbs_teams_df = get_fbs_teams_by_year(all_teams_df, year)
    teams, games, fcs_losses, records, connectivity = process_game_data(games_df, fbs_teams_df)
    return teams, games, fcs_losses, records, connectivity


# --- model/prior_model.py ---

def get_prior_ratings(all_games_df, all_teams_df, year, params = DEFAULT_PARAMS, solver=None):
    """model/prior_model.py:10-136 (prior-season solve: no prior term, no lambda).

    Returns (prior_ratings, details). Football returns only the ratings dict
    (including the "fcs" entry), RAW: the prior model's output is never shifted.
    `details` is added (edit 3).
    """
    teams, games, fcs_losses, records, connectivity = get_data_by_year_up_to_week(all_games_df, all_teams_df, year)

    # Parameters
    # Values and their meanings live in model/params.py; the defaults are the production model.
    r_min, r_max = params.r_min, params.r_max
    r_fcs_min, r_fcs_max = params.r_fcs_min, params.r_fcs_max
    gamma_loss, gamma_fcs, gamma_margin = params.gamma_loss, params.gamma_fcs, params.gamma_margin

    # Margin Regularization Parameters
    TARGET_GAP_FOR_MEDIAN = params.target_gap      # rating points the median margin should represent
    MAX_RATING_GAP_FROM_MARGIN = params.max_gap    # cap for maximum rating gap any margin can demand
    k_margin = compute_k_margin(games, TARGET_GAP_FOR_MEDIAN)

    # Decision Variables
    r = {team: cp.Variable(name = f"r_{team}") for team in teams} # team rating
    r_fcs = cp.Variable(name = "r_fcs") # rating for dummy FCS team

    # FCS Loss Penalty Terms
    fcs_loss_terms = []
    for (team, margin, alpha, _, _) in fcs_losses:
        fcs_loss_terms.append(cp.pos(r[team] + (margin * alpha) - r_fcs)**2 * gamma_fcs)
    # cp.sum([]) is a plain 0 with no .value, which crashed the diagnostics below in a season
    # with no FCS losses. Same guard as model/model.py.
    fcs_loss_penalty = cp.sum(fcs_loss_terms) if fcs_loss_terms else cp.Constant(0.0)

    # Soft Margin Penalty Terms
    soft_margin_terms = []
    for (i, j, k, winner, margin, alpha, _, _) in games:
        margin_val = max(0.0, float(margin))
        margin_scaled = k_margin * np.sqrt(margin_val)
        margin_used = min(margin_scaled, MAX_RATING_GAP_FROM_MARGIN)
        if winner == i:
            winner_team = i
            loser_team = j
        else:
            winner_team = j
            loser_team = i
        shortfall = r[loser_team] + (margin_used * alpha) - r[winner_team]
        if not params.two_sided_margin:
            shortfall = cp.pos(shortfall)
        soft_margin_terms.append(shortfall**2 * gamma_margin)
    soft_margin_penalty = cp.sum(soft_margin_terms)

    # Loss Rate Penalty Terms
    loss_rate_terms = []
    for team, record in records.items():
        wins = record[0]
        losses = record[1]
        games_played = wins + losses
        if games_played > 0:
            loss_rate = losses / games_played
            loss_rate_terms.append((r[team] * loss_rate)**2 * gamma_loss)
    loss_rate_penalty = cp.sum(loss_rate_terms)

    # Objective Function
    objective = cp.Minimize(soft_margin_penalty + loss_rate_penalty + fcs_loss_penalty)

    # Constraints
    constraints = []
    for (team) in teams:
        if r_min is not None:
            constraints.append(r[team] >= r_min)
        constraints.append(r[team] <= r_max)
    constraints.append(r_fcs >= r_fcs_min)
    constraints.append(r_fcs <= r_fcs_max)
    # Redundant with a positive r_min (the pre-2026 model); skipped when there is no lower
    # bound (production), where it would silently put the floor back.
    if r_min is not None and r_min >= 0:
        for (team) in teams:
            constraints.append(r[team] >= 0)

    # Solve
    problem = cp.Problem(objective, constraints)
    if solver is None:
        problem.solve(verbose = False)
    else:
        problem.solve(solver = solver, verbose = False)
    if problem.status not in ["infeasible", "unbounded"]:
        prior_ratings = {}
        for variable in problem.variables():
            name = variable.name()
            if name.startswith("r_"):
                team_name = name[2:]
                try:
                    prior_ratings[team_name] = float(variable.value)
                except Exception:
                    prior_ratings[team_name] = float(np.asarray(variable.value).item())
        prior_ratings = dict(sorted(prior_ratings.items(), key = lambda item: item[1], reverse = True))

    details = {
        "objective": float(problem.value),
        "margin_penalty": float(soft_margin_penalty.value),
        "loss_rate_penalty": float(loss_rate_penalty.value),
        "fcs_loss_penalty": float(fcs_loss_penalty.value),
        "prior_term": 0.0,
        "r_fcs": float(r_fcs.value),
        "solver": problem.solver_stats.solver_name,
        "k_margin": float(k_margin),
        "n_games": len(games),
        "n_fcs_losses": len(fcs_losses),
    }
    return prior_ratings, details


# --- model/model.py ---

def get_ratings(all_games_df, all_teams_df, year, week = None, params = DEFAULT_PARAMS, prior_ratings=None, solver=None):
    """model/model.py:11-160.

    Returns (ratings, records, details). Football returns (ratings, records);
    `ratings` is SHIFTED for display (shift_to_display) and includes the "fcs"
    entry. `prior_ratings` (dict) replaces the DB read chain
    `get_prior_ratings(year - 1, params)`; when None the reference prior solve is
    run on the same in-memory frames. `details` is added (edit 3) and carries
    `raw_ratings`, the solve's values before the shift.
    """
    # Get Data
    teams, games, fcs_losses, records, connectivity = get_data_by_year_up_to_week(all_games_df, all_teams_df, year, week)
    if prior_ratings is None:
        prior_ratings, _ = get_prior_ratings(all_games_df, all_teams_df, year - 1, params, solver=solver)
    else:
        prior_ratings = dict(prior_ratings)  # football mutates its own dict; do not mutate the caller's

    # Assign Prior Rating to New FBS Teams
    for team in teams:
        if team not in prior_ratings.keys():
            prior_ratings[team] = 1

    # Parameters
    # Values and their meanings live in model/params.py; the defaults are the production model.
    r_min, r_max = params.r_min, params.r_max
    r_fcs_min, r_fcs_max = params.r_fcs_min, params.r_fcs_max
    gamma_loss, gamma_fcs, gamma_margin = params.gamma_loss, params.gamma_fcs, params.gamma_margin

    # Prior Regularization Parameter
    _lambda = cp.Parameter(nonneg=True) # calculate term based on lambda_max and connectivity matrix
    if week < 7:
        _lambda.value = (7 - week) / 700
    else:
        _lambda.value = 0

    # Margin Regularization Parameters
    TARGET_GAP_FOR_MEDIAN = params.target_gap      # rating points the median margin should represent
    MAX_RATING_GAP_FROM_MARGIN = params.max_gap    # cap for maximum rating gap any margin can demand
    k_margin = compute_k_margin(games, TARGET_GAP_FOR_MEDIAN)

    # Decision Variables
    r = {team: cp.Variable(name = f"r_{team}") for team in teams} # team rating
    r_fcs = cp.Variable(name = "r_fcs") # rating for dummy FCS team

    # Prior Rating Terms
    prior_term = _lambda * cp.sum([(r[i] - prior_ratings[i])**2 for i in teams])

    # FCS Loss Penalty Terms
    fcs_loss_terms = []
    for (team, margin, alpha, _, _) in fcs_losses:
        fcs_loss_terms.append(cp.pos(r[team] + (margin * alpha) - r_fcs)**2 * gamma_fcs)
    fcs_loss_penalty = cp.sum(fcs_loss_terms) if fcs_loss_terms else cp.Constant(0.0)

    # Soft Margin Penalty Terms
    soft_margin_terms = []
    for (i, j, k, winner, margin, alpha, _, _) in games:
        margin_val = max(0.0, float(margin))
        margin_scaled = k_margin * np.sqrt(margin_val)
        margin_used = min(margin_scaled, MAX_RATING_GAP_FROM_MARGIN)
        if winner == i:
            winner_team = i
            loser_team = j
        else:
            winner_team = j
            loser_team = i
        shortfall = r[loser_team] + (margin_used * alpha) - r[winner_team]
        if not params.two_sided_margin:
            shortfall = cp.pos(shortfall)
        soft_margin_terms.append(shortfall**2 * gamma_margin)
    soft_margin_penalty = cp.sum(soft_margin_terms)

    # Loss Rate Penalty Terms
    loss_rate_terms = []
    for team, record in records.items():
        wins = record[0]
        losses = record[1]
        games_played = wins + losses
        if games_played > 0:
            loss_rate = losses / games_played
            loss_rate_terms.append((r[team] * loss_rate)**2 * gamma_loss)
    loss_rate_penalty = cp.sum(loss_rate_terms)

    # Objective Function
    objective = cp.Minimize(soft_margin_penalty + loss_rate_penalty + fcs_loss_penalty + prior_term)

    # Constraints
    constraints = []
    for (team) in teams:
        if r_min is not None:
            constraints.append(r[team] >= r_min)
        constraints.append(r[team] <= r_max)
    constraints.append(r_fcs >= r_fcs_min)
    constraints.append(r_fcs <= r_fcs_max)

    # Solve
    problem = cp.Problem(objective, constraints)
    if solver is None:
        problem.solve(verbose = False)
    else:
        problem.solve(solver = solver, verbose = False)

    # Diagnostics
    if problem.status not in ["infeasible", "unbounded"]:
        ratings = {}
        for variable in problem.variables():
            name = variable.name()
            if name.startswith("r_"):
                team_name = name[2:]
                try:
                    ratings[team_name] = float(variable.value)
                except Exception:
                    ratings[team_name] = float(np.asarray(variable.value).item())
        ratings = dict(sorted(ratings.items(), key = lambda item: item[1], reverse = True))

        details = {
            "objective": float(problem.value),
            "margin_penalty": float(soft_margin_penalty.value),
            "loss_rate_penalty": float(loss_rate_penalty.value),
            "fcs_loss_penalty": float(fcs_loss_penalty.value),
            "prior_term": float(prior_term.value),
            "r_fcs": float(r_fcs.value),
            "solver": problem.solver_stats.solver_name,
            "lambda": float(_lambda.value),
            "k_margin": float(k_margin),
            "connectivity": connectivity,
            "n_games": len(games),
            "n_fcs_losses": len(fcs_losses),
            "raw_ratings": ratings,
        }

        # Published scale: one uniform shift, after every diagnostic above has read the raw
        # solve. Order is unchanged, so `ratings` stays sorted.
        ratings = shift_to_display(ratings, params)

        return ratings, records, details

    else:
        raise ValueError(f"Problem status: {problem.status}")
