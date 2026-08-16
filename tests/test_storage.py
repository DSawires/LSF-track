"""Blob storage backends and backup retention."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from app.storage import LocalStorage, S3Storage


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def iter_chunks(self, size: int):
        for i in range(0, len(self.data), size):
            yield self.data[i : i + size]


class FakeS3Client:
    """Just enough of boto3's S3 client for the backend's four calls."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.exceptions = type("E", (), {"NoSuchKey": KeyError})

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):
        self.objects[key] = fileobj.read()

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": FakeBody(self.objects[Key])}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


def test_local_storage_roundtrip(tmp_path):
    storage = LocalStorage(Path(tmp_path))
    storage.put("images/abc", io.BytesIO(b"pixels"))
    response = storage.response("images/abc", "image/png")
    assert response is not None
    assert storage.response("images/missing", "image/png") is None
    assert storage.list("images/") == ["images/abc"]
    storage.delete("images/abc")
    assert storage.list("images/") == []


def test_s3_storage_layout_and_streaming():
    client = FakeS3Client()
    storage = S3Storage("factory-bucket", prefix="lsf/", client=client)
    storage.put("images/abc", io.BytesIO(b"pixels"), "image/png")

    # The single-bucket layout: prefix + namespace + id.
    assert list(client.objects) == ["lsf/images/abc"]

    response = storage.response("images/abc", "image/png")

    async def drain(streaming_response):
        return b"".join([chunk async for chunk in streaming_response.body_iterator])

    assert asyncio.run(drain(response)) == b"pixels"
    assert storage.response("images/nope", "image/png") is None

    storage.put("backups/lsf-20260101-000000Z.sql.gz", io.BytesIO(b"dump"), "application/gzip")
    assert storage.list("backups/") == ["backups/lsf-20260101-000000Z.sql.gz"]
    assert storage.list("images/") == ["images/abc"]

    storage.delete("images/abc")
    assert "lsf/images/abc" not in client.objects


def test_backup_retention_keeps_newest(tmp_path):
    """Timestamped names sort chronologically, so pruning is list arithmetic --
    the same slice cmd_backup uses."""
    storage = LocalStorage(Path(tmp_path))
    names = [f"backups/lsf-2026010{i}-000000Z.sql.gz" for i in range(1, 8)]
    for name in names:
        storage.put(name, io.BytesIO(b"x" * 600))

    keep = 3
    existing = storage.list("backups/")
    for old in existing[:-keep]:
        storage.delete(old)

    assert storage.list("backups/") == names[-keep:]
