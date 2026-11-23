"""Deterministic raw fixture in cfb-rankings' table shape (seeded, checked in).

Writes, next to this file:
  football_teams.csv  -- rows of football's `teams` table that the model reads
                         (season, school; id and conference kept for shape).
  football_games.csv  -- rows of football's `games` table (the `rankings_games`
                         view is `season_type = 'regular'`): scores and
                         neutral_site are raw; winner/margin/alpha are NOT
                         stored because football derives them at ingest
                         (database/get_games.py:95-108) and the tests derive
                         them with the code under test.

Two seasons: 2023 (prior) and 2024 (current).  Names are fictional.
Run:  uv run python tests/fixtures/make_football_fixture.py
"""
from __future__ import annotations

import csv
import random
from pathlib import Path

HERE = Path(__file__).parent
SEED = 20241123

CORE = ["Ashford", "Bexley", "Carlow", "Dunmore", "Eastvale", "Fairmont", "Glenrock",
        "Hartwell", "Ironwood", "Juniper", "Kingsley", "Larkspur", "Millbrook", "Northgate"]
FCS = ["Alder State", "Birch College", "Cedar Tech", "Dogwood A&M"]
FBS = {2023: CORE + ["Redfern"], 2024: CORE + ["Oakhurst"]}
# Oakhurst is FCS in 2023 (so absent from the 2023 teams table and from the
# prior ratings); Redfern is FBS in 2023 only and appears as an FCS opponent in 2024.
FCS_OPP = {2023: FCS + ["Oakhurst"], 2024: FCS + ["Redfern"]}
REGULAR_WEEKS = range(1, 15)  # weeks 1..14 plus a week-15 championship game


def _simulate(rng, strength, home, away, neutral, force_winner=None, margin=None, loser_pts=None):
    """Return (home_score, away_score)."""
    diff = strength.get(home, -30.0) - strength.get(away, -30.0) + (0.0 if neutral else 3.0)
    d = rng.gauss(diff, 10.0)
    home_wins = d > 0
    if force_winner is not None:
        home_wins = force_winner == home
    if margin is None:
        margin = max(1, round(abs(d)))
        if force_winner is not None and (d > 0) != home_wins:
            margin = rng.randint(1, 14)
    lo = loser_pts if loser_pts is not None else int(min(max(rng.gauss(20, 8), 0), 42))
    hi = lo + margin
    return (hi, lo) if home_wins else (lo, hi)


def _season(season, rng, strength, scripted, forced_winners, bye_weeks, skip_prob):
    """Random schedule + scripted games. scripted: list of dicts."""
    games = []
    seen = set()
    for g in scripted:
        seen.add(frozenset((g["home"], g["away"])))
    fbs = FBS[season]
    fcs_pool = FCS_OPP[season]
    for week in REGULAR_WEEKS:
        busy = {t for g in scripted if g["week"] == week for t in (g["home"], g["away"])}
        elig = [t for t in fbs if t not in busy and week not in bye_weeks.get(t, ())]
        rng.shuffle(elig)
        if week <= 5:  # early-season FCS games
            for _ in range(2):
                if len(elig) > 2:
                    t = elig.pop()
                    opp = rng.choice(fcs_pool)
                    h, a = (t, opp) if rng.random() < 0.7 else (opp, t)
                    games.append(dict(week=week, home=h, away=a, neutral=False))
        while len(elig) >= 2:
            a, b = elig.pop(), elig.pop()
            key = frozenset((a, b))
            if key in seen or rng.random() < skip_prob:
                continue
            seen.add(key)
            neutral = rng.random() < 0.08
            h, aw = (a, b) if rng.random() < 0.5 else (b, a)
            games.append(dict(week=week, home=h, away=aw, neutral=neutral))
    games += [dict(g) for g in scripted]
    games.sort(key=lambda g: g["week"])
    rows = []
    for g in games:
        if "hs" in g:
            hs, as_ = g["hs"], g["as"]
        else:
            fw = forced_winners(g)
            hs, as_ = _simulate(rng, strength, g["home"], g["away"], g["neutral"], force_winner=fw)
        rows.append(dict(season=season, week=g["week"], season_type=g.get("season_type", "regular"),
                         home_team=g["home"], home_score=hs, away_team=g["away"], away_score=as_,
                         neutral_site=g["neutral"]))
    return rows


def _winner(r):
    return r["home_team"] if r["home_score"] > r["away_score"] else r["away_team"]


def _fix_losses(rows, season, rng):
    """2023 is the prior solve: no flat directions allowed, so every FBS team needs a loss."""
    fbs = set(FBS[season])
    for t in FBS[season]:
        losses = [r for r in rows if t in (r["home_team"], r["away_team"]) and r["home_score"] != r["away_score"]
                  and _winner(r) != t and r["home_score"] + r["away_score"] > 0]
        if losses:
            continue
        mine = [r for r in rows if t in (r["home_team"], r["away_team"]) and r["home_team"] in fbs and r["away_team"] in fbs]
        r = mine[-1]
        r["home_score"], r["away_score"] = r["away_score"], r["home_score"]


def build():
    rows = []
    # ---- 2023 (prior season) -------------------------------------------------
    rng = random.Random(SEED)
    s23 = {t: rng.gauss(0, 8) for t in FBS[2023]}
    s23["Northgate"] = -20.0
    sc23 = [
        dict(week=2, home="Alder State", away="Bexley", neutral=False, hs=21, **{"as": 17}),   # FBS away loss to FCS, margin 4
        dict(week=5, home="Juniper", away="Birch College", neutral=False, hs=14, **{"as": 24}),  # FBS home loss to FCS, margin 10
        dict(week=8, home="Oakhurst", away="Eastvale", neutral=True, hs=31, **{"as": 20}),     # neutral loss to future-FBS team, margin 11
        dict(week=15, home="Carlow", away="Glenrock", neutral=True, hs=27, **{"as": 24}),
    ]
    rows += _season(2023, rng, s23, sc23, lambda g: None, {}, 0.25)
    _fix_losses([r for r in rows if r["season"] == 2023], 2023, rng)

    # ---- 2024 (current season) ----------------------------------------------
    rng = random.Random(SEED + 1)
    s24 = {t: rng.gauss(0, 8) for t in FBS[2024]}
    s24["Ashford"] = 25.0
    s24["Northgate"] = -30.0
    sc24 = [
        # FBS losses to FCS: home/14, away/21, neutral/10, winless team home/12, away/3 (late)
        dict(week=2, home="Eastvale", away="Alder State", neutral=False, hs=10, **{"as": 24}),
        dict(week=6, home="Birch College", away="Juniper", neutral=False, hs=31, **{"as": 10}),
        dict(week=9, home="Kingsley", away="Cedar Tech", neutral=True, hs=20, **{"as": 30}),
        dict(week=4, home="Northgate", away="Dogwood A&M", neutral=False, hs=7, **{"as": 19}),
        dict(week=11, home="Alder State", away="Hartwell", neutral=False, hs=27, **{"as": 24}),
        # FBS win over FCS, big margin (record only)
        dict(week=3, home="Ashford", away="Redfern", neutral=False, hs=52, **{"as": 3}),
        # rematch: same two teams, split results
        dict(week=2, home="Carlow", away="Dunmore", neutral=False, hs=28, **{"as": 21}),
        dict(week=6, home="Dunmore", away="Carlow", neutral=False, hs=24, **{"as": 14}),
        # margin large enough to hit the 20-point cap (88; k*sqrt(88) > 20)
        dict(week=5, home="Glenrock", away="Northgate", neutral=False, hs=91, **{"as": 3}),
        # tiny margins
        dict(week=4, home="Hartwell", away="Ironwood", neutral=True, hs=24, **{"as": 23}),
        dict(week=8, home="Kingsley", away="Millbrook", neutral=False, hs=19, **{"as": 17}),
        # unplayed (0-0 sentinel) games inside the window
        dict(week=3, home="Millbrook", away="Dogwood A&M", neutral=False, hs=0, **{"as": 0}),
        dict(week=7, home="Fairmont", away="Ironwood", neutral=False, hs=0, **{"as": 0}),
        # Ashford's first loss; championship week
        dict(week=12, home="Kingsley", away="Ashford", neutral=False, hs=35, **{"as": 31}),
        dict(week=15, home="Ashford", away="Hartwell", neutral=True, hs=38, **{"as": 20}),
        # postseason row: excluded by the rankings_games view
        dict(week=16, home="Hartwell", away="Bexley", neutral=True, hs=30, **{"as": 27}, season_type="postseason"),
    ]

    def forced24(g):
        pair = (g["home"], g["away"])
        if g["week"] <= 10 and "Ashford" in pair:
            return "Ashford"
        if "Northgate" in pair:
            return pair[1] if pair[0] == "Northgate" else pair[0]
        return None

    bye24 = {"Larkspur": (1, 2, 3)}
    rows24 = _season(2024, rng, s24, sc24, forced24, bye24, 0.25)
    rows += rows24

    for i, r in enumerate(rows, start=1):
        r["id"] = i
    return rows


def teams_rows():
    out, i = [], 1
    for season in (2023, 2024):
        for school in FBS[season]:
            out.append(dict(id=i, season=season, school=school, conference="East" if i % 2 else "West"))
            i += 1
    return out


def main():
    rows = build()
    with open(HERE / "football_games.csv", "w", newline="") as f:
        cols = ["id", "season", "week", "season_type", "home_team", "home_score", "away_team", "away_score", "neutral_site"]
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for r in rows:
            r = dict(r)
            r["neutral_site"] = "true" if r["neutral_site"] else "false"
            w.writerow({c: r[c] for c in cols})
    with open(HERE / "football_teams.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "season", "school", "conference"], lineterminator="\n")
        w.writeheader()
        w.writerows(teams_rows())
    print(f"{len(rows)} games, {sum(r['season'] == 2023 for r in rows)} in 2023, {sum(r['season'] == 2024 for r in rows)} in 2024")


if __name__ == "__main__":
    main()
