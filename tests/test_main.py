"""The weekly orchestration with the local sqlite store, replayed API fixtures and a fake S3."""
import json
import re
from datetime import date, datetime, timedelta, timezone

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
                   "solve_seconds:", "margin_median_and_k:", "capped_games:", "raw_rating_range:", "display_shift:", "n_flat_teams:", "r_lower:",
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


def test_missing_prior_season_fails_while_the_prior_weight_is_positive(store, monkeypatch):
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


def test_the_prior_requirement_follows_the_configured_fade_week(store):
    from model.config import BASKETBALL_DEFAULT

    last = BASKETBALL_DEFAULT.prior_fade_weeks - 1  # 9: the last week with prior weight > 0
    code, lines = go(Options(season=2026, week=last), store, season_2026_api(), FakeS3())
    assert code == 1 and any(ln.startswith("PRIOR_FAILURE:") for ln in lines)
    code, _ = go(Options(season=2026, week=last + 1), store, season_2026_api(), FakeS3())
    assert code == 0


def test_stored_ratings_are_published_on_the_shifted_scale(store):
    go(Options(season=2026, week=10), store, season_2026_api(), FakeS3())
    ratings = [r["rating"] for r in store.query("SELECT rating FROM ratings WHERE season = 2026 AND week = 10")]
    assert min(ratings) == 0.01 and len(set(ratings)) > 1


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
    (date(2030, 7, 8), "outside_season"),     # an unconfigured season, but not in its months
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
    s3, api = FakeS3(), season_2026_api()
    assert go(Options(season=2026, weeks=(1, 18), allow_missing_prior=True), store, api, s3)[0] == 0  # nothing to catch up
    code, lines = go(Options(allow_missing_prior=True), store, api, s3, today=date(2026, 3, 16))
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
    code, lines = go(Options(season=2026, weeks=(10, 12)), store, api, s3)  # week 10: the first without a prior
    assert code == 0 and api.calls == 2  # ONE ingest
    assert [ln for ln in lines if ln.startswith("WEEKLY_OK")] == [f"WEEKLY_OK season=2026 week={w}" for w in (10, 11, 12)]
    assert lines[-1].startswith("RANGE_DONE season=2026 weeks=10-12 ratings_rows_written=177")
    assert count(store) == 177
    validate_index(s3.json("cbb/rankings/index.json"), [(2026, [10, 11, 12])])
    assert s3.json("cbb/rankings/latest.json")["week"] == 12
    assert all(r["delta"] == 0 for r in s3.json("cbb/rankings/2026/week-10.json")["rankings"])


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
    go(Options(season=2026, week=10), store, season_2026_api(), s3)
    puts, ratings = len(s3.puts), store.query("SELECT * FROM ratings")
    code, lines = go(Options(season=2026, weeks=(10, 11), no_ingest=True, dry_run=True), store, None, None)
    assert code == 0 and len(s3.puts) == puts and store.query("SELECT * FROM ratings") == ratings
    text = "\n".join(lines)
    assert "WEEKLY_DRY_RUN season=2026 week=10" in text and "WEEKLY_DRY_RUN season=2026 week=11" in text
    assert "'unchanged': 59" in text and "'new': 59" in text  # week 10 is stored and unchanged, week 11 is new
    assert "cbb/rankings/2026/week-11.json" in text and "WEEKLY_OK" not in text
    assert count(store, where="week = 11") == 0


# -- the Monday with a stored prior season ----------------------------------------------------------------

def test_monday_with_the_prior_season_present_runs_end_to_end(store):
    api, s3 = both_seasons_api(), FakeS3()
    assert go(Options(season=2026, all_weeks=True, allow_missing_prior=True), store, api, s3)[0] == 0
    assert go(Options(season=2027, weeks=(1, 2)), store, api, s3, today=MONDAY_2026_11_23)[0] == 0  # weeks 1-2 are stored
    code, lines = go(Options(), store, api, s3, today=MONDAY_2026_11_23)
    assert code == 0 and lines[-1] == "WEEKLY_OK season=2027 week=3"
    assert "catch_up_weeks" not in "\n".join(lines)
    assert count(store, where="season = 2027 AND week = 3") == 22
    assert "  model_warnings: none" in lines and not any("PRIOR" in ln for ln in lines)
    validate_week_payload(s3.json("cbb/rankings/2027/week-03.json"), 2027, 3, 22)
    validate_index(s3.json("cbb/rankings/index.json"), [(2027, [1, 2, 3]), (2026, list(range(1, 20)))])
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


# -- the calendar decides the season: an unconfigured one fails in its months ------------------------------

class NoApi:
    """Any call is a bug: the day must resolve before the API is touched."""
    calls = 0
    quota_remaining = None

    def get(self, path, params=None):
        raise AssertionError("an API call was made")


def days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def test_an_unconfigured_season_fails_every_day_from_november_through_march_and_noops_otherwise(store):
    """2028-29 and 2029-30 are not in seasons.json: walk a whole year (2028-04-01 .. 2029-03-31)."""
    seen = {"failure": 0, "noop": 0, "warning": 0}
    for today in days(date(2028, 4, 1), date(2029, 3, 31)):
        code, lines = go(Options(), store, NoApi(), FakeS3(), today=today)
        in_months = today.month >= 11 or today.month <= 3
        if in_months:
            assert code == 1, today
            assert len(lines) == 1 and lines[0].startswith("WEEKLY_FAILURE: "), today
            season = today.year + 1 if today.month >= 11 else today.year
            assert "ingest/seasons.json" in lines[0] and f'add "{season}": ' in lines[0] and "selection_sunday" in lines[0]
            seen["failure"] += 1
        else:
            assert code == 0, today
            assert lines[-1].startswith("WEEKLY_NOOP: outside_season: "), today
            if today.month in (9, 10):
                assert len(lines) == 2 and lines[0].startswith("WEEKLY_WARNING: season 2029 ") and "ingest/seasons.json" in lines[0], today
                seen["warning"] += 1
            else:
                assert len(lines) == 1, today  # April-August: nothing to say
            seen["noop"] += 1
    assert seen == {"failure": 151, "noop": 214, "warning": 61}
    assert all(count(store, t) == 0 for t in ("games", "teams", "seasons", "ratings"))


def test_a_configured_season_never_fails_for_missing_configuration_across_a_year(store):
    """2025-26 and 2026-27 are configured: no day of 2026-04-01 .. 2027-10-31 may be a failure for
    that reason, and the off-season days are quiet no-ops."""
    ingest_run.ingest_season(store, 2027, season_2027_api())  # the season row exists, so no day needs an API call
    for today in days(date(2026, 4, 1), date(2026, 10, 31)):
        code, lines = go(Options(), store, NoApi(), FakeS3(), today=today)
        assert code == 0 and lines == [lines[0]] and lines[0].startswith("WEEKLY_NOOP: outside_season: "), today
    for today in days(date(2026, 11, 1), date(2026, 11, 8)):  # the season's first days: week 1 has not ended
        code, lines = go(Options(), store, NoApi(), FakeS3(), today=today)
        assert code == 0 and lines[0].startswith("WEEKLY_NOOP: no_week_to_publish: "), today
    for today in days(date(2027, 3, 22), date(2027, 7, 31)):  # after the last week was published
        code, lines = go(Options(), store, NoApi(), FakeS3(), today=today)
        assert code == 0 and lines == [lines[0]] and lines[0].startswith("WEEKLY_NOOP: outside_season: "), today
    for today in days(date(2027, 8, 1), date(2027, 8, 31)):  # season 2028 is unconfigured, but it is August
        code, lines = go(Options(), store, NoApi(), FakeS3(), today=today)
        assert code == 0 and len(lines) == 1 and lines[0].startswith("WEEKLY_NOOP: outside_season: "), today
    assert all(count(store, t) == 0 for t in ("ratings", "ratings_staging"))


def test_the_upcoming_season_warning_is_visible_but_not_a_failure(store):
    code, lines = go(Options(), store, NoApi(), FakeS3(), today=date(2027, 9, 6))  # season 2028 is not configured
    assert code == 0
    assert lines[0].startswith("WEEKLY_WARNING: season 2028 starts in November and is not in ingest/seasons.json")
    assert '"2028": {"selection_sunday": "YYYY-MM-DD"' in lines[0] and lines[1].startswith("WEEKLY_NOOP:")
    code, lines = go(Options(), store, NoApi(), FakeS3(), today=date(2026, 9, 7))  # season 2027 is configured: silent
    assert code == 0 and len(lines) == 1


def test_an_unconfigured_season_named_by_a_week_flag_is_a_usage_error_even_off_season(store):
    code, lines = go(Options(week=3), store, NoApi(), FakeS3(), today=date(2030, 7, 8))
    assert code == 2 and lines[0].startswith("WEEKLY_FAILURE: season 2030 is not in")


# -- catch-up: weeks without ratings are rated and published first -------------------------------------------

def primed_with_2026(store):
    api, s3 = both_seasons_api(), FakeS3()
    assert go(Options(season=2026, all_weeks=True, allow_missing_prior=True), store, api, s3)[0] == 0
    return api, s3


def weeks_in(store, season=2027, table="ratings"):
    return [r["week"] for r in store.query(f"SELECT DISTINCT week FROM {table} WHERE season = ? ORDER BY week", [season])]


def ok_weeks(lines):
    return [ln for ln in lines if ln.startswith("WEEKLY_OK")]


def test_no_missed_week_publishes_only_the_resolved_week(store):
    api, s3 = primed_with_2026(store)
    go(Options(season=2027, weeks=(1, 2)), store, api, s3, today=MONDAY_2026_11_23)
    puts_before = len(s3.puts)
    code, lines = go(Options(), store, api, s3, today=MONDAY_2026_11_23)
    assert code == 0 and ok_weeks(lines) == ["WEEKLY_OK season=2027 week=3"]
    assert "catch_up_weeks" not in "\n".join(lines) and weeks_in(store) == [1, 2, 3]
    assert [k for k in s3.puts[puts_before:] if "week-" in k] == ["cbb/rankings/2027/week-03.json"]


def test_one_missed_week_is_rated_and_published_before_the_resolved_week(store):
    api, s3 = primed_with_2026(store)
    go(Options(season=2027, week=1), store, api, s3, today=MONDAY_2026_11_23)  # the run that should have rated week 2 never happened
    puts_before = len(s3.puts)
    code, lines = go(Options(), store, api, s3, today=MONDAY_2026_11_23)
    assert code == 0
    assert ok_weeks(lines) == ["WEEKLY_OK season=2027 week=2", "WEEKLY_OK season=2027 week=3"]  # ascending, each listed
    assert any(ln.startswith("  catch_up_weeks: [2]") for ln in lines)
    assert [k for k in s3.puts[puts_before:] if "week-" in k] == ["cbb/rankings/2027/week-02.json", "cbb/rankings/2027/week-03.json"]
    assert weeks_in(store) == [1, 2, 3]
    # week 3's deltas are against week 2 (the greatest earlier week), not against week 1
    week2 = {r["team"]: r["rank"] for r in s3.json("cbb/rankings/2027/week-02.json")["rankings"]}
    for r in s3.json("cbb/rankings/2027/week-03.json")["rankings"]:
        assert r["delta"] == week2[r["team"]] - r["rank"]
    assert s3.json("cbb/rankings/latest.json")["week"] == 3
    validate_index(s3.json("cbb/rankings/index.json"), [(2027, [1, 2, 3]), (2026, list(range(1, 20)))])


def test_two_missed_weeks_heal_in_ascending_order(store):
    api, s3 = primed_with_2026(store)
    puts_before = len(s3.puts)
    code, lines = go(Options(), store, api, s3, today=MONDAY_2026_11_23)
    assert code == 0 and lines[-1] == "WEEKLY_OK season=2027 week=3"
    assert ok_weeks(lines) == [f"WEEKLY_OK season=2027 week={w}" for w in (1, 2, 3)]
    assert any(ln.startswith("  catch_up_weeks: [1, 2]") for ln in lines)
    assert [k for k in s3.puts[puts_before:] if "week-" in k] == [f"cbb/rankings/2027/week-0{w}.json" for w in (1, 2, 3)]
    assert count(store, where="season = 2027") == 3 * 22


def test_more_missed_weeks_than_the_cap_is_a_catch_up_failure_that_names_the_backfill_workflow(store):
    api, s3 = primed_with_2026(store)
    puts_before = len(s3.puts)
    code, lines = go(Options(max_catch_up=1), store, api, s3, today=MONDAY_2026_11_23)  # weeks 1 and 2 are missing
    assert code == 1
    failure = [ln for ln in lines if ln.startswith("CATCHUP_FAILURE:")]
    assert len(failure) == 1 and "Backfill Ratings" in failure[0] and "[1, 2]" in failure[0] and "limit of 1" in failure[0]
    assert count(store, where="season = 2027") == 0 and len(s3.puts) == puts_before  # nothing rated or published
    assert not any(ln.startswith("WEEKLY_OK") for ln in lines)


def test_the_default_cap_is_four_weeks(store):
    assert main.parse_options([]).max_catch_up == 4
    assert main.parse_options(["--max-catch-up", "9"]).max_catch_up == 9
    with pytest.raises(SystemExit):
        main.parse_options(["--max-catch-up", "-1"])
    # season 2026 on Monday 2025-12-22 resolves to week 7: weeks 1-6 are six missing weeks
    store.upsert("ratings", [{"team_id": 1, "season": 2026, "week": 5, "rating": 1.0, "wins": 0, "losses": 0}],
                 ("team_id", "season", "week"))
    code, lines = go(Options(), store, season_2026_api(), FakeS3(), today=date(2025, 12, 22))
    assert code == 1 and "weeks [1, 2, 3, 4, 6]" in "\n".join(lines) and "CATCHUP_FAILURE" in lines[-1]
    assert count(store, where="week = 7") == 0


def test_the_prior_season_requirement_applies_to_catch_up_weeks(store):
    api, s3 = season_2026_api(), FakeS3()
    assert go(Options(season=2026, weeks=(1, 4), allow_missing_prior=True), store, api, s3)[0] == 0
    puts_before = len(s3.puts)
    # Monday 2025-12-22 resolves to week 7; weeks 5 and 6 are missing, and all three need season 2025
    # (the basketball prior fades out at week 10)
    code, lines = go(Options(), store, api, s3, today=date(2025, 12, 22))
    assert code == 1 and any(ln.startswith("PRIOR_FAILURE:") and "weeks 5-7" in ln and "Backfill season 2025" in ln for ln in lines)
    assert count(store, where="week > 4") == 0 and len(s3.puts) == puts_before
    code, lines = go(Options(allow_missing_prior=True), store, api, s3, today=date(2025, 12, 22))
    assert code == 0 and ok_weeks(lines) == [f"WEEKLY_OK season=2026 week={w}" for w in (5, 6, 7)]


def test_staging_catch_up_uses_ratings_staging_and_publishes_nothing(store):
    api, s3 = primed_with_2026(store)
    puts_before = len(s3.puts)
    production = count(store)
    code, lines = go(Options(staging=True), store, api, s3=None, today=MONDAY_2026_11_23)
    assert code == 0 and ok_weeks(lines) == [f"WEEKLY_OK season=2027 week={w}" for w in (1, 2, 3)]
    assert weeks_in(store, table="ratings_staging") == [1, 2, 3] and weeks_in(store) == []
    assert count(store) == production and len(s3.puts) == puts_before
    # a rerun finds the staging table complete and rates only the resolved week
    _, lines = go(Options(staging=True), store, api, s3=None, today=MONDAY_2026_11_23)
    assert ok_weeks(lines) == ["WEEKLY_OK season=2027 week=3"]


@pytest.mark.parametrize("opts", [Options(season=2027, week=3), Options(season=2027, weeks=(3, 3)), Options(season=2027, all_weeks=True)])
def test_explicit_weeks_never_catch_up(store, opts):
    api, s3 = primed_with_2026(store)
    code, lines = go(opts, store, api, s3, today=MONDAY_2026_11_23)
    assert code == 0 and "catch_up_weeks" not in "\n".join(lines)
    assert weeks_in(store) == ([1, 2, 3] if opts.all_weeks else [3])


def test_a_dry_run_reports_the_catch_up_weeks_and_writes_nothing(store):
    api, s3 = primed_with_2026(store)
    store.execute("DELETE FROM ratings WHERE season = 2027")
    ingest_run.ingest_season(store, 2027, api)
    puts = len(s3.puts)
    code, lines = go(Options(no_ingest=True, dry_run=True), store, None, None, today=MONDAY_2026_11_23)
    assert code == 0 and len(s3.puts) == puts and count(store, where="season = 2027") == 0
    assert [ln for ln in lines if ln.startswith("WEEKLY_DRY_RUN")] == [f"WEEKLY_DRY_RUN season=2027 week={w} table=ratings mode=production" for w in (1, 2, 3)]


# -- the override file is validated before anything is spent or written ------------------------------------

@pytest.mark.parametrize("opts", [Options(), Options(season=2027), Options(season=2026, week=10)])
def test_a_broken_override_file_fails_before_any_api_call_or_write(store, monkeypatch, tmp_path, opts):
    bad = tmp_path / "overrides.json"
    bad.write_text("{not json")
    monkeypatch.setattr(overrides_module, "DEFAULT_OVERRIDES_PATH", bad)
    api, s3 = both_seasons_api(), FakeS3()
    code, lines = go(opts, store, api, s3, today=MONDAY_2026_11_23 if opts.season != 2026 else AFTER_2026)
    assert code == 1 and api.calls == 0 and lines[0].startswith("OVERRIDE_FAILURE:") and len(lines) == 1
    assert all(count(store, t) == 0 for t in ("games", "teams", "seasons", "ratings")) and s3.puts == []


def test_a_score_override_on_a_game_that_becomes_neutralized_fails_the_run_with_a_fix_hint(store, monkeypatch, tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"version": 1, "overrides": [{
        "kind": "score", "game_id": 212784, "season": 2026, "home_team_id": 52, "away_team_id": 99,
        "home_score": 70, "away_score": 72, "reason": "Official scorer corrected the final"}]}))
    monkeypatch.setattr(overrides_module, "DEFAULT_OVERRIDES_PATH", path)
    games = load("cbbd_games_2026.json")
    target = next(g for g in games if g["id"] == 212784)
    path.write_text(path.read_text().replace('"home_team_id": 52', f'"home_team_id": {target["homeTeamId"]}')
                    .replace('"away_team_id": 99', f'"away_team_id": {target["awayTeamId"]}'))
    s3 = FakeS3()
    assert go(Options(season=2026, week=10), store, season_2026_api(), s3)[0] == 0  # the override applies while the game exists
    api = FakeApi({2026: load("cbbd_teams_2026.json")}, {2026: [g for g in games if g["id"] != 212784]})
    code, lines = go(Options(season=2026, week=10), store, api, s3)  # the source drops it: neutralized
    assert code == 1
    failure = next(ln for ln in lines if ln.startswith("OVERRIDE_FAILURE:"))
    assert "game_id=212784" in failure and "only a final game can be re-scored" in failure
    assert "ingest/game_result_overrides.json" in failure and "remove the entry" in failure


# -- a failed ingest never leaves the overrides reverted ------------------------------------------------------

SITE_OVERRIDE = {"kind": "site", "game_id": 213466, "season": 2026, "home_team_id": 902, "away_team_id": 344,
                 "neutral_site": False, "swap_home_away": True, "reason": "Played at Washington State"}


def with_site_override(monkeypatch, tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"version": 1, "overrides": [SITE_OVERRIDE]}))
    monkeypatch.setattr(overrides_module, "DEFAULT_OVERRIDES_PATH", path)


def malformed_api():
    games = load("cbbd_games_2026.json") + [
        {"id": 9_100_000 + i, "startDate": "2025-11-05T00:00:00.000Z", "status": "final"} for i in range(6)]  # SYNTHETIC
    return FakeApi({2026: load("cbbd_teams_2026.json")}, {2026: games})


def test_a_failed_ingest_that_wrote_rows_still_restores_the_overrides(store, monkeypatch, tmp_path):
    with_site_override(monkeypatch, tmp_path)
    trip = Tripwire()
    monkeypatch.setattr(main, "rate_week", trip)
    s3 = FakeS3()
    code, lines = go(Options(season=2026, week=10), store, malformed_api(), s3)
    assert code == 1 and not trip.called
    assert_stopped(store, s3, lines, "INGEST_FAILURE")
    assert "overrides re-applied afterwards: 1 game(s) restored" in "\n".join(lines)
    assert count(store, "games") == 40  # the fetched rows were written before the gate failed
    assert store.query("SELECT home_team_id FROM games WHERE id = 213466")[0]["home_team_id"] == 344  # not reverted to 902
    assert store.query("SELECT last_ingested_at FROM seasons")[0]["last_ingested_at"] is None  # and not stamped


def test_a_failed_ingest_that_raised_after_writing_also_restores_the_overrides(store, monkeypatch, tmp_path):
    with_site_override(monkeypatch, tmp_path)

    def writes_then_dies(store_, season, client):
        ingest_run.ingest_season(store_, season, season_2026_api())  # rows written, source values
        assert store_.query("SELECT home_team_id FROM games WHERE id = 213466")[0]["home_team_id"] == 902
        raise RuntimeError("connection dropped")

    monkeypatch.setattr(main, "ingest_season", writes_then_dies)
    code, lines = go(Options(season=2026, week=10), store, season_2026_api(), FakeS3())
    assert code == 1 and any(ln.startswith("INGEST_FAILURE: RuntimeError: connection dropped") for ln in lines)
    assert store.query("SELECT home_team_id FROM games WHERE id = 213466")[0]["home_team_id"] == 344


def test_a_failed_ingest_that_wrote_nothing_does_not_touch_the_store(store, monkeypatch, tmp_path):
    with_site_override(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr(main, "apply_overrides", lambda *a, **k: called.append(1))
    api = FakeApi({2026: []}, {2026: load("cbbd_games_2026.json")})  # no teams: nothing written
    code, lines = go(Options(season=2026, week=10), store, api, FakeS3())
    assert code == 1 and called == [] and lines[0].startswith("INGEST_FAILURE: ") and "re-applied" not in lines[0]


def test_ingest_cli_also_restores_overrides_after_a_failed_ingest(tmp_path, monkeypatch, capsys):
    with_site_override(monkeypatch, tmp_path)
    monkeypatch.setenv("CBB_STORE", "local")
    monkeypatch.setenv("CBB_LOCAL_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(ingest_run, "CbbdClient", malformed_api)
    assert ingest_run.main(["season", "2026"]) == 1
    out = capsys.readouterr().out
    assert "INGEST_FAILURE: 6 of 46 fetched games were dropped" in out and "213466" in out.split('"overrides"')[1].split('"failed"')[0]
    from database import get_store
    assert get_store().query("SELECT home_team_id FROM games WHERE id = 213466")[0]["home_team_id"] == 344
