"""D-I teams per season from `/teams?season=S`.

With a season the endpoint returns exactly that season's D-I teams (probe, section 1); the
unfiltered `/teams` lists every division and must not be used.
"""
from __future__ import annotations

from typing import Any

from database.store import Store

LOGO_HOST = "https://cdn.collegefootballdata.com"


def fetch_teams(client, season: int) -> list[dict[str, Any]]:
    """One API call."""
    return client.get("/teams", {"season": season})


def _logo(raw: dict[str, Any], folder: str) -> str | None:
    """The 500 px logo URL from the `logos` list, else derived from the ESPN id."""
    marker = f"/{folder}/500/"
    for url in raw.get("logos") or []:
        if marker in url:
            return url
    source_id = raw.get("sourceId")
    return f"{LOGO_HOST}/{folder}/500/{source_id}.png" if source_id and str(source_id).isdigit() else None


def team_row(raw: dict[str, Any], season: int) -> dict[str, Any]:
    if raw.get("conferenceId") is None or raw.get("conference") is None:
        raise ValueError(f"team {raw.get('id')} ({raw.get('school')}) has no conference in season {season}")
    return {
        "id": raw["id"],
        "season": season,
        "source_id": raw.get("sourceId"),
        "school": raw["school"],
        "abbreviation": raw.get("abbreviation"),
        "conference_id": raw["conferenceId"],
        "conference": raw["conference"],
        "primary_color": raw.get("primaryColor"),
        "secondary_color": raw.get("secondaryColor"),
        "logo": _logo(raw, "cbb-logos"),
        "logo_dark": _logo(raw, "cbb-logos-dark"),
    }


def team_rows(raw_teams: list[dict[str, Any]], season: int) -> list[dict[str, Any]]:
    """Rows for the upsert, one per team id (a repeated id keeps the last)."""
    return list({r["id"]: r for r in (team_row(t, season) for t in raw_teams)}.values())


def stored_team_ids(store: Store, season: int) -> set[int]:
    return {int(r["id"]) for r in store.query("SELECT id FROM teams WHERE season = ?", [season])}
