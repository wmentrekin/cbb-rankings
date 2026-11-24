"""The weekly orchestration with the local sqlite store, replayed API fixtures and a fake S3."""
import json
import re
from datetime import date, datetime, timezone

import pytest

import main
from ingest import overrides as overrides_module
from ingest import run as ingest_run
from ingest import weeks as weeks_module
from ingest.http import QuotaFloorError
from main import Options
from model.pipeline import NoTeamsError
from model.solve import ModelSolveError
from tests.fake_s3 import FakeS3
from tests.fakes import FakeApi, load, season_2026_api, season_2027_api

BUCKET = "test-bucket"
AFTER_2026 = date(2026, 4, 6)  # a Monday after the 2025-26 season (weeks 1-19 have all ended)
MONDAY_2026_11_23 = date(2026, 11, 23)  # week 3 of 2026-27 (anchor 2026-11-02) has just ended
R2_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")


@pytest.fixture(autouse=True)
def small_opening_day(monkeypatch):
    """The trimmed fixtures hold a handful of games per day, not the 20+ of a real opening day."""
    original = weeks_module.compute_anchor_monday
    monkeypatch.setattr(ingest_run, "compute_anchor_monday", lambda dates: original(dates, min_games=3))


@pytest.fixture(autouse=True)
def no_r2_in_env(monkeypatch):
    for name in R2_ENV:
        monkeypatch.delenv(name, raising=False)


def both_seasons_api() -> FakeApi:
    a26, a27 = season_2026_api(), season_2027_api()
    return FakeApi({**a26.teams, **a27.teams}, {**a26.games, **a27.games})


def go(opts, store, api=None, s3=None, today=AFTER_2026, now=None):
    lines: list[str] = []
    code = main.run(
        opts, store=store, api=api, r2=(s3, BUCKET) if s3 is not None else None, today=today,
        now=now or (lambda: datetime(2026, 4, 6, 10, 0, tzinfo=timezone.utc)), out=lines.append,
    )
    return code, lines


def count(store, table="ratings", where="1"):
    return store.query(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}")[0]["n"]


def markers(lines):
    return [ln.split(":")[0].split(" ")[0] for ln in lines if re.match(r"^[A-Z_]+[: ]", ln)]


def strip_stamp(obj):
    return {k: v for k, v in obj.items() if k != "generated_at_utc"}


def validate_week_payload(payload, season, week, teams):
    assert list(payload) == ["season", "week", "generated_at_utc", "rankings"]
    assert (payload["season"], payload["week"]) == (season, week)
    assert datetime.fromisoformat(payload["generated_at_utc"]).tzinfo is not None
    rows = payload["rankings"]
    assert len(rows) == teams
    for i, row in enumerate(rows, 1):
        assert list(row) == ["rank", "team", "conference", "logo", "record", "rating", "delta"]
        assert row["rank"] == i and isinstance(row["rank"], int)
        assert isinstance(row["team"], str) and isinstance(row["conference"], str)
        assert row["logo"] is None or row["logo"].startswith("http")
        assert re.fullmatch(r"\d+-\d+", row["record"])
        assert isinstance(row["rating"], float) and round(row["rating"], 2) == row["rating"]
        assert isinstance(row["delta"], int)
    ratings = [r["rating"] for r in rows]
    assert ratings == sorted(ratings, reverse=True)


def validate_index(index, expected):
    assert list(index) == ["generated_at_utc", "latest_season", "latest_week", "seasons"]
    assert index["seasons"] == [{"season": s, "weeks": w} for s, w in expected]
    assert index["latest_season"] == expected[0][0] and index["latest_week"] == expected[0][1][-1]


# -- happy path ------------------------------------------------------------------------------------

def test_happy_path_ingests_rates_writes_and_publishes(store):
    api, s3 = season_2026_api(), FakeS3()
    code, lines = go(Options(season=2026, week=10), store, api, s3)
    assert code == 0 and lines[-1] == "WEEKLY_OK season=2026 week=10"
    assert api.calls == 2 and count(store, "games") == 40  # ingest ran first
    assert count(store) == 59 and count(store, "ratings_staging") == 0
    rows = store.query("SELECT * FROM ratings WHERE team_id = 52 OR team_id = 77")
    assert all(r["season"] == 2026 and r["week"] == 10 for r in rows)
    assert s3.puts == [
        "cbb/rankings/2026/week-10.json", "cbb/rankings/2026/latest.json", "cbb/rankings/latest.json", "cbb/rankings/index.json",
    ]
    validate_week_payload(s3.json("cbb/rankings/2026/week-10.json"), 2026, 10, 59)
    assert s3.json("cbb/rankings/latest.json") == s3.json("cbb/rankings/2026/week-10.json")
    validate_index(s3.json("cbb/rankings/index.json"), [(2026, [10])])
    text = "\n".join(lines)
    for needle in ("api_calls: 2 (quota remaining 898)", "d1_rows_written_ingest: 101", "teams_rated: 59", "model_status: optimal",
                   "solve_seconds:", "margin_median_and_k:", "capped_games:", "teams_at_floor:", "n_flat_teams:", "r_lower:",
                   "ratings_rows_written: 59", "artifact_keys:", "ingest_anomalies: none", "ingest_warnings: none"):
        assert needle in text, needle


def test_stored_ratings_match_the_published_artifact(store):
    s3 = FakeS3()
    go(Options(season=2026, week=10), store, season_2026_api(), s3)
    stored = {r["school"]: r for r in store.query(
        "SELECT t.school, r.rating, r.wins, r.losses FROM ratings r JOIN teams t ON t.id = r.team_id AND t.season = r.season")}
    for row in s3.json("cbb/rankings/2026/week-10.json")["rankings"]:
        s = stored[row["team"]]
        assert row["rating"] == round(s["rating"], 2) and row["record"] == f"{s['wins']}-{s['losses']}"


def test_main_entry_point_parses_arguments(store):
    s3 = FakeS3()
    code = main.main(["--season", "2026", "--week", "10"], store=store, api=season_2026_api(), r2=(s3, BUCKET), today=AFTER_2026)
    assert code == 0 and count(store) == 59 and len(s3.puts) == 4


# -- failure paths: stop at the right point, print the marker, exit non-zero --------------------------

class Tripwire:
    """Replaces `rate_week` or `write_ratings`; fails the test if a later step runs."""
    def __init__(self):
        self.called = False

    def __call__(self, *a, **k):
        self.called = True
        raise AssertionError("a step after the failure ran")


def assert_stopped(store, s3, lines, marker, *, ratings=0):
    assert any(ln.startswith(f"{marker}:") for ln in lines)
    assert not any(ln.startswith("WEEKLY_OK") for ln in lines)
    assert count(store) == ratings and count(store, "ratings_staging") == 0
    assert s3.puts == []


def test_ingest_gate_failure_stops_before_model_and_publish(store, monkeypatch):
    trip = Tripwire()
    monkeypatch.setattr(main, "rate_week", trip)
    s3 = FakeS3()
    api = FakeApi({2026: []}, {2026: load("cbbd_games_2026.json")})  # /teams returns nothing
    code, lines = go(Options(season=2026, week=10), store, api, s3)
    assert code == 1 and not trip.called
    assert_stopped(store, s3, lines, "INGEST_FAILURE")
    assert "no teams for season 2026" in "\n".join(lines)


def test_ingest_exception_is_an_ingest_failure(store, monkeypatch):
    class Dead:
        calls, quota_remaining = 0, None

        def get(self, path, params=None):
            raise QuotaFloorError("quota 10 is below the floor 50")

    trip = Tripwire()
    monkeypatch.setattr(main, "rate_week", trip)
    s3 = FakeS3()
    code, lines = go(Options(season=2026, week=10), store, Dead(), s3)
    assert code == 1 and not trip.called
    assert_stopped(store, s3, lines, "INGEST_FAILURE")
    assert "QuotaFloorError" in "\n".join(lines)


def test_override_failure_stops_before_the_model(store, monkeypatch, tmp_path):
    bad = tmp_path / "overrides.json"
    bad.write_text(json.dumps({"version": 1, "overrides": [{
        "kind": "exclude", "game_id": 999999999, "season": 2026, "home_team_id": 1, "away_team_id": 2, "reason": "no such game"}]}))
    monkeypatch.setattr(overrides_module, "DEFAULT_OVERRIDES_PATH", bad)
    trip = Tripwire()
    monkeypatch.setattr(main, "rate_week", trip)
    s3 = FakeS3()
    code, lines = go(Options(season=2026, week=10), store, season_2026_api(), s3)
    assert code == 1 and not trip.called
    assert_stopped(store, s3, lines, "OVERRIDE_FAILURE")
    assert "game_id=999999999" in "\n".join(lines)


def test_malformed_override_file_is_an_override_failure(store, monkeypatch, tmp_path):
    bad = tmp_path / "overrides.json"
    bad.write_text("{not json")
    monkeypatch.setattr(overrides_module, "DEFAULT_OVERRIDES_PATH", bad)
    s3 = FakeS3()
    code, lines = go(Options(season=2026, week=10), store, season_2026_api(), s3)
    assert code == 1
    assert_stopped(store, s3, lines, "OVERRIDE_FAILURE")


@pytest.mark.parametrize("error", [ModelSolveError("status infeasible"), NoTeamsError("no teams stored")])
def test_model_error_writes_and_publishes_nothing(store, monkeypatch, error):
    def boom(*a, **k):
        raise error

    monkeypatch.setattr(main, "rate_week", boom)
    trip = Tripwire()
    monkeypatch.setattr(main, "write_ratings", trip)
    s3 = FakeS3()
    code, lines = go(Options(season=2026, week=10), store, season_2026_api(), s3)
    assert code == 1 and not trip.called
    assert_stopped(store, s3, lines, "MODEL_FAILURE")
    assert type(error).__name__ in "\n".join(lines)


def test_missing_prior_season_fails_in_weeks_1_to_6(store, monkeypatch):
    trip = Tripwire()
    monkeypatch.setattr(main, "rate_week", trip)
    s3 = FakeS3()
    code, lines = go(Options(season=2026, week=3), store, season_2026_api(), s3)
    assert code == 1 and not trip.called
    assert_stopped(store, s3, lines, "PRIOR_FAILURE")
    assert "Backfill season 2025" in "\n".join(lines)


def test_missing_prior_season_fails_the_scheduled_run_too(store):
    s3 = FakeS3()
    code, lines = go(Options(), store, season_2027_api(), s3, today=MONDAY_2026_11_23)
    assert code == 1 and any(ln.startswith("PRIOR_FAILURE:") and "Backfill season 2026" in ln for ln in lines)
    assert count(store, "games") > 0 and count(store) == 0 and s3.puts == []  # ingested, then stopped


def test_allow_missing_prior_rates_without_priors(store):
    s3 = FakeS3()
    code, lines = go(Options(season=2026, week=3, allow_missing_prior=True), store, season_2026_api(), s3)
    assert code == 0 and count(store) == 59 and len(s3.puts) == 4
    text = "\n".join(lines)
    assert "tolerated by --allow-missing-prior" in text and "no prior-season data for 2025" in text


def test_a_range_covering_early_weeks_fails_before_writing_any_week(store):
    s3 = FakeS3()
    code, lines = go(Options(season=2026, weeks=(5, 9)), store, season_2026_api(), s3)
    assert code == 1
    assert_stopped(store, s3, lines, "PRIOR_FAILURE")


def test_publish_failure_raises_marker_exits_nonzero_and_keeps_ratings(store):
    s3 = FakeS3(fail_put_on="2026/latest.json")
    code, lines = go(Options(season=2026, week=10), store, season_2026_api(), s3)
    assert code == 1
    assert any(ln.startswith("PUBLISH_FAILURE:") and "not (fully) published" in ln for ln in lines)
    assert not any(ln.startswith("WEEKLY_OK") for ln in lines)
    assert count(store) == 59  # ratings stay written; a rerun republishes
    assert "cbb/rankings/index.json" not in s3.objects


def test_listing_failure_is_a_publish_failure(store):
    code, lines = go(Options(season=2026, week=10), store, season_2026_api(), FakeS3(fail_list=True))
    assert code == 1 and any(ln.startswith("PUBLISH_FAILURE:") for ln in lines)


def test_unconfigured_r2_fails_before_any_api_call_or_write(store):
    api = season_2026_api()
    code, lines = go(Options(season=2026, week=10), store, api, s3=None)
    assert code == 1 and api.calls == 0
    assert any(ln.startswith("PUBLISH_FAILURE:") and "R2_BUCKET_NAME" in ln for ln in lines)
    assert count(store, "games") == 0 and count(store) == 0


def test_a_write_failure_stops_before_publishing(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("D1 is down")

    monkeypatch.setattr(main, "write_ratings", boom)
    s3 = FakeS3()
    code, lines = go(Options(season=2026, week=10), store, season_2026_api(), s3)
    assert code == 1
    assert_stopped(store, s3, lines, "WRITE_FAILURE")


def test_an_unexpected_exception_is_a_weekly_failure(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("surprise")

    monkeypatch.setattr(main, "get_season_row", boom)
    code, lines = go(Options(season=2026, week=10), store, season_2026_api(), FakeS3())
    assert code == 1 and lines == ["WEEKLY_FAILURE: RuntimeError: surprise"]


# -- no-op Mondays ------------------------------------------------------------------------------------

@pytest.mark.parametrize("today, code", [
    (date(2026, 7, 13), "outside_season"),    # mid-summer
    (date(2026, 10, 26), "outside_season"),   # before the 2026-27 window opens
    (date(2026, 3, 23), "outside_season"),    # the Monday after the last week was published
    (date(2026, 4, 6), "outside_season"),     # April
    (date(2030, 1, 7), "season_not_configured"),
])
def test_out_of_season_mondays_do_nothing_and_spend_no_api_call(store, today, code):
    api, s3 = both_seasons_api(), FakeS3()
    exit_code, lines = go(Options(), store, api, s3, today=today)
    assert exit_code == 0 and len(lines) == 1 and lines[0].startswith(f"WEEKLY_NOOP: {code}: ")
    assert api.calls == 0 and s3.puts == []
    assert all(count(store, t) == 0 for t in ("games", "teams", "seasons", "ratings", "ratings_staging"))


def test_a_known_season_whose_week_one_has_not_ended_is_a_no_call_noop(store):
    ingest_run.ingest_season(store, 2027, season_2027_api())  # the season row exists now
    api, s3 = season_2027_api(), FakeS3()
    code, lines = go(Options(), store, api, s3, today=date(2026, 11, 2))  # anchor Monday itself
    assert code == 0 and lines[0].startswith("WEEKLY_NOOP: no_week_to_publish: ")
    assert api.calls == 0 and s3.puts == [] and count(store) == 0


def test_a_brand_new_season_is_ingested_first_then_resolved(store):
    api, s3 = season_2027_api(), FakeS3()
    code, lines = go(Options(), store, api, s3, today=date(2026, 11, 2))
    assert code == 0 and api.calls == 2  # no row yet: the ingest (teams + games) was the only way to know
    assert any(ln.startswith("WEEKLY_NOOP: no_week_to_publish: ") for ln in lines)
    assert count(store, "seasons") == 1 and count(store) == 0 and s3.puts == []


def test_selection_sunday_week_is_published_on_the_monday_after(store):
    s3 = FakeS3()
    code, lines = go(Options(), store, season_2026_api(), s3, today=date(2026, 3, 16))
    assert code == 0 and lines[-1] == "WEEKLY_OK season=2026 week=19"
    assert s3.json("cbb/rankings/index.json")["latest_week"] == 19


def test_explicit_season_not_in_seasons_json_is_an_error_not_a_noop(store):
    code, lines = go(Options(season=2031, week=1), store, FakeApi({}, {}), FakeS3())
    assert code == 2 and lines[0].startswith("WEEKLY_FAILURE: ")


@pytest.mark.parametrize("opts, fragment", [
    (Options(season=2026, week=20), "weeks 20-20"),     # after the Selection Sunday week
    (Options(season=2026, weeks=(1, 25)), "weeks 1-25"),
    (Options(season=2026, week=19), "weeks 19-19"),     # today (2026-02-01) is before week 19 has ended
])
def test_unpublishable_weeks_are_refused(store, opts, fragment):
    s3 = FakeS3()
    code, lines = go(opts, store, season_2026_api(), s3, today=date(2026, 4, 6) if opts.week != 19 else date(2026, 2, 1))
    assert code == 2 and fragment in lines[-1] and lines[-1].startswith("WEEKLY_FAILURE:")
    assert count(store) == 0 and s3.puts == []


# -- staging -------------------------------------------------------------------------------------------

def test_staging_writes_only_ratings_staging_and_publishes_nothing(store):
    s3 = FakeS3()
    code, lines = go(Options(season=2026, week=10, staging=True), store, season_2026_api(), s3=None)  # no R2 needed
    assert code == 0 and lines[-1] == "WEEKLY_OK season=2026 week=10"
    assert count(store) == 0 and count(store, "ratings_staging") == 59 and s3.puts == []
    assert "none (staging, not published)" in "\n".join(lines)


def test_publish_staging_uses_the_staging_prefix_and_staging_ratings_and_never_production(store):
    production = {"cbb/rankings/2026/week-05.json": b"prod", "cbb/rankings/latest.json": b"prod", "cbb/rankings/index.json": b"prod"}
    s3 = FakeS3(production)
    code, _ = go(Options(season=2026, week=10, staging=True, publish_staging=True), store, season_2026_api(), s3)
    assert code == 0 and count(store) == 0
    assert s3.puts and all(k.startswith("cbb-staging/rankings/") for k in s3.puts)
    assert {k: s3.objects[k] for k in production} == production
    validate_week_payload(s3.json("cbb-staging/rankings/2026/week-10.json"), 2026, 10, 59)
    validate_index(s3.json("cbb-staging/rankings/index.json"), [(2026, [10])])


# -- ratings-only range mode, idempotency, stale rows ---------------------------------------------------

def test_range_mode_ingests_once_and_publishes_each_week_in_order(store):
    api, s3 = season_2026_api(), FakeS3()
    code, lines = go(Options(season=2026, weeks=(9, 11)), store, api, s3)
    assert code == 0 and api.calls == 2  # ONE ingest
    assert [ln for ln in lines if ln.startswith("WEEKLY_OK")] == [f"WEEKLY_OK season=2026 week={w}" for w in (9, 10, 11)]
    assert lines[-1].startswith("RANGE_DONE season=2026 weeks=9-11 ratings_rows_written=177")
    assert count(store) == 177
    validate_index(s3.json("cbb/rankings/index.json"), [(2026, [9, 10, 11])])
    assert s3.json("cbb/rankings/latest.json")["week"] == 11
    assert all(r["delta"] == 0 for r in s3.json("cbb/rankings/2026/week-09.json")["rankings"])


def test_all_weeks_then_no_ingest_rerun_writes_nothing_new(store):
    api, s3 = season_2026_api(), FakeS3()
    code, lines = go(Options(season=2026, all_weeks=True, allow_missing_prior=True), store, api, s3)
    assert code == 0 and count(store) == 19 * 59
    validate_index(s3.json("cbb/rankings/index.json"), [(2026, list(range(1, 20)))])
    before = {k: json.loads(v) for k, v in s3.objects.items()}
    ratings_before = store.query("SELECT * FROM ratings ORDER BY season, week, team_id")
    puts_before = len(s3.puts)

    later = lambda: datetime(2026, 4, 13, 10, 0, tzinfo=timezone.utc)  # noqa: E731
    code, lines = go(Options(season=2026, all_weeks=True, allow_missing_prior=True, no_ingest=True), store, api, s3, now=later)
    assert code == 0 and api.calls == 2  # no new API calls
    assert [ln for ln in lines if ln.startswith("  ratings_rows_written:")] == ["  ratings_rows_written: 0"] * 19
    assert store.query("SELECT * FROM ratings ORDER BY season, week, team_id") == ratings_before
    # artifacts are re-uploaded (a rerun is a republish), identical apart from generated_at_utc
    assert len(s3.puts) > puts_before
    after = {k: json.loads(v) for k, v in s3.objects.items()}
    assert set(after) == set(before)
    assert {k: strip_stamp(v) for k, v in after.items()} == {k: strip_stamp(v) for k, v in before.items()}
    assert after["cbb/rankings/2026/week-19.json"]["generated_at_utc"] == later().isoformat()


def test_stale_rows_of_the_same_season_week_are_removed_and_other_weeks_kept(store):
    s3 = FakeS3()
    go(Options(season=2026, week=10), store, season_2026_api(), s3)
    store.upsert("ratings", [
        {"team_id": 999999, "season": 2026, "week": 10, "rating": 5.0, "wins": 1, "losses": 1},   # stale: not a team any more
        {"team_id": 999999, "season": 2026, "week": 4, "rating": 5.0, "wins": 1, "losses": 1},    # other week: untouched
        {"team_id": 999999, "season": 2025, "week": 10, "rating": 5.0, "wins": 1, "losses": 1},   # other season: untouched
    ], ("team_id", "season", "week"))
    code, lines = go(Options(season=2026, week=10, no_ingest=True), store, None, s3)
    assert code == 0 and "  ratings_rows_written: 1" in lines  # the delete
    assert count(store, where="team_id = 999999 AND season = 2026 AND week = 10") == 0
    assert count(store, where="team_id = 999999") == 2
    assert count(store, where="season = 2026 AND week = 10") == 59
    assert all(r["team"] != "999999" for r in s3.json("cbb/rankings/2026/week-10.json")["rankings"])


def test_write_ratings_refuses_unknown_tables(store):
    with pytest.raises(ValueError):
        main.write_ratings(store, "games", 2026, 1, [])


def test_dry_run_reports_and_writes_nothing(store):
    s3 = FakeS3()
    go(Options(season=2026, week=9), store, season_2026_api(), s3)
    puts, ratings = len(s3.puts), store.query("SELECT * FROM ratings")
    code, lines = go(Options(season=2026, weeks=(9, 10), no_ingest=True, dry_run=True), store, None, None)
    assert code == 0 and len(s3.puts) == puts and store.query("SELECT * FROM ratings") == ratings
    text = "\n".join(lines)
    assert "WEEKLY_DRY_RUN season=2026 week=9" in text and "WEEKLY_DRY_RUN season=2026 week=10" in text
    assert "'unchanged': 59" in text and "'new': 59" in text  # week 9 is stored and unchanged, week 10 is new
    assert "cbb/rankings/2026/week-10.json" in text and "WEEKLY_OK" not in text
    assert count(store, where="week = 10") == 0


# -- the Monday with a stored prior season ----------------------------------------------------------------

def test_monday_with_the_prior_season_present_runs_end_to_end(store):
    api, s3 = both_seasons_api(), FakeS3()
    assert go(Options(season=2026, all_weeks=True, allow_missing_prior=True), store, api, s3)[0] == 0
    code, lines = go(Options(), store, api, s3, today=MONDAY_2026_11_23)
    assert code == 0 and lines[-1] == "WEEKLY_OK season=2027 week=3"
    assert count(store, where="season = 2027 AND week = 3") == 22
    assert "  model_warnings: none" in lines and not any("PRIOR" in ln for ln in lines)
    validate_week_payload(s3.json("cbb/rankings/2027/week-03.json"), 2027, 3, 22)
    validate_index(s3.json("cbb/rankings/index.json"), [(2027, [3]), (2026, list(range(1, 20)))])
    assert s3.json("cbb/rankings/latest.json")["season"] == 2027 == s3.json("cbb/rankings/2027/latest.json")["season"]
    assert s3.json("cbb/rankings/2026/latest.json")["week"] == 19


# -- command line ----------------------------------------------------------------------------------------

def test_a_store_that_cannot_be_opened_is_a_weekly_failure(monkeypatch, capsys):
    def boom():
        raise RuntimeError("missing environment variables: CLOUDFLARE_API_TOKEN")

    monkeypatch.setattr(main, "get_store", boom)
    assert main.main(["--season", "2026", "--week", "10"]) == 1
    assert capsys.readouterr().out.startswith("WEEKLY_FAILURE: RuntimeError: missing environment variables")


def test_parse_options():
    o = main.parse_options(["--season", "2026", "--weeks", "3-7", "--no-ingest", "--dry-run", "--allow-missing-prior"])
    assert (o.season, o.weeks, o.no_ingest, o.dry_run, o.allow_missing_prior, o.range_mode) == (2026, (3, 7), True, True, True, True)
    assert main.parse_options(["--season", "2026", "--weeks", "5"]).weeks == (5, 5)
    assert main.parse_options(["--staging", "--publish-staging"]).prefix == "cbb-staging/"
    assert main.parse_options([]).prefix == "cbb/" and main.parse_options([]).table == "ratings"
    assert main.parse_options(["--staging"]).table == "ratings_staging" and not main.parse_options(["--staging"]).publishes


@pytest.mark.parametrize("argv", [
    ["--weeks", "3-7"],                                         # range mode needs a season
    ["--all-weeks"],
    ["--season", "2026", "--week", "3", "--weeks", "3-4"],
    ["--season", "2026", "--weeks", "3-4", "--all-weeks"],
    ["--season", "2026", "--weeks", "7-3"],
    ["--season", "2026", "--weeks", "0-3"],
    ["--season", "2026", "--weeks", "a-b"],
    ["--publish-staging"],
    ["--season", "2026", "--dry-run"],                          # a dry run must not ingest
    ["--week", "0"],
])
def test_invalid_argument_combinations_exit_2(argv):
    with pytest.raises(SystemExit) as exc:
        main.parse_options(argv)
    assert exc.value.code == 2
