"""Solver comparison through the frozen reference (documents the B3-T1 recommendation).

    uv run python tests/reference/measure_solvers.py

Measures, per solver, wall time of the whole reference call (assembly excluded
and reported separately) and agreement with OSQP (football's implicit default)
on (a) the football-shaped fixture and (b) a seeded synthetic basketball-sized
season: 365 teams, about 5,800 rated games over 19 weeks, 12 losses to lower-division teams.
"""
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.reference import football_reference as ref  # noqa: E402
from tests.reference.fixture_io import load_fixture, load_goldens  # noqa: E402

SOLVERS = ["OSQP", "CLARABEL", "SCS"]


def synthetic(seed=7, n_teams=365, n_games=5800, weeks=19, n_lower_losses=12, season=2025):
    rng = np.random.default_rng(seed)
    names = [f"Team{i:03d}" for i in range(n_teams)]
    strength = dict(zip(names, rng.normal(0, 9, n_teams)))
    rows = []
    for g in range(n_games):
        week = 1 + g * weeks // n_games
        h, a = rng.choice(n_teams, 2, replace=False)
        h, a = names[h], names[a]
        neutral = bool(rng.random() < 0.05)
        d = strength[h] - strength[a] + (0 if neutral else 3.5) + rng.normal(0, 11)
        margin = max(1, int(round(abs(d))))
        lo = int(rng.integers(55, 80))
        hs, as_ = (lo + margin, lo) if d > 0 else (lo, lo + margin)
        rows.append((season, week, "regular", h, hs, a, as_, neutral))
    lower = [f"Lower{i:03d}" for i in range(40)]
    for g in range(1500):  # lower-division games: mostly D-I wins
        week = 1 + g * 8 // 1500
        t = names[int(rng.integers(n_teams))]
        lose = g < n_lower_losses
        win_pts, lose_pts = int(rng.integers(70, 90)), int(rng.integers(50, 70))
        home = bool(rng.random() < 0.7)
        t_pts, o_pts = (lose_pts, win_pts) if lose else (win_pts, lose_pts)
        opp = lower[int(rng.integers(len(lower)))]
        rows.append((season, week, "regular", t if home else opp, t_pts if home else o_pts, opp if home else t, o_pts if home else t_pts, False))
    games = pd.DataFrame(rows, columns=["season", "week", "season_type", "home_team", "home_score", "away_team", "away_score", "neutral_site"])
    games["id"] = range(1, len(games) + 1)
    # prior season: same teams, independent results
    prior = games.copy()
    prior["season"] = season - 1
    prior["home_score"], prior["away_score"] = prior["away_score"].values, prior["home_score"].values
    games = pd.concat([prior, games], ignore_index=True)
    teams = pd.DataFrame({"id": range(2 * n_teams), "season": [season - 1] * n_teams + [season] * n_teams,
                          "school": names * 2, "conference": "X"})
    return games, teams


def timed(fn, repeat=3):
    out, times = None, []
    for _ in range(repeat):
        t = time.perf_counter()
        out = fn()
        times.append(time.perf_counter() - t)
    return out, statistics.median(times)


def compare(label, games, teams, year, weeks, repeat):
    print(f"\n== {label} ==")
    for week in weeks:
        t = time.perf_counter()
        teams_list, g, fl, rec, _ = ref.get_data_by_year_up_to_week(games, teams, year, week)
        print(f"-- year {year} week {week}: {len(teams_list)} teams, {len(g)} rated games, {len(fl)} lower-division losses "
              f"(assembly {time.perf_counter() - t:.2f}s)")
        results = {}
        for solver in SOLVERS:
            (ratings, _, details), secs = timed(lambda: ref.get_ratings(games, teams, year, week, solver=solver), repeat)
            results[solver] = (ratings, details, secs)
        base = results["OSQP"]
        for solver, (ratings, details, secs) in results.items():
            flat = {t for t, r in rec.items() if r[1] == 0} if week >= 7 else set()
            diff = max(abs(ratings[k] - base[0][k]) for k in ratings if k not in flat)
            rel = abs(details["objective"] - base[1]["objective"]) / base[1]["objective"]
            print(f"   {solver:9s} {secs:7.2f}s (incl. prior solve + assembly)  max|rating - OSQP| {diff:.2e} (excl. {len(flat)} zero-loss)  "
                  f"objective {details['objective']:.6f} rel diff vs OSQP {rel:.1e}  r_fcs {details['r_fcs']:.4f}")


if __name__ == "__main__":
    g, t = load_fixture()
    compare("football-shaped fixture", g, t, 2024, [3, 10, 15], repeat=5)
    sg, st = synthetic()
    compare("synthetic basketball-sized season (365 teams)", sg, st, 2025, [3, 10, 19], repeat=1)
