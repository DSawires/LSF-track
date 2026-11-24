"""Blob storage: local disk by default, S3 when LSF_S3_BUCKET is set.

One bucket holds everything, namespaced by key prefix: item photos under
`images/`, database dumps under `backups/`. The application API is identical
either way — phones never see AWS, auth stays in the app, and `docker compose
up` on a laptop keeps working with no cloud setup.

Credentials follow the standard AWS chain; on EC2 that means the instance role,
so nothing is configured here.
"""

from __future__ import annotations

import os
import shutil
import uuid
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO

from fastapi.responses import FileResponse, Response, StreamingResponse

from app.config import get_settings

# The suffix of a local file still being written (see LocalStorage.put).
_PARTIAL = ".part"


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, key: str, fileobj: BinaryIO, content_type: str | None = None) -> None:
        """All of it or none of it: written beside the destination under
        another name and moved into place once it is complete and on disk.
        Written straight to its final name, a disk that filled halfway left a
        truncated file there -- and for a database dump, a truncated file with
        the newest name is "the latest backup" to everything that lists them.
        """
        destination = self.root / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}{_PARTIAL}")
        try:
            with partial.open("wb") as out:
                shutil.copyfileobj(fileobj, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(partial, destination)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

    def response(self, key: str, media_type: str) -> Response | None:
        path = self.root / key
        if not path.is_file():
            return None
        return FileResponse(path, media_type=media_type)

    def read(self, key: str) -> bytes | None:
        """The whole object, for the few callers that need the bytes
        themselves (drawing a picture into a PDF) rather than a response."""
        path = self.root / key
        return path.read_bytes() if path.is_file() else None

    def list(self, prefix: str) -> list[str]:
        return sorted(self.sizes(prefix))

    def sizes(self, prefix: str) -> dict[str, int | None]:
        """Every object under a prefix, with its size in bytes."""
        base = self.root / prefix
        if not base.is_dir():
            return {}
        return {
            f"{prefix}{p.name}": p.stat().st_size
            for p in base.iterdir()
            # A write that was killed mid-way leaves its partial behind; it is
            # not an object.
            if p.is_file() and not p.name.endswith(_PARTIAL)
        }

    def delete(self, key: str) -> None:
        (self.root / key).unlink(missing_ok=True)


class S3Storage:
    def __init__(self, bucket: str, prefix: str = "", client=None) -> None:
        if client is None:  # pragma: no cover - exercised against real AWS only
            import boto3

            client = boto3.client("s3")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def put(self, key: str, fileobj: BinaryIO, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self.client.upload_fileobj(
            fileobj, self.bucket, self._key(key), ExtraArgs=extra or None
        )

    def response(self, key: str, media_type: str) -> Response | None:
        # Streamed through the app rather than a presigned redirect: auth stays
        # in one place and the CSP can keep img-src limited to 'self'.
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except self.client.exceptions.NoSuchKey:
            return None
        return StreamingResponse(obj["Body"].iter_chunks(64 * 1024), media_type=media_type)

    def read(self, key: str) -> bytes | None:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except self.client.exceptions.NoSuchKey:
            return None
        return obj["Body"].read()

    def list(self, prefix: str) -> list[str]:
        return sorted(self.sizes(prefix))

    def sizes(self, prefix: str) -> dict[str, int | None]:
        """Every object under a prefix, with its size in bytes (None where the
        listing does not say)."""
        found: dict[str, int | None] = {}
        token: str | None = None
        strip = len(self.prefix)
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": self._key(prefix)}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            for entry in page.get("Contents", []):
                found[entry["Key"][strip:]] = entry.get("Size")
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        return found

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))


@lru_cache
def get_storage():
    settings = get_settings()
    if settings.s3_bucket:
        return S3Storage(settings.s3_bucket, settings.s3_prefix)
    return LocalStorage(Path(settings.upload_dir))
