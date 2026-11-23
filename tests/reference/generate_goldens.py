"""Generate golden outputs from the REAL cfb-rankings code (never from the reference copy).

Usage (from the repository root; sqlalchemy and python-dotenv are needed only
because football's modules import them at module level; no database is touched):

    uv run --with sqlalchemy==2.0.46 --with python-dotenv==1.2.1 \\
        python tests/reference/generate_goldens.py --football /home/user/cfb-rankings

`--football` may also come from the CFB_RANKINGS_PATH environment variable.
The football checkout must be at commit 8ab8201; the script refuses otherwise.

Isolation: this script imports nothing from this repository's `model`/`database`
packages. Football's repository root is put first on sys.path and every football
module's __file__ is asserted to live under it. Only football's two database
readers (`get_games_by_year_week`, `get_fbs_teams_by_year` in model/process_data.py)
are replaced, by in-memory readers over the checked-in fixture; `create_engine`
is replaced by a function that raises, so a database connection is impossible.
"""
import argparse
import contextlib
import io
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"
OUT = HERE / "goldens" / "football_goldens.json"
FOOTBALL_COMMIT = "8ab8201"

# (name, year, week). Prior season is always year - 1, solved by football itself.
SCENARIOS = [
    ("w03", 2024, 3),    # below the fade cutoff, a team with no games yet, 0-0 FBS-vs-FCS game
    ("w06", 2024, 6),    # last faded week (lambda = 1/700)
    ("w07", 2024, 7),    # first week with lambda = 0; 0-0 FBS-vs-FBS game enters the window
    ("w10", 2024, 10),
    ("w15", 2024, 15),   # whole regular season (the week-16 postseason row is excluded by the view)
]


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--football", default=os.environ.get("CFB_RANKINGS_PATH"))
    a = p.parse_args()
    if not a.football:
        p.error("pass --football or set CFB_RANKINGS_PATH")
    return Path(a.football).resolve()


def main():
    football = parse_args()
    head = subprocess.run(["git", "-C", str(football), "rev-parse", "--short=7", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(football), "status", "--porcelain", "--", "model", "database"],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert head == FOOTBALL_COMMIT and not dirty, f"football is at {head} (dirty={bool(dirty)}), need clean {FOOTBALL_COMMIT}"

    # Isolation: only football's root (plus the standard library / site-packages) is importable as model/database.
    sys.path = [str(football)] + [p for p in sys.path if Path(p or ".").resolve() not in (HERE, HERE.parent, HERE.parent.parent)]
    import sqlalchemy

    def no_database(*a, **k):
        raise RuntimeError("database access is disabled while generating goldens")
    sqlalchemy.create_engine = no_database

    import cvxpy as cp
    import numpy as np
    import pandas as pd
    import clarabel, osqp, scs, networkx  # noqa: F401  (versions are recorded below)
    import importlib.metadata as md

    football_model = importlib.import_module("model.model")
    football_prior = importlib.import_module("model.prior_model")
    football_pd = importlib.import_module("model.process_data")
    derivations = importlib.import_module("database.game_derivations")
    for mod in (football_model, football_prior, football_pd, derivations):
        assert str(Path(mod.__file__).resolve()).startswith(str(football) + os.sep), mod.__file__

    games_all = pd.read_csv(FIXTURES / "football_games.csv")
    teams_all = pd.read_csv(FIXTURES / "football_teams.csv")
    for c in ("home_score", "away_score"):
        games_all[c] = games_all[c].astype("Int64")
    games_all["neutral_site"] = games_all["neutral_site"].astype(bool)

    # ---- ingest derivations, with football's own functions (database/get_games.py:95-108) ----
    def derive(df):
        df = df.copy()
        df["margin"] = df.apply(lambda r: derivations.compute_margin(r["home_score"], r["away_score"]), axis=1)
        df["winner"] = df.apply(lambda r: derivations.compute_winner(r["home_team"], r["away_team"], r["home_score"], r["away_score"]), axis=1)
        df["alpha"] = df.apply(lambda r: derivations.compute_alpha(r["home_team"], r["winner"], r["neutral_site"]), axis=1)
        return df

    state = {"games": derive(games_all), "teams": teams_all}

    # ---- replace football's two database readers (SQL emulated: rankings_games view + query predicates) ----
    def fake_games_by_year_week(year, week=None):
        df = state["games"]
        df = df[df["season_type"] == "regular"]
        if week:
            return df[(df["season"] == year) & (df["week"] <= week)].copy()
        return df[df["season"] == year].copy()

    def fake_fbs_teams_by_year(year):
        return state["teams"][state["teams"]["season"] == year].copy()

    football_pd.get_games_by_year_week = fake_games_by_year_week
    football_pd.get_fbs_teams_by_year = fake_fbs_teams_by_year

    # ---- spy on cp.Problem (solver actually chosen) and on the prior solve's output ----
    problems = []
    BaseProblem = cp.Problem

    class SpyProblem(BaseProblem):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            problems.append(self)

    cp.Problem = SpyProblem

    prior_capture = []
    real_get_prior = football_prior.get_prior_ratings

    def spy_get_prior(year):
        out = real_get_prior(year)
        prior_capture.append(dict(out))  # football later adds default entries to this dict
        return out
    football_model.get_prior_ratings = spy_get_prior

    def f(x):
        return None if x is None else float(x)

    def evaluate_terms(assembled, ratings, prior_used, week, with_prior):
        """Plain-Python evaluation of the four objective terms at a solution.

        cvxpy flattens the objective sum, so football's term expressions cannot be
        read back from the Problem; this evaluates the same formulas from football's
        real assembled data, and describe() asserts the total equals problem.value.
        """
        games = assembled["games"]
        # football's median is taken over tuple index 5, which is `alpha`, not the margin (model/model.py:63)
        margins = [abs(g[5]) for g in games if g[5] is not None and g[5] >= 0]
        median_margin = float(np.median(margins)) if margins else 1.0
        k_margin = 7.0 / max(np.sqrt(median_margin), 1e-6)
        pos = lambda x: max(x, 0.0)
        margin = loss = fcs = prior = 0.0
        for (i, j, _, winner, m, alpha, _, _) in games:
            w, l = (i, j) if winner == i else (j, i)
            used = min(k_margin * np.sqrt(max(0.0, float(m))), 20.0)
            margin += pos(ratings[l] + used * alpha - ratings[w]) ** 2 * 0.05
        for team, (wins, losses, _) in assembled["records"].items():
            if wins + losses > 0:
                loss += (ratings[team] * (losses / (wins + losses))) ** 2 * 0.5
        for (team, m, alpha, _, _) in assembled["fcs_losses"]:
            fcs += pos(ratings[team] + m * alpha - ratings["fcs"]) ** 2 * 5
        if with_prior:
            lam = (7 - week) / 700 if week < 7 else 0.0
            prior = lam * sum((ratings[t] - prior_used.get(t, 1)) ** 2 for t in assembled["teams"])
        return {"margin": margin, "loss_rate": loss, "fcs_loss": fcs, "prior": prior}

    def describe(problem, ratings, assembled, prior_used, week, with_prior):
        terms = evaluate_terms(assembled, ratings, prior_used, week, with_prior)
        total = sum(terms.values())
        assert abs(total - problem.value) <= 1e-7 * max(1.0, abs(problem.value)), (total, problem.value)
        return {
            "objective": f(problem.value),
            "terms": terms,
            "r_fcs": f(next(v for v in problem.variables() if v.name() == "r_fcs").value),
            "solver": problem.solver_stats.solver_name,
        }

    def py(x):
        if isinstance(x, (np.integer,)):
            return int(x)
        if isinstance(x, (np.floating,)):
            return float(x)
        if isinstance(x, (list, tuple)):
            return [py(i) for i in x]
        if isinstance(x, dict):
            return {k: py(v) for k, v in x.items()}
        return x

    def run(year, week):
        problems.clear()
        prior_capture.clear()
        with contextlib.redirect_stdout(io.StringIO()):  # football prints (and solves verbosely) to stdout
            ratings, records = football_model.get_ratings(year, week)
            data = football_pd.get_data_by_year_up_to_week(year, week)
            prior_data = football_pd.get_data_by_year_up_to_week(year - 1)
        assert len(problems) == 2, len(problems)
        prior_problem, main_problem = problems
        assembled = {"teams": data[0], "games": py(data[1]), "fcs_losses": py(data[2]),
                     "records": py(data[3]), "connectivity": float(data[4])}
        prior_assembled = {"teams": prior_data[0], "games": py(prior_data[1]), "fcs_losses": py(prior_data[2]),
                           "records": py(prior_data[3])}
        prior_used = prior_capture[0]
        return {
            "year": year, "week": week,
            "ratings": dict(ratings), "records": py(records),
            "main": describe(main_problem, dict(ratings), assembled, prior_used, week, True),
            "prior_ratings_used": prior_used,
            "prior": describe(prior_problem, prior_used, prior_assembled, None, 0, False),
            "assembled": assembled,
        }

    scenarios = {}
    prior_season_ref = None
    for name, year, week in SCENARIOS:
        res = run(year, week)
        if prior_season_ref is None:
            prior_season_ref = {"year": year - 1, "ratings": res["prior_ratings_used"], **res["prior"]}
        else:  # the prior solve repeats for every scenario; it must be bit-identical
            assert res["prior_ratings_used"] == prior_season_ref["ratings"]
            assert res["prior"]["objective"] == prior_season_ref["objective"]
        res.pop("prior_ratings_used"); res.pop("prior")
        lam = (7 - week) / 700 if week < 7 else 0
        zero_loss = sorted(t for t, rec in res["records"].items() if rec[1] == 0)
        # A team with no loss has no strictly convex term in its own rating when lambda = 0
        # (undefeated: only one-sided hinges; no games: nothing), so its rating may be a flat direction.
        res["lambda"] = lam
        res["non_unique_teams"] = zero_loss if lam == 0 else []
        scenarios[name] = res

    # ---- quirk scenarios, also from the real code ----
    def attempt(fn):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                out = fn()
            return {"raised": None, "result": out}
        except Exception as e:
            return {"raised": type(e).__name__, "message": str(e)}

    quirks = {}
    quirks["week_none"] = attempt(lambda: len(football_model.get_ratings(2024, None)[0]))
    quirks["no_prior_season_data"] = attempt(lambda: len(football_model.get_ratings(2023, 15)[0]))
    # week=0 is falsy: every game is read, and lambda = 7/700
    problems.clear(); prior_capture.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        r0, rec0 = football_model.get_ratings(2024, 0)
        d0 = football_pd.get_data_by_year_up_to_week(2024, 0)
    a0 = {"teams": d0[0], "games": py(d0[1]), "fcs_losses": py(d0[2]), "records": py(d0[3])}
    quirks["week_zero"] = {"raised": None, "ratings": dict(r0), "records": py(rec0), "week": 0,
                           **describe(problems[-1], dict(r0), a0, prior_capture[0], 0, True)}

    base_games = state["games"]
    g24 = games_all[games_all["season"] == 2024]
    played = {frozenset(x) for x in zip(g24["home_team"], g24["away_team"])}
    core = sorted(teams_all[teams_all["season"] == 2024]["school"])
    tie_pair = next((a, b) for ai, a in enumerate(core) for b in core[ai + 1:] if frozenset((a, b)) not in played)
    extra = {"season": 2024, "week": 8, "season_type": "regular", "home_team": tie_pair[0], "home_score": 17,
             "away_team": tie_pair[1], "away_score": 17, "neutral_site": False, "id": 9001}
    tie_df = pd.concat([games_all, pd.DataFrame([extra]).astype({"home_score": "Int64", "away_score": "Int64"})], ignore_index=True)
    state["games"] = derive(tie_df)
    problems.clear(); prior_capture.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        rt, rect = football_model.get_ratings(2024, 10)
        dt = football_pd.get_data_by_year_up_to_week(2024, 10)
    at = {"teams": dt[0], "games": py(dt[1]), "fcs_losses": py(dt[2]), "records": py(dt[3])}
    quirks["tie_game"] = {"extra_game": extra, "year": 2024, "week": 10, "ratings": dict(rt), "records": py(rect),
                          "assembled": py(at), **describe(problems[-1], dict(rt), at, prior_capture[0], 10, True)}

    # prior season without lower-division losses: the solve succeeds, then the diagnostic print
    # dereferences `.value` of the int 0 that cp.sum([]) returned
    g23 = derive(games_all)[lambda d: d["season"] == 2023]
    fbs23 = set(teams_all[teams_all["season"] == 2023]["school"])
    lost_to_lower = g23[((g23["home_team"].isin(fbs23)) & ~g23["away_team"].isin(fbs23) & (g23["home_score"] < g23["away_score"])) |
                        ((g23["away_team"].isin(fbs23)) & ~g23["home_team"].isin(fbs23) & (g23["away_score"] < g23["home_score"]))]
    dropped = [int(i) for i in lost_to_lower["id"]]
    state["games"] = derive(games_all[~games_all["id"].isin(dropped)])
    quirks["prior_without_fcs_losses"] = {"dropped_game_ids": dropped,
                                          **attempt(lambda: len(football_prior.get_prior_ratings(2023)))}
    state["games"] = derive(games_all)

    # ---- derivation goldens: grid of single-game inputs through football's real functions ----
    cases = []
    for home_score, away_score in [(0, 0), (1, 0), (0, 1), (24, 24), (31, 17), (17, 31), (3, 0), (0, 3), (45, 44), (10, 70)]:
        for neutral in (False, True):
            winner = derivations.compute_winner("H", "A", home_score, away_score)
            cases.append({
                "home_team": "H", "away_team": "A", "home_score": home_score, "away_score": away_score,
                "neutral_site": neutral,
                "margin": int(derivations.compute_margin(home_score, away_score)),
                "winner": winner,
                "alpha": derivations.compute_alpha("H", winner, neutral),
            })
    fixture_derived = {
        int(r["id"]): {"winner": None if pd.isnull(r["winner"]) else r["winner"], "margin": int(r["margin"]), "alpha": float(r["alpha"])}
        for _, r in derive(games_all).iterrows()
    }

    versions = {p: md.version(p) for p in ("cvxpy", "numpy", "pandas", "clarabel", "osqp", "scs", "networkx", "scipy")}
    golden = {
        "provenance": {
            "repository": "cfb-rankings", "commit": head,
            "generated_by": "tests/reference/generate_goldens.py",
            "python": sys.version.split()[0], "versions": versions,
            "installed_solvers": cp.installed_solvers(),
            "default_solver_for_main_problem": scenarios["w03"]["main"]["solver"],
        },
        "prior_season": prior_season_ref,
        "scenarios": scenarios,
        "quirks": py(quirks),
        "derivation_cases": cases,
        "fixture_derived": {str(k): v for k, v in fixture_derived.items()},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(py(golden), indent=1, sort_keys=False, allow_nan=False) + "\n")
    print(f"wrote {OUT}")
    for n, s in scenarios.items():
        print(n, s["main"]["solver"], s["main"]["objective"], s["main"]["terms"], "non_unique:", s["non_unique_teams"])


if __name__ == "__main__":
    main()
