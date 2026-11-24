import json
from datetime import datetime, timezone

import pytest

from artifacts.r2 import DEFAULT_PREFIX, STAGING_PREFIX, PublishError, RankingsPublisher, normalize_prefix, r2_from_env
from tests.fake_s3 import FakeS3

NOW = datetime(2026, 3, 16, 10, 0, tzinfo=timezone.utc)
FOOTBALL = {
    "rankings/2026/week-01.json": b"football", "rankings/2026/week-15.json": b"football",
    "rankings/latest.json": b"football", "rankings/index.json": b"football", "schedule/2026.json": b"football",
}


def payload(season, week):
    return {"season": season, "week": week, "generated_at_utc": NOW.isoformat(), "rankings": []}


def publisher(s3, prefix=DEFAULT_PREFIX):
    return RankingsPublisher(s3, "test-bucket", prefix)


def test_keys_have_two_digit_weeks_under_the_prefix():
    p = publisher(FakeS3())
    assert p.week_key(2026, 3) == "cbb/rankings/2026/week-03.json"
    assert p.week_key(2026, 12) == "cbb/rankings/2026/week-12.json"
    assert p.season_latest_key(2026) == "cbb/rankings/2026/latest.json"
    assert (p.latest_key, p.index_key) == ("cbb/rankings/latest.json", "cbb/rankings/index.json")
    assert publisher(FakeS3(), "cbb-staging").week_key(2027, 1) == "cbb-staging/rankings/2027/week-01.json"


def test_first_publish_writes_week_season_latest_global_latest_and_index_json():
    s3 = FakeS3()
    result = publisher(s3).publish(payload(2026, 3), NOW)
    assert result.keys == [
        "cbb/rankings/2026/week-03.json", "cbb/rankings/2026/latest.json",
        "cbb/rankings/latest.json", "cbb/rankings/index.json",
    ]
    assert s3.puts == result.keys and set(s3.content_types.values()) == {"application/json"}
    assert s3.json("cbb/rankings/latest.json") == payload(2026, 3) == s3.json("cbb/rankings/2026/latest.json")
    assert s3.json("cbb/rankings/index.json") == {
        "generated_at_utc": NOW.isoformat(), "latest_season": 2026, "latest_week": 3,
        "seasons": [{"season": 2026, "weeks": [3]}],
    }
    assert result.bytes_by_key["cbb/rankings/index.json"] == len(s3.objects["cbb/rankings/index.json"])


def test_older_week_does_not_overwrite_latest_and_index_lists_everything():
    s3 = FakeS3()
    p = publisher(s3)
    for week in (1, 2, 12):
        p.publish(payload(2026, week), NOW)
    s3.puts.clear()
    again = p.publish(payload(2026, 5), NOW)  # a backfill of an old week
    assert again.keys == ["cbb/rankings/2026/week-05.json", "cbb/rankings/index.json"]
    assert not again.latest_updated and not again.season_latest_updated
    assert s3.json("cbb/rankings/latest.json")["week"] == 12 == s3.json("cbb/rankings/2026/latest.json")["week"]
    assert s3.json("cbb/rankings/index.json")["seasons"] == [{"season": 2026, "weeks": [1, 2, 5, 12]}]


def test_index_seasons_descending_weeks_ascending_and_latest_from_listing():
    s3 = FakeS3()
    p = publisher(s3)
    for season, week in [(2026, 19), (2026, 2), (2027, 1), (2025, 30)]:
        p.publish(payload(season, week), NOW)
    index = s3.json("cbb/rankings/index.json")
    assert index["seasons"] == [
        {"season": 2027, "weeks": [1]}, {"season": 2026, "weeks": [2, 19]}, {"season": 2025, "weeks": [30]},
    ]
    assert (index["latest_season"], index["latest_week"]) == (2027, 1)
    # an old season's newest week updates that season's latest only
    assert s3.json("cbb/rankings/2025/latest.json")["week"] == 30
    assert s3.json("cbb/rankings/2026/latest.json")["week"] == 19
    assert s3.json("cbb/rankings/latest.json")["season"] == 2027
    p.publish(payload(2026, 19), NOW)  # republish: still not the global latest
    assert s3.json("cbb/rankings/latest.json")["season"] == 2027


def test_index_is_rebuilt_by_listing_so_it_heals_and_follows_pagination():
    objects = {f"cbb/rankings/2026/week-{w:02d}.json": b"{}" for w in range(1, 8)}
    objects["cbb/rankings/index.json"] = b'{"stale": true}'
    s3 = FakeS3(objects, page_size=2)
    publisher(s3).publish(payload(2026, 8), NOW)
    assert s3.json("cbb/rankings/index.json")["seasons"] == [{"season": 2026, "weeks": list(range(1, 9))}]
    assert len(s3.listings) > 0 and s3.listings == ["cbb/rankings/"] * len(s3.listings)


def test_foreign_keys_are_never_listed_read_written_or_indexed():
    s3 = FakeS3(FOOTBALL, ignore_prefix=True)  # a listing that returns football's keys regardless of Prefix
    publisher(s3).publish(payload(2027, 1), NOW)
    index = s3.json("cbb/rankings/index.json")
    assert index["seasons"] == [{"season": 2027, "weeks": [1]}]  # football's 2026 weeks 1 and 15 are not ours
    assert all(k.startswith("cbb/") for k in s3.puts)
    assert {k: s3.objects[k] for k in FOOTBALL} == FOOTBALL
    assert set(s3.listings) == {"cbb/rankings/"}


def test_look_alike_keys_under_other_prefixes_are_ignored():
    s3 = FakeS3({"cbb-staging/rankings/2026/week-04.json": b"{}", "xcbb/rankings/2026/week-05.json": b"{}",
                 "cbb/rankings/2026/week-xx.json": b"{}", "cbb/rankings/2026/extra/week-06.json": b"{}"}, ignore_prefix=True)
    publisher(s3).publish(payload(2026, 1), NOW)
    assert s3.json("cbb/rankings/index.json")["seasons"] == [{"season": 2026, "weeks": [1]}]


def test_staging_prefix_never_touches_production():
    s3 = FakeS3({"cbb/rankings/2026/week-05.json": b"prod", "cbb/rankings/latest.json": b"prod", "cbb/rankings/index.json": b"prod"})
    publisher(s3, STAGING_PREFIX).publish(payload(2026, 7), NOW)
    assert all(k.startswith("cbb-staging/") for k in s3.puts)
    assert s3.objects["cbb/rankings/latest.json"] == b"prod" and s3.objects["cbb/rankings/index.json"] == b"prod"
    assert s3.json("cbb-staging/rankings/index.json")["seasons"] == [{"season": 2026, "weeks": [7]}]


@pytest.mark.parametrize("bad", ["", "/", "rankings", "rankings/", "schedule/", "a/b/", "../x", "/cbb/", "cbb//"])
def test_prefix_must_be_one_safe_segment_and_not_footballs(bad):
    with pytest.raises(PublishError):
        normalize_prefix(bad)


def test_prefix_gets_its_trailing_slash():
    assert normalize_prefix("cbb") == "cbb/" and normalize_prefix("cbb-staging/") == "cbb-staging/"


def test_upload_failure_raises_publish_error():
    s3 = FakeS3(fail_put_on="latest.json")
    with pytest.raises(PublishError, match="upload of cbb/rankings/2026/latest.json failed"):
        publisher(s3).publish(payload(2026, 1), NOW)
    assert "cbb/rankings/index.json" not in s3.objects  # the index is last


def test_listing_failure_raises_publish_error():
    with pytest.raises(PublishError, match="listing cbb/rankings/ failed"):
        publisher(FakeS3(fail_list=True)).publish(payload(2026, 1), NOW)


def test_truncated_listing_without_a_token_raises():
    class Broken(FakeS3):
        def list_objects_v2(self, **kwargs):
            return {"Contents": [], "IsTruncated": True}

    with pytest.raises(PublishError, match="continuation token"):
        publisher(Broken()).list_weeks()


def test_week_missing_from_the_listing_raises():
    class Lagging(FakeS3):
        def list_objects_v2(self, **kwargs):
            return {"Contents": [], "IsTruncated": False}

    with pytest.raises(PublishError, match="missing from the bucket listing"):
        publisher(Lagging()).publish(payload(2026, 1), NOW)


def test_empty_bucket_name_is_refused():
    with pytest.raises(PublishError):
        RankingsPublisher(FakeS3(), "")


def test_r2_from_env_requires_all_four_names_and_builds_the_r2_endpoint_without_network():
    with pytest.raises(PublishError, match="R2_BUCKET_NAME"):
        r2_from_env({"R2_ACCOUNT_ID": "a", "R2_ACCESS_KEY_ID": "b", "R2_SECRET_ACCESS_KEY": "c"})
    client, bucket = r2_from_env({
        "R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "id", "R2_SECRET_ACCESS_KEY": "secret", "R2_BUCKET_NAME": "bkt",
    })
    assert bucket == "bkt"
    assert client.meta.endpoint_url == "https://acct.r2.cloudflarestorage.com"
    assert client.meta.region_name == "auto"
    assert "secret" not in json.dumps({"bucket": bucket})
