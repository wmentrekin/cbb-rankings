# CBBD source probe (B1-T1)

Probe of the CollegeBasketballData API (`https://api.collegebasketballdata.com`) run on 2026-11-23
to settle the provisional schema in `docs/cbb-rankings-port/plan.yaml`. All numbers come from saved
responses for the completed 2025-26 season (`season=2026`) and a spot-check of 2026-27 (`season=2027`,
games through 2026-11-23 plus the schedule to 2026-12-01).

**Verdict: the source is usable. Games between D-I and non-D-I teams are present (548 final, regular
season, in 2025-26), the non-D-I side carries a real team id, and `/teams?season=S` returns exactly
the D-I teams for season S.** The plan's key decision ("an opponent whose id is not in teams for that
season is non-D-I") works as written.

API calls used: 16 (14 returned 200; the first 2 returned an intermittent `502` from the edge and may
or may not have been counted). Quota header `X-CallLimit-Remaining` read 965 after the last call.

## 1. Non-D-I opponents

### Teams endpoint

| Request | Rows | `conference` null | Teams with no `logos` |
|---|---|---|---|
| `/teams?season=2025` | 364 | 0 | 0 |
| `/teams?season=2026` | 365 | 0 | 0 |
| `/teams?season=2027` | 365 | 0 | 0 |
| `/teams` (no season) | 1,546 | 1,181 | 778 |

- With `season`, `/teams` returns D-I teams only, each with that season's `conferenceId`/`conference`
  (31 conferences in 2025-26). Membership matches `/conferences/history` exactly for 2025, 2026 and
  2027 (0 differences each way) and `/teams/directory?season=2026` (same 365 ids, same conferences).
- Without `season`, `/teams` returns every team the source knows (D-I, D-II, D-III, NAIA, junior
  colleges, placeholders such as `TBD`), with current-season conferences for D-I and `conference: null`
  for everyone else. Do not use it to decide D-I membership.
- No division/classification field exists on teams or games, and no endpoint or parameter filters by
  division. D-I membership per season is expressed only by presence in `/teams?season=S`
  (equivalently, by a `/conferences/history` membership row covering S). D-I independents appear with
  conference `Indep.` (id 32, "Division I Independents"), not null (e.g. Chicago State 2023-2024).
- Transitions: a reclassifying school appears in `/teams?season=S` from its first transition season.
  New Haven (id 1507) appears from 2026 (NEC); Mercyhurst (id 165) from 2025 (NEC); West Florida
  (id 1073) from 2027 (ASUN). A school leaving D-I disappears: St. Francis (PA) (id 278) is in 2026
  (NEC history `endSeason: 2026`) and absent from 2027. Season-to-season changes 2025->2026: +1 team,
  5 conference moves; 2026->2027: +1, -1, 27 conference moves (e.g. Gonzaga WCC -> Pac-12).

### Games

2025-26, all 6,318 rows, classified by whether each side's id is in `/teams?season=2026`:

| home D-I | away D-I | games |
|---|---|---|
| yes | yes | 5,767 |
| yes | no | 548 |
| no | yes | 3 |
| no | no | **0** |

- Non-D-I sides: `homeTeamId`/`awayTeamId` always present (0 null ids in either season), team name
  present, `homeConferenceId`/`homeConference` (and away) **null** (551 of 551). D-I sides never
  have a null conference (0 of 12,085). Every non-D-I id in games (363 distinct) exists in the
  unfiltered `/teams` list with `conference: null`.
- Non-D-I vs non-D-I games: none in either season.
- The 551 mixed games: 548 `final`, 2 `cancelled`, 1 `postponed`; all `seasonType: regular`;
  `conferenceGame: false`; 8 neutral; 2 inside in-season events (Chaminade in Maui).
- 268 of 365 D-I teams played at least one non-D-I game (1: 75 teams, 2: 115, 3: 69, 4: 9).
- **D-I losses to non-D-I in 2025-26: 6** (of 548 final):

| id | date (UTC) | home | pts | away | pts | neutral |
|---|---|---|---|---|---|---|
| 212745 | 2025-11-04 | Boise State | 78 | Hawai'i Pacific | 79 | false |
| 215288 | 2025-11-07 | Western Michigan | 81 | Northwood (MI) | 85 | false |
| 215294 | 2025-11-07 | Delaware | 62 | Wilmington (DE) | 71 | false |
| 213028 | 2025-11-11 | Gardner-Webb | 81 | North Greenville | 92 | false |
| 215445 | 2025-11-26 | Delaware State | 58 | Virginia St | 62 | true |
| 358414 | 2025-12-18 | Montana | 75 | Montana Tech | 82 | false |

- 2026-27 so far: 1,608 rows; D-I/D-I 1,265, D-I/non-D-I 343 (2 with non-D-I home), non-D-I/non-D-I
  0; 283 mixed games final with 4 D-I losses. Same representation (non-D-I conference null).

Sample mixed row (trimmed):

```json
{"id": 215243, "startDate": "2025-11-03T16:30:00.000Z", "seasonType": "regular", "gameType": "STD",
 "neutralSite": false, "conferenceGame": false, "status": "final",
 "homeTeamId": 76, "homeTeam": "East Texas A&M", "homeConferenceId": 27, "homeConference": "Southland",
 "homePoints": 119, "homeWinner": true,
 "awayTeamId": 742, "awayTeam": "Arlington Baptist", "awayConferenceId": null, "awayConference": null,
 "awayPoints": 60, "awayWinner": false}
```

### Exhibitions against non-D-I teams (uncertainty)

The source has no exhibition marker (no `preseason` rows in either season, and nothing before the
2025-11-03 opener despite querying from 2025-10-01). Some D-I vs non-D-I games may count for one side
only or be called exhibitions by one school. Example: Montana 75, Montana Tech 82 (id 358414) is
called a "road exhibition" by Montana Tech's athletics site while the API, ESPN and CBS list it as a
normal game; Boise State's loss to Hawai'i Pacific is reported as an official loss for Boise State.
Ingestion cannot tell these apart. Options for the owner: (a) trust the source (football parity:
CFBD games are taken as given); (b) remove disputed games with the B2-T2 override file case by case.

### Classification rule

```
d1_ids[S] = { t.id for t in GET /teams?season=S }        # one call per season
side_is_d1(game, side) = game[side + "TeamId"] in d1_ids[game.season]
non_d1_opponent       = exactly one side is D-I
# both sides non-D-I: never observed; drop the row (do not store) and count it in the ingest log
# cross-check (log a warning, do not branch on it): non-D-I side has <side>Conference == null,
#   D-I side has non-null <side>Conference
```

## 2. Season keying

`season` is the year the season ends: 2025-26 is `season: 2026`, `seasonLabel: "20252026"` on games.
`/teams/directory` reports `seasonLabel: "2025–26"` (en dash). 2026-27 is `season: 2027`,
`"20262027"`. The `season` query parameter on `/games` and `/teams` uses the same integer.

## 3. Season type, tournament, game type

2025-26 value counts (6,318 rows):

| `seasonType` | `tournament` | `gameType` | `conferenceGame` | rows | what it is |
|---|---|---|---|---|---|
| regular | null | STD | true | 3,373 | conference regular season |
| regular | null | STD | false | 2,321 | non-conference, incl. challenges and one-off events |
| regular | null | TRNMNT | false | 219 | in-season multi-team events (206 Nov, 13 Dec) |
| regular | null | TRNMNT | true | 300 | conference tournaments (2026-03-03 .. 2026-03-15) |
| postseason | NCAA | TRNMNT | false* | 67 | NCAA tournament incl. 4 First Four |
| postseason | NIT | TRNMNT | false* | 31 | NIT |
| postseason | null | TRNMNT | false | 7 | College Basketball Crown |

\* `conferenceGame` is false in national postseason even for same-conference pairs (3 cases).

- `seasonType` values seen: `regular`, `postseason` (enum also has `preseason`; 0 rows).
- `tournament` values seen: null, `NCAA`, `NIT`. Conference tournaments and the Crown have
  `tournament: null`. No CBI rows exist in 2025-26.
- `gameType`: `STD` 5,694, `TRNMNT` 624.
- `gameNotes` is free text (null on 5,455 rows): event names ("Southwest Maui Invitational",
  "ACC/SEC Men's Challenge"), conference-tournament rounds ("SEC Tournament - 1st Round"), NCAA rounds
  ("NCAA Men's Basketball Championship - Midwest Region - First Four"), NIT rounds ("NIT - 1st
  Round"), Crown rounds ("College Basketball Crown - Quarterfinals") and schedule notes ("Flex Game:
  1/13 or 1/14"). Not needed for any rule.
- Conference tournaments: all 300 are `regular`/`TRNMNT`/`conferenceGame: true` with both teams in the
  same conference; every one of 31 conferences has rows (Ivy 3 .. Big Ten 17). 269 neutral, 31 at home
  sites.
- No `regular` game falls after Selection Sunday (2026-03-15); the last is 2026-03-15 15:30 ET. The
  first `postseason` game is 2026-03-17 18:00 ET (First Four).

How to tell them apart:

```
national_postseason = seasonType == "postseason"          # NCAA (tournament=="NCAA"),
                                                          # NIT (tournament=="NIT"), Crown and any
                                                          # other national event (tournament null)
conference_tourney  = seasonType == "regular" and gameType == "TRNMNT" and conferenceGame
in_season_event     = seasonType == "regular" and gameType == "TRNMNT" and not conferenceGame
regular_season      = seasonType == "regular" and gameType == "STD"
```

Proposed rated rule:

```
is_rated = (seasonType == "regular"
            and game_date_et <= seasons.selection_sunday     # defensive guard; 0 rows affected in 2025-26
            and status == "final")                            # see section 4; or keep status separate
```

`is_rated` should be stored independent of status (rated-eligible), with "played" decided by
`status == "final"` at assembly; either works. Ambiguities: a future national event that the source
labels `regular` would be caught only by the date guard; a conference tournament game played after
Selection Sunday has never occurred. The Selection Sunday date is not in the API and must be stored in
`seasons` (2026: 2026-03-15, confirmed by the data; 2027: expected 2027-03-14, not verified here).

## 4. Status

Values seen: `final`, `postponed`, `cancelled`, `scheduled`, `in_progress` (2025-26: 6,300 final,
14 postponed, 4 cancelled; 2026-27: 1,240 final, 361 scheduled, 4 postponed, 3 in progress).

- Unplayed rows (`scheduled`, `in_progress`, `postponed`, `cancelled`) have `homePoints`/`awayPoints`
  **0** (3 scheduled rows had `null` for one or both), `homePeriodPoints: null`, `homeWinner: null`.
  A 0-0 rule would work today, but status is the reliable signal (as planned).
- **Postponed rows are never updated.** A rescheduled game is a new row with a new `id`; the old row
  stays `postponed` with 0-0. Example: Eastern Illinois v Blackburn, id 213650 `postponed` 2025-12-02,
  replayed as id 358658 `final` 2026-01-18 (105-49). All 14 postponed 2025-26 rows follow this pattern.
- `scheduled` can go stale: Maryland Eastern Shore v Neumann (id 375019, 2026-11-15) is still
  `scheduled` eight days later.
- Every `final` row has both scores, non-equal (0 ties, 0 zero scores), and winner flags.
- Exhibitions: none identifiable (see section 1).

Played rule: `played = status == "final"`. Everything else is ignored for rating and records.

## 5. Scores and overtime

- `homePoints`/`awayPoints`: integers. `homeWinner`/`awayWinner`: booleans on finals, consistent
  with points in 6,300 of 6,300; null otherwise.
- `homePeriodPoints`/`awayPeriodPoints`: list of per-half points then per-overtime points; sums equal
  the totals in 6,300 of 6,300. Length 2 = regulation (5,972), 3 = OT (277), 4 = 2OT (42), 5 = 3OT (9).
  Example: Kent State 97 Troy 103, `[34, 53, 10]` / `[42, 45, 16]`.
- `periods = len(homePeriodPoints)`; overtimes = periods - 2.

## 6. Neutral site and conference flags

- `neutralSite` (687 true in 2025-26): NCAA 67/67 true; NIT rounds 1-3 false (campus sites), semifinal
  and final true; Crown 7/7 true; Maui, Battle 4 Atlantis, Champions Classic, Jimmy V and CBS Sports
  Classic games all true. Plausible.
- Quirk: non-D-I host schools. Chaminade is listed as home team for Washington State's game in Pullman
  (id 213466, venue Beasley Coliseum, `neutralSite: false`), so home/away is inverted there, and for
  one Maui game (vs Texas, id 358311) `neutralSite` is false. These touch only mixed games, and
  alpha matters only when the D-I side loses (none of these). Overrides can fix them if needed.
- `conferenceGame` (3,673 true): true only when both teams share a conference (0 exceptions); false
  for the 3 same-conference national postseason games.

## 7. Dates

- `startDate`: ISO 8601 UTC with milliseconds, e.g. `"2025-11-04T00:00:00.000Z"`. It is true UTC:
  Kent State v Troy on opening night (Mon 2025-11-03, 7 pm ET) is `2025-11-04T00:00:00.000Z`; a Maui
  game at 6:59 pm HST is `2025-11-27T04:59:00.000Z` (2025-11-26 ET). 3,406 of 6,318 games (54%) have
  a UTC date different from the US/Eastern date, so `game_date` must be computed with
  `America/New_York`.
- `startTimeTbd: true` rows carry a placeholder of local midnight ET (`05:00:00.000Z` in November), so
  the ET conversion yields the intended date.
- 2025-26: first game 2025-11-03 08:00 ET (`2025-11-03T13:00:00.000Z`); last regular-season game
  2026-03-15 15:30 ET; last game overall 2026-04-06 ET (NCAA final, `2026-04-07T00:50:00.000Z`).
- Selection Sunday 2026: 2026-03-15. All 300 conference tournament games are on or before it.
- 2026-27: first game **Sunday 2026-11-01** 09:30 ET (Villanova v Notre Dame in Rome, "Eternal City
  Tip-Off"), the day before the main opening Monday (2026-11-02). With an anchor Monday of 2026-11-02
  this game falls before week 1. This is the "rescheduled opener" case in the B2 week tests. Options:
  (a) anchor = Monday of the week containing the first game (2026-10-26; week 1 then holds one game);
  (b) anchor = opening Monday, and games before it are folded into week 1. Owner/B2 to choose.
- 2025-26 weekly volume with anchor 2025-11-03 (all regular-season rows): 117 (holiday week 8) to 461
  (week 1); Selection Sunday falls in week 19.

## 8. Teams

Fields from `/teams`: `id`, `sourceId`, `school`, `mascot`, `abbreviation`, `displayName`,
`shortDisplayName`, `primaryColor`, `secondaryColor`, `currentVenueId`, `currentVenue`, `currentCity`,
`currentState`, `conferenceId`, `conference`, `logos`.

- `id` is stable across seasons: 2025 vs 2026 vs unfiltered lists differ only in
  `conferenceId`/`conference` for the same `id`. `sourceId` is the ESPN team id (numeric string for
  all D-I teams; 120 non-D-I teams have values like `"UNKNOWN#Cotner College"`).
- Names in games (`homeTeam`) equal `/teams` `school` for every row (0 mismatches).
- `school` is unique among D-I teams, and `abbreviation` is unique among D-I teams (2025-26). Across all
  teams there are 81 duplicated school names, one involving D-I: `Holy Cross` is both id 111
  (Crusaders, D-I) and id 1397 (Saints, non-D-I). Keying on id is required.
- Colors: hex without `#` (`"037961"`). 1 D-I team has null `primaryColor` (Le Moyne), 35 have null
  `secondaryColor`.
- Logos: `logos` is a list of 16 URLs on `cdn.collegefootballdata.com`, light and dark at sizes 500,
  256, 128, 96, 64, 48, 32, 16, keyed by `sourceId`:
  `https://cdn.collegefootballdata.com/cbb-logos/500/{sourceId}.png` and
  `https://cdn.collegefootballdata.com/cbb-logos-dark/500/{sourceId}.png`. All 365 D-I teams have
  logos. The CDN host was not reachable from this session's network, so the images themselves were
  not checked. Logo host for the site's `img-src`: `cdn.collegefootballdata.com`.
- Membership by season: `/conferences/history` (one call, 34 conferences) lists each conference's
  teams with `startSeason`/`endSeason` (`endSeason: null` = current), e.g. Delaware CAA 2002-2025,
  CUSA 2026-null. It agrees exactly with `/teams?season=S` for 2025-2027 and can rebuild any season's
  D-I set without a per-season `/teams` call (but lacks colors and logos).
- `/teams/directory?season=S` gives the same D-I ids and conferences in 67 KB (vs 495 KB) without
  colors or logos.

## 9. Volume and call budget

- 2025-26: 6,318 game rows (6,213 regular, 105 postseason), 0 duplicate `id`, 0 duplicate
  `sourceId`. Rated finals: 6,195 (5,647 D-I vs D-I, 548 D-I vs non-D-I). Each D-I team has 25-35
  D-I-vs-D-I rated games.
- `/games` returns at most 3,000 rows, oldest first: `/games?season=2026` with no dates returned
  exactly 3,000 rows ending 2026-01-06. Date filters are `startDateRange`/`endDateRange` (inclusive).
- Minimum calls for one full season: **4** = 1 `/teams?season=S` + 3 `/games` chunks (used here:
  2025-10-01..2025-12-08 = 1,922 rows; ..2026-01-31 = 2,253; ..2026-05-01 = 2,143). Adding
  `seasonType=regular` would drop 105 rows but no calls. Two-season backfill: 8 calls.
- Weekly incremental: **1** `/games` call for a date window (max 461 rows per week in 2025-26). A
  window covering the past 2-3 weeks still fits in one call and catches late status changes. Plus 1
  `/teams?season=S` call once per season (or when membership is in doubt). About 20-25 calls per season.
- History: `season=2003` returns games (93 rows on 2002-12-07, `seasonLabel: "20022003"`, with non-D-I
  opponents present as null-conference sides). Conference history goes back further (to 1906).

## 10. Surprises and risks for the schema

- Postponed games leave orphan rows; replays have new ids (section 4). Store them; they are filtered by
  status.
- Stale `scheduled` status on past games, and `null` points on a few scheduled rows: never derive
  played-ness or margins from points alone.
- Exhibition-like games against non-D-I teams are not marked (section 1).
- Non-D-I host schools can invert home/away (section 6).
- Name collision `Holy Cross` across divisions confirms id keying.
- First game of 2026-27 is on a Sunday before the opening Monday (section 7).
- No duplicate game ids, no null team ids, no non-D-I vs non-D-I games, no ties.

## Schema implications

Changes to the provisional schema in `plan.yaml`:

1. `teams`: keep `PRIMARY KEY (id, season)`, D-I only, populated from `/teams?season=S`. Add
   `source_id TEXT` (ESPN id; logos derive from it) and `conference_id INTEGER`; keep `conference` as
   the source's short name (`"Big Ten"`, `"Am. East"`, `"Indep."`). Store `logo` as the light 500 px URL
   and add `logo_dark` (the `cbb-logos-dark/500` URL). Colors stored without `#`; `primary_color` and
   `secondary_color` nullable.
2. `games.id` = CBBD `id` (integer). Add `source_id TEXT` (ESPN game id) for cross-checks.
3. `games.season_type` holds `regular`/`postseason`; `games.tournament` holds `NCAA`/`NIT`/null. Add
   `game_type TEXT` (`STD`/`TRNMNT`); it is the only field separating conference tournaments and
   in-season events from regular-season games. `game_notes TEXT` is optional (useful for display and
   debugging only).
4. `is_rated` per section 3 (`season_type == 'regular'` and ET date on or before
   `seasons.selection_sunday`); "played" is `status = 'final'`, as planned.
5. `games.status` domain: `scheduled`, `in_progress`, `final`, `postponed`, `cancelled`. Scores
   nullable; store the source's 0-0 on unplayed rows as NULL to keep "unplayed" unambiguous.
6. Keep `home_team_id`/`away_team_id` NOT NULL (never null in the source) and with no foreign key to
   `teams`, because non-D-I opponents are not in `teams`. Non-D-I is decided at assembly by absence from
   `teams` for that season, as planned. Keep `home_team`/`away_team` names for display of non-D-I
   opponents. Optional: `home_conference`/`away_conference` to make the null-conference cross-check
   queryable.
7. `games.periods` = `len(homePeriodPoints)` (2 = regulation).
8. `seasons`: `selection_sunday` must be entered (not in the API). `anchor_monday` needs a rule for a
   Sunday opener (section 7). Optional `season_label` is derivable (`2026` -> `2025-26`).
9. Rows where neither side is D-I: none observed; drop them at ingest and log a count.
10. Site CSP: `img-src` gains `https://cdn.collegefootballdata.com`.

## Corrections (2026-11-23, after review of the ingest)

Findings above are unchanged; these facts were found later and take precedence where they differ.

- Finals without scores DO occur in 2026-27. Two rows are `status: final` with `homePoints` and
  `awayPoints` both 0 (ids 374609, North Carolina A&T vs Warren Wilson, and 375008,
  Bethune-Cookman vs Paine), both against non-D-I schools. Ingest stores them with no result and
  `is_rated = 0` and counts them as the `final_tie` anomaly.
- Same-pair, same-day duplicate rows can occur: ids 372907 and 378701 are both Loyola Chicago vs
  Seattle U on 2026-11-23 (ET), listed with the home side reversed. Both were `scheduled` when saved
  (so neither was a double count yet); ingest keeps only the larger id rated if both ever become
  played.
- The source's site quirks (a game listed at the wrong side, or a neutral-site event marked not
  neutral; for example 213466 and 358311 in 2025-26) are fixed through `site` overrides
  (`ingest/game_result_overrides.json`), not by ingest.
