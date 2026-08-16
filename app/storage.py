"""Blob storage: local disk by default, S3 when LSF_S3_BUCKET is set.

One bucket holds everything, namespaced by key prefix: item photos under
`images/`, database dumps under `backups/`. The application API is identical
either way — phones never see AWS, auth stays in the app, and `docker compose
up` on a laptop keeps working with no cloud setup.

Credentials follow the standard AWS chain; on EC2 that means the instance role,
so nothing is configured here.
"""

from __future__ import annotations

import shutil
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO

from fastapi.responses import FileResponse, Response, StreamingResponse

from app.config import get_settings


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, key: str, fileobj: BinaryIO, content_type: str | None = None) -> None:
        destination = self.root / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as out:
            shutil.copyfileobj(fileobj, out)

    def response(self, key: str, media_type: str) -> Response | None:
        path = self.root / key
        if not path.is_file():
            return None
        return FileResponse(path, media_type=media_type)

    def list(self, prefix: str) -> list[str]:
        base = self.root / prefix
        if not base.is_dir():
            return []
        return sorted(f"{prefix}{p.name}" for p in base.iterdir() if p.is_file())

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

    def list(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": self._key(prefix)}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            keys.extend(o["Key"] for o in page.get("Contents", []))
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        strip = len(self.prefix)
        return sorted(k[strip:] for k in keys)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))


@lru_cache
def get_storage():
    settings = get_settings()
    if settings.s3_bucket:
        return S3Storage(settings.s3_bucket, settings.s3_prefix)
    return LocalStorage(Path(settings.upload_dir))
