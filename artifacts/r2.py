"""Publish rankings artifacts to Cloudflare R2 (S3 API, boto3).

Keys, under a prefix (default `cbb/`; `cbb-staging/` for staging; the prefix always ends in `/`):

    {prefix}rankings/{season}/week-{WW}.json   one week (two-digit week)
    {prefix}rankings/{season}/latest.json      the season's latest week
    {prefix}rankings/latest.json               the latest week of the latest season
    {prefix}rankings/index.json                {generated_at_utc, latest_season, latest_week,
                                                seasons: [{season, weeks: [int asc]}]} (seasons desc)

The bucket also holds football's objects (`rankings/...`, `schedule/...`) with no prefix: this
module never lists, reads or writes a key outside its own prefix, and a listing that returns
foreign keys is filtered, not trusted.

The index is REBUILT BY LISTING the bucket under the prefix (as football does), so it heals
itself if objects are added or removed by hand. `latest.json` files always point at the true
latest (highest season, then highest week): publishing an old week during a backfill refreshes
that week's file and the index but leaves a newer `latest.json` alone.

Unlike football's publisher, nothing here swallows errors: any failure raises `PublishError`, so
an unattended run that did not publish is red.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

DEFAULT_PREFIX = "cbb/"
STAGING_PREFIX = "cbb-staging/"
FOREIGN_PREFIXES = ("rankings/", "schedule/")  # football's keys, which have no prefix
ENV_NAMES = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")
_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/$")


class PublishError(RuntimeError):
    """An artifact could not be published (or R2 is not configured). The run must fail."""


def normalize_prefix(prefix: str) -> str:
    """A single-segment, non-empty prefix ending in `/` that cannot collide with football's keys."""
    if not isinstance(prefix, str):
        raise PublishError(f"R2 prefix must be a string, got {prefix!r}")
    prefix = prefix if prefix.endswith("/") else prefix + "/"
    if not _PREFIX_RE.fullmatch(prefix) or ".." in prefix or prefix in FOREIGN_PREFIXES:
        raise PublishError(f"invalid R2 prefix {prefix!r}: need one non-empty path segment, not football's")
    return prefix


def r2_from_env(env: Mapping[str, str] | None = None) -> tuple[Any, str]:
    """(boto3 S3 client for R2, bucket name) from the R2_* variables. Raises PublishError if any
    is missing; building the client makes no network call."""
    env = os.environ if env is None else env
    missing = [n for n in ENV_NAMES if not env.get(n)]
    if missing:
        raise PublishError(f"R2 is not configured: missing environment variables {', '.join(missing)}")
    import boto3  # imported here so tests and dry runs never need it

    client = boto3.client(
        service_name="s3",
        endpoint_url=f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    return client, env["R2_BUCKET_NAME"]


@dataclass
class PublishResult:
    season: int
    week: int
    keys: list[str] = field(default_factory=list)  # in upload order
    latest_season: int | None = None
    latest_week: int | None = None
    season_latest_updated: bool = False
    latest_updated: bool = False
    bytes_by_key: dict[str, int] = field(default_factory=dict)


class RankingsPublisher:
    def __init__(self, client: Any, bucket: str, prefix: str = DEFAULT_PREFIX):
        if not bucket:
            raise PublishError("R2 bucket name is empty")
        self.client, self.bucket, self.prefix = client, bucket, normalize_prefix(prefix)
        self._week_re = re.compile(rf"^{re.escape(self.prefix)}rankings/(\d+)/week-(\d{{2,}})\.json$")

    # -- keys --------------------------------------------------------------------------------

    def week_key(self, season: int, week: int) -> str:
        return f"{self.prefix}rankings/{season}/week-{week:02d}.json"

    def season_latest_key(self, season: int) -> str:
        return f"{self.prefix}rankings/{season}/latest.json"

    @property
    def latest_key(self) -> str:
        return f"{self.prefix}rankings/latest.json"

    @property
    def index_key(self) -> str:
        return f"{self.prefix}rankings/index.json"

    # -- R2 ----------------------------------------------------------------------------------

    def _put(self, key: str, payload: Any, result: PublishResult) -> None:
        if not key.startswith(self.prefix):
            raise PublishError(f"refusing to write {key!r}: outside the prefix {self.prefix!r}")
        body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/json")
        except Exception as exc:
            raise PublishError(f"upload of {key} failed: {type(exc).__name__}: {exc}") from exc
        result.keys.append(key)
        result.bytes_by_key[key] = len(body)

    def list_weeks(self) -> dict[int, set[int]]:
        """{season: {weeks}} from the week files under this prefix, across every listing page."""
        listing_prefix = f"{self.prefix}rankings/"
        seasons: dict[int, set[int]] = {}
        token = None
        try:
            while True:
                kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": listing_prefix}
                if token:
                    kwargs["ContinuationToken"] = token
                page = self.client.list_objects_v2(**kwargs)
                for obj in page.get("Contents") or []:
                    match = self._week_re.match(obj["Key"])  # also drops anything a fake or a bug returns outside the prefix
                    if match:
                        seasons.setdefault(int(match.group(1)), set()).add(int(match.group(2)))
                if not page.get("IsTruncated"):
                    return seasons
                token = page.get("NextContinuationToken")
                if not token:
                    raise PublishError("listing is truncated but has no continuation token")
        except PublishError:
            raise
        except Exception as exc:
            raise PublishError(f"listing {listing_prefix} failed: {type(exc).__name__}: {exc}") from exc

    @staticmethod
    def build_index(seasons: Mapping[int, set[int]], now: datetime) -> dict[str, Any]:
        latest_season = max(seasons)
        return {
            "generated_at_utc": now.astimezone(timezone.utc).isoformat(),
            "latest_season": latest_season,
            "latest_week": max(seasons[latest_season]),
            "seasons": [{"season": s, "weeks": sorted(seasons[s])} for s in sorted(seasons, reverse=True)],
        }

    def publish(self, payload: dict[str, Any], now: datetime | None = None) -> PublishResult:
        """Upload the week file, then whichever `latest` files it is the newest for, then the
        rebuilt index (last, so the index never names a week that is not there). Raises
        PublishError on any failure; objects already uploaded stay."""
        now = now or datetime.now(timezone.utc)
        season, week = int(payload["season"]), int(payload["week"])
        result = PublishResult(season=season, week=week)
        self._put(self.week_key(season, week), payload, result)

        seasons = self.list_weeks()
        if week not in seasons.get(season, set()):
            raise PublishError(f"{self.week_key(season, week)} was uploaded but is missing from the bucket listing")
        result.latest_season = max(seasons)
        result.latest_week = max(seasons[result.latest_season])
        if week == max(seasons[season]):
            self._put(self.season_latest_key(season), payload, result)
            result.season_latest_updated = True
            if season == result.latest_season:
                self._put(self.latest_key, payload, result)
                result.latest_updated = True
        self._put(self.index_key, self.build_index(seasons, now), result)
        return result
