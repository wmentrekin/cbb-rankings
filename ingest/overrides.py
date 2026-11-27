"""Operator overrides of game results, kept in `ingest/game_result_overrides.json`.

The source's result for a game is not always the one the rankings should use, and its list of
official games is not always the one the owner accepts (for example a disputed exhibition
against a non-D-I school). Overrides live in a checked-in file, so they are reviewed in a pull
request, and they are re-applied on every run: an ingest upsert rewrites an overridden game
from the source, so `apply_overrides` must run right after ingest and before the model reads
games. A manual database edit would simply be overwritten.

Three kinds of entry (every key shown is required unless marked optional; unknown keys are
rejected):

    {"kind": "score", "game_id": 401, "season": 2026, "home_team_id": 52, "away_team_id": 99,
     "home_score": 70, "away_score": 72, "periods": 2, "reason": "Official scorer corrected ..."}
    {"kind": "exclude", "game_id": 402, "season": 2026, "home_team_id": 52, "away_team_id": 721,
     "reason": "Exhibition against a non-D-I school the source lists as official"}
    {"kind": "site", "game_id": 403, "season": 2026, "home_team_id": 902, "away_team_id": 344,
     "neutral_site": false, "swap_home_away": true, "reason": "Played at the away team's arena"}

`score` sets the final score; winner_team_id, margin and alpha are re-derived with
`ingest.derive.derive_result` (the function ingest uses) from the stored neutral_site flag, so
they cannot drift; `periods` (optional, an int >= 2) is stored when given. Only a game whose
stored status is `final` can be re-scored. `exclude` sets `is_rated = 0` and leaves scores alone.
`site` fixes the source's inverted-site quirks: it sets `neutral_site` and, when
`swap_home_away` (optional, default false) is true, swaps home and away (ids, names and scores
together), then re-derives winner, margin and alpha. Its team ids are the game AS THE SOURCE
LISTS IT; a game already swapped by an earlier run is recognised, so applying twice is a no-op.

A game takes at most one `score` or `site` entry plus optionally one `exclude`; the two
combine (each reads the row the other left). An `exclude` matches the two teams in either order,
because a `site` swap may have reversed them.

Team ids and season are a safety check against the stored row: a mismatch fails that entry and
touches nothing. A failed entry is logged with OVERRIDE_FAILURE and returned in the summary, and
the other entries still apply. A malformed FILE raises `OverrideFileError` (also logged with the
marker). Writes go through `store.upsert` with its changed-values guard, so applying twice
writes nothing the second time.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from database.store import Store
from ingest.derive import DEFAULT_ALPHA, AlphaConfig, derive_result

log = logging.getLogger(__name__)

OVERRIDE_FAILURE_MARKER = "OVERRIDE_FAILURE"
DEFAULT_OVERRIDES_PATH = Path(__file__).resolve().parent / "game_result_overrides.json"
FILE_VERSION = 1

_IDENTITY = {"kind", "game_id", "season", "home_team_id", "away_team_id", "reason"}
_ALLOWED_KEYS = {
    "score": (_IDENTITY | {"home_score", "away_score"}, {"periods"}),  # (required, optional)
    "exclude": (_IDENTITY, set()),
    "site": (_IDENTITY | {"neutral_site"}, {"swap_home_away"}),
}
_INT_KEYS = ("game_id", "season", "home_team_id", "away_team_id", "home_score", "away_score", "periods")
_BOOL_KEYS = ("neutral_site", "swap_home_away")


class OverrideFileError(ValueError):
    """The overrides file is missing, unreadable or malformed. Nothing was applied."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _entry_errors(entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return ["is not an object"]
    kind = entry.get("kind")
    if kind not in _ALLOWED_KEYS:
        return [f"kind must be one of {sorted(_ALLOWED_KEYS)}, got {kind!r}"]
    required, optional = _ALLOWED_KEYS[kind]
    errors = []
    if missing := sorted(required - set(entry)):
        errors.append(f"missing key(s) {missing}")
    if unknown := sorted(set(entry) - required - optional):
        errors.append(f"unknown key(s) {unknown} for kind {kind!r}")
    for key in _INT_KEYS:
        if key in entry and not _is_int(entry[key]):
            errors.append(f"{key} must be an integer, got {entry[key]!r}")
    for key in _BOOL_KEYS:
        if key in entry and not isinstance(entry[key], bool):
            errors.append(f"{key} must be true or false, got {entry[key]!r}")
    reason = entry.get("reason")
    if "reason" in entry and not (isinstance(reason, str) and reason.strip()):
        errors.append("reason must be a non-empty string")
    if kind == "score":
        for key in ("home_score", "away_score", "periods"):
            if _is_int(entry.get(key)) and entry[key] < (2 if key == "periods" else 0):
                errors.append(f"{key} must be >= {2 if key == 'periods' else 0}, got {entry[key]}")
        if _is_int(entry.get("home_score")) and entry.get("home_score") == entry.get("away_score"):
            errors.append(f"home_score and away_score are both {entry['home_score']}: a tie is not a result")
    if _is_int(entry.get("home_team_id")) and entry.get("home_team_id") == entry.get("away_team_id"):
        errors.append("home_team_id and away_team_id are the same")
    return errors


def load_overrides(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Read and strictly validate the file; every problem found is listed in the one error."""
    target = Path(path) if path is not None else DEFAULT_OVERRIDES_PATH
    try:
        data = json.loads(target.read_text())
    except (OSError, ValueError) as exc:
        raise OverrideFileError(f"{target}: cannot read as JSON: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {"version", "overrides"}:
        raise OverrideFileError(f"{target}: top level must be an object with exactly 'version' and 'overrides'")
    errors = []
    if not _is_int(data["version"]) or data["version"] != FILE_VERSION:
        errors.append(f"version must be {FILE_VERSION}, got {data['version']!r}")
    entries = data["overrides"]
    if not isinstance(entries, list):
        raise OverrideFileError(f"{target}: 'overrides' must be a list")
    for i, entry in enumerate(entries):
        errors += [f"overrides[{i}]: {e}" for e in _entry_errors(entry)]
    valid = [e for e in entries if isinstance(e, dict) and _is_int(e.get("game_id")) and e.get("kind") in _ALLOWED_KEYS]
    for group, label in ((("score", "site"), "score/site"), (("exclude",), "exclude")):
        ids = [e["game_id"] for e in valid if e["kind"] in group]
        if dupes := sorted({g for g in ids if ids.count(g) > 1}):
            errors.append(f"duplicate game_id(s) {dupes}: a game takes at most one {label} entry")
    if errors:
        raise OverrideFileError(f"{target}: " + "; ".join(errors))
    return entries


@dataclass
class OverrideSummary:
    applied: list[int] = field(default_factory=list)  # changed the stored row
    unchanged: list[int] = field(default_factory=list)  # stored row already matched
    failed: list[dict[str, Any]] = field(default_factory=list)  # {"game_id", "reason"}
    skipped_other_season: list[int] = field(default_factory=list)
    rows_written: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied, "unchanged": self.unchanged, "failed": self.failed,
            "skipped_other_season": self.skipped_other_season, "rows_written": self.rows_written,
        }


def _fail(summary: OverrideSummary, game_id: int, reason: str) -> None:
    summary.failed.append({"game_id": game_id, "reason": reason})
    log.error("%s: game_id=%s reason=%s", OVERRIDE_FAILURE_MARKER, game_id, reason)


def _swap_sides(row: dict[str, Any]) -> dict[str, Any]:
    """Home and away exchanged: ids, names and scores together (winner and margin are unchanged)."""
    swapped = dict(row)
    for field_ in ("team_id", "team", "score"):
        swapped[f"home_{field_}"], swapped[f"away_{field_}"] = row[f"away_{field_}"], row[f"home_{field_}"]
    return swapped


def _require_orientation(stored: dict[str, Any], entry: dict[str, Any]) -> None:
    for key in ("home_team_id", "away_team_id"):
        if stored[key] != entry[key]:
            raise ValueError(f"{key} mismatch: stored {stored[key]!r}, override {entry[key]!r}")


def _site_row(entry: dict[str, Any], stored: dict[str, Any], alpha_cfg: AlphaConfig) -> dict[str, Any]:
    pair = (stored["home_team_id"], stored["away_team_id"])
    if pair == (entry["home_team_id"], entry["away_team_id"]):
        row = dict(stored)  # as the source lists it
    elif entry.get("swap_home_away") and pair == (entry["away_team_id"], entry["home_team_id"]):
        row = _swap_sides(stored)  # an earlier run already swapped it: undo, then redo below
    else:
        _require_orientation(stored, entry)
        raise ValueError(f"team id mismatch: stored home/away {pair}")  # swapped pair without swap_home_away
    if entry.get("swap_home_away"):
        row = _swap_sides(row)
    row["neutral_site"] = int(entry["neutral_site"])
    result, _ = derive_result(  # a game without a clean final result keeps its NULL result
        row["status"], row["home_team_id"], row["away_team_id"], row["home_score"], row["away_score"],
        bool(row["neutral_site"]), alpha_cfg,
    )
    row.update(result)
    return row


def _target_row(entry: dict[str, Any], stored: dict[str, Any], alpha_cfg: AlphaConfig) -> dict[str, Any]:
    """The full stored row with the override applied; raises ValueError when it cannot be."""
    if stored["season"] != entry["season"]:
        raise ValueError(f"season mismatch: stored {stored['season']!r}, override {entry['season']!r}")
    kind = entry["kind"]
    if kind == "site":
        return _site_row(entry, stored, alpha_cfg)
    if kind == "exclude":
        stored_pair = {stored["home_team_id"], stored["away_team_id"]}
        if stored_pair != {entry["home_team_id"], entry["away_team_id"]}:
            _require_orientation(stored, entry)  # names the differing id
        return {**stored, "is_rated": 0}
    _require_orientation(stored, entry)
    row = dict(stored)
    if stored["status"] != "final":
        raise ValueError(f"only a final game can be re-scored; stored status is {stored['status']!r}")
    result, code = derive_result(
        "final", stored["home_team_id"], stored["away_team_id"], entry["home_score"], entry["away_score"],
        bool(stored["neutral_site"]), alpha_cfg,
    )
    if code:  # unreachable after validation; kept so a rule change cannot store a half-result
        raise ValueError(f"scores rejected by derive_result: {code}")
    row.update(result)
    if "periods" in entry:
        row["periods"] = entry["periods"]
    return row


def apply_overrides(
    store: Store, season: int | None = None, *, path: Path | str | None = None,
    alpha_cfg: AlphaConfig = DEFAULT_ALPHA,
) -> OverrideSummary:
    """Apply the entries for `season` (all entries when None) to the stored games.

    Call it after every ingest and before the model runs. Raises `OverrideFileError` for a
    malformed file; a bad entry is reported in the summary and never raises.
    """
    try:
        entries = load_overrides(path)
    except OverrideFileError as exc:
        log.error("%s: %s", OVERRIDE_FAILURE_MARKER, exc)
        raise
    summary = OverrideSummary()
    for entry in entries:
        game_id = entry["game_id"]
        if season is not None and entry["season"] != season:
            summary.skipped_other_season.append(game_id)
            continue
        try:
            rows = store.query("SELECT * FROM games WHERE id = ?", [game_id])
            if not rows:
                raise ValueError("no stored game with this id")
            target = _target_row(entry, rows[0], alpha_cfg)
            if target == rows[0]:
                summary.unchanged.append(game_id)
                continue
            summary.rows_written += store.upsert("games", [target], ("id",)).rows_written
            summary.applied.append(game_id)
            log.info("applied %s override to game %s: %s", entry["kind"], game_id, entry["reason"])
        except Exception as exc:  # per-entry containment: the others still apply
            _fail(summary, game_id, str(exc))
    return summary
