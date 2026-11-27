-- Source data: seasons, D-I teams per season, and games.
-- SQLite dialect, STRICT tables. Runs under Cloudflare D1 and Python's sqlite3.
-- Booleans are INTEGER 0/1. Dates are TEXT (ISO 8601).

-- One row per season; `season` is the year the season ends (2025-26 is 2026).
-- selection_sunday is entered by hand (the source does not provide it).
CREATE TABLE seasons (
    season           INTEGER PRIMARY KEY,
    anchor_monday    TEXT NOT NULL,   -- YYYY-MM-DD, first day of week 1
    selection_sunday TEXT NOT NULL,   -- YYYY-MM-DD
    last_ingested_at TEXT             -- ISO 8601 UTC
) STRICT;

-- D-I teams only, per season, from /teams?season=S. A game side whose id is not here for the
-- game's season is non-D-I. WITHOUT ROWID: a rowid table with a composite key would write a
-- second (autoindex) row on every insert, which counts against D1's daily rows-written budget.
CREATE TABLE teams (
    id              INTEGER NOT NULL,
    season          INTEGER NOT NULL,
    source_id       TEXT,             -- ESPN team id
    school          TEXT NOT NULL,
    abbreviation    TEXT,
    conference_id   INTEGER NOT NULL,
    conference      TEXT NOT NULL,
    primary_color   TEXT,             -- hex without '#'
    secondary_color TEXT,
    logo            TEXT,
    logo_dark       TEXT,
    PRIMARY KEY (id, season)
) STRICT, WITHOUT ROWID;

-- Team ids are deliberately not foreign keys: non-D-I opponents are not in `teams`.
CREATE TABLE games (
    id               INTEGER PRIMARY KEY,   -- source game id
    source_id        TEXT,                  -- ESPN game id
    season           INTEGER NOT NULL,
    week             INTEGER NOT NULL,
    game_date        TEXT NOT NULL,         -- YYYY-MM-DD, US/Eastern
    start_date       TEXT NOT NULL,         -- ISO 8601 UTC
    season_type      TEXT NOT NULL CHECK (season_type IN ('regular', 'postseason', 'preseason')),
    tournament       TEXT,                  -- NCAA / NIT / NULL
    game_type        TEXT NOT NULL,         -- STD / TRNMNT
    game_notes       TEXT,
    is_rated         INTEGER NOT NULL CHECK (is_rated IN (0, 1)),
    status           TEXT NOT NULL CHECK (status IN ('scheduled', 'in_progress', 'final', 'postponed', 'cancelled')),
    home_team_id     INTEGER NOT NULL,
    home_team        TEXT NOT NULL,
    home_score       INTEGER,               -- NULL until final
    away_team_id     INTEGER NOT NULL,
    away_team        TEXT NOT NULL,
    away_score       INTEGER,
    neutral_site     INTEGER NOT NULL CHECK (neutral_site IN (0, 1)),
    conference_game  INTEGER NOT NULL CHECK (conference_game IN (0, 1)),
    periods          INTEGER,               -- 2 = regulation, 3+ = overtime
    margin           INTEGER,
    winner_team_id   INTEGER,
    alpha            REAL
) STRICT;

-- Each index adds a written row per insert on D1 (100,000 rows/day on the free plan): keep few.
CREATE INDEX games_season_week ON games (season, week);
CREATE INDEX games_season_rated ON games (season, is_rated);
