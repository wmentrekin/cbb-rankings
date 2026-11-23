-- Weekly ratings. ratings_staging has the same columns and is used by staging runs.
-- WITHOUT ROWID so the composite primary key is the only row written per insert.

CREATE TABLE ratings (
    team_id INTEGER NOT NULL,
    season  INTEGER NOT NULL,
    week    INTEGER NOT NULL,
    rating  REAL NOT NULL,
    wins    INTEGER NOT NULL,
    losses  INTEGER NOT NULL,
    PRIMARY KEY (team_id, season, week)
) STRICT, WITHOUT ROWID;

CREATE TABLE ratings_staging (
    team_id INTEGER NOT NULL,
    season  INTEGER NOT NULL,
    week    INTEGER NOT NULL,
    rating  REAL NOT NULL,
    wins    INTEGER NOT NULL,
    losses  INTEGER NOT NULL,
    PRIMARY KEY (team_id, season, week)
) STRICT, WITHOUT ROWID;
