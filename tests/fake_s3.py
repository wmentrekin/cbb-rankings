"""An in-memory stand-in for the boto3 S3 client: just `put_object` and `list_objects_v2`
(paged, with continuation tokens), enough for `artifacts.r2`."""
from __future__ import annotations

import json


class FakeS3:
    def __init__(self, objects: dict[str, bytes] | None = None, *, page_size: int = 1000,
                 ignore_prefix: bool = False, fail_put_on: str | None = None, fail_list: bool = False):
        self.objects: dict[str, bytes] = dict(objects or {})
        self.content_types: dict[str, str] = {}
        self.page_size = page_size
        self.ignore_prefix = ignore_prefix  # a listing that returns every key, whatever Prefix says
        self.fail_put_on = fail_put_on  # substring of a key whose upload raises
        self.fail_list = fail_list
        self.puts: list[str] = []
        self.listings: list[str] = []

    def put_object(self, *, Bucket, Key, Body, ContentType=None, **kwargs):
        assert Bucket == "test-bucket"
        if self.fail_put_on and self.fail_put_on in Key:
            raise RuntimeError(f"simulated R2 failure for {Key}")
        self.objects[Key] = Body
        self.content_types[Key] = ContentType
        self.puts.append(Key)
        return {}

    def list_objects_v2(self, *, Bucket, Prefix="", ContinuationToken=None, **kwargs):
        assert Bucket == "test-bucket"
        if self.fail_list:
            raise RuntimeError("simulated listing failure")
        self.listings.append(Prefix)
        keys = sorted(k for k in self.objects if self.ignore_prefix or k.startswith(Prefix))
        start = int(ContinuationToken) if ContinuationToken else 0
        page = keys[start:start + self.page_size]
        more = start + self.page_size < len(keys)
        response = {"Contents": [{"Key": k} for k in page], "IsTruncated": more, "KeyCount": len(page)}
        if more:
            response["NextContinuationToken"] = str(start + self.page_size)
        return response

    def json(self, key: str):
        return json.loads(self.objects[key])
