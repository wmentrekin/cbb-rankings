"""Loads the football-shaped fixture and the goldens (shared by the reference tests and scripts)."""
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT.parent / "fixtures"
GOLDENS = ROOT / "goldens" / "football_goldens.json"


def load_fixture():
    games = pd.read_csv(FIXTURES / "football_games.csv")
    teams = pd.read_csv(FIXTURES / "football_teams.csv")
    for c in ("home_score", "away_score"):
        games[c] = games[c].astype("Int64")
    games["neutral_site"] = games["neutral_site"].astype(bool)
    return games, teams


def load_goldens():
    return json.loads(GOLDENS.read_text())
