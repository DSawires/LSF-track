"""Blob storage backends and backup retention."""

from __future__ import annotations

import asyncio
import gzip
import io
from pathlib import Path

import pytest

from app.services.backups import dump_keys, prune_plan, unreadable
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
    """Timestamped names sort chronologically; the plan is what cmd_backup
    deletes."""
    storage = LocalStorage(Path(tmp_path))
    names = [f"backups/lsf-2026010{i}-000000Z.sql.gz" for i in range(1, 8)]
    for name in names:
        storage.put(name, io.BytesIO(b"x" * 600))

    delete, held = prune_plan(storage.sizes("backups/"), keep=3)
    assert (delete, held) == (names[:-3], [])
    for old in delete:
        storage.delete(old)
    assert storage.list("backups/") == names[-3:]
    # Nothing to do when there are no more than it keeps, and 0 keeps them all.
    assert prune_plan(storage.sizes("backups/"), keep=3) == ([], [])
    assert prune_plan(storage.sizes("backups/"), keep=0) == ([], [])


def test_pruning_only_ever_touches_the_backup_jobs_own_files(tmp_path):
    storage = LocalStorage(Path(tmp_path))
    names = [f"backups/lsf-2026010{i}-000000Z.sql.gz" for i in range(1, 6)]
    for name in names:
        storage.put(name, io.BytesIO(b"x" * 600))
    # Someone's own copies, left beside them: one that sorts first and one last.
    storage.put("backups/before-upgrade.sql.gz", io.BytesIO(b"y" * 600))
    storage.put("backups/manual-2026.sql.gz", io.BytesIO(b"y" * 600))

    delete, held = prune_plan(storage.sizes("backups/"), keep=2)
    assert (delete, held) == (names[:3], [])
    assert dump_keys(storage.list("backups/")) == names


def test_a_small_dump_never_evicts_a_much_larger_one(tmp_path):
    """A container started against an empty database dumps it at once and
    again every night. Those must not push the real backups off the shelf."""
    storage = LocalStorage(Path(tmp_path))
    real = [f"backups/lsf-2026010{i}-020000Z.sql.gz" for i in range(1, 4)]
    for name in real:
        storage.put(name, io.BytesIO(b"x" * 50_000))
    empties = [f"backups/lsf-202601{i:02d}-020000Z.sql.gz" for i in range(10, 16)]
    for night, name in enumerate(empties, start=1):
        storage.put(name, io.BytesIO(b"x" * 900))
        delete, held = prune_plan(storage.sizes("backups/"), keep=3)
        for old in delete:
            storage.delete(old)
        # Every real backup is still there, however many nights go by...
        assert set(real) <= set(storage.list("backups/")), night
    # ...each of them held back by name, so the job can say so...
    assert held == real
    # ...and the small ones are still pruned among themselves.
    assert storage.list("backups/") == real + empties[-3:]

    # An ordinary night-to-night change in size holds nothing back.
    for name in real:
        storage.delete(name)
    storage.put("backups/lsf-20260116-020000Z.sql.gz", io.BytesIO(b"x" * 500))
    assert prune_plan(storage.sizes("backups/"), keep=3)[1] == []


def test_a_local_write_is_all_or_nothing(tmp_path):
    storage = LocalStorage(Path(tmp_path))
    key = "backups/lsf-20260101-020000Z.sql.gz"
    storage.put(key, io.BytesIO(b"last night, complete"))

    class DiskFills(io.BytesIO):
        def read(self, *args):
            if self.tell() > 0:
                raise OSError(28, "No space left on device")
            return super().read(8)

    # Overwriting it: the complete file stays, not half of the new one.
    with pytest.raises(OSError):
        storage.put(key, DiskFills(b"tonight, cut short by a full disk"))
    assert storage.read(key) == b"last night, complete"
    # A new name: nothing appears under it at all, and nothing is left behind.
    other = "backups/lsf-20260102-020000Z.sql.gz"
    with pytest.raises(OSError):
        storage.put(other, DiskFills(b"tonight, cut short by a full disk"))
    assert storage.list("backups/") == [key]
    assert [p.name for p in (Path(tmp_path) / "backups").iterdir()] == [Path(key).name]

    # A write killed outright leaves its partial on disk; it is not a backup.
    (Path(tmp_path) / "backups" / ".lsf-20260103-020000Z.sql.gz.abc.part").write_bytes(b"half")
    assert storage.list("backups/") == [key]


def test_a_dump_is_read_to_its_end_before_it_is_trusted(tmp_path):
    whole = tmp_path / "whole.sql.gz"
    with gzip.open(whole, "wb") as out:
        out.write(b"CREATE TABLE events ();\n" * 5000)
    assert unreadable(str(whole)) is None

    cut = tmp_path / "cut.sql.gz"
    cut.write_bytes(whole.read_bytes()[: whole.stat().st_size // 2])
    assert "not a complete gzip file" in unreadable(str(cut))

    not_gzip = tmp_path / "plain.sql.gz"
    not_gzip.write_bytes(b"CREATE TABLE events ();")
    assert "not a complete gzip file" in unreadable(str(not_gzip))

    empty = tmp_path / "empty.sql.gz"
    with gzip.open(empty, "wb"):
        pass
    assert unreadable(str(empty)) == "it is empty"


def test_restore_refuses_a_cut_short_dump_before_it_drops_anything(tmp_path, monkeypatch, capsys):
    import subprocess
    from types import SimpleNamespace

    import manage
    from app.config import get_settings

    whole = tmp_path / "whole.sql.gz"
    with gzip.open(whole, "wb") as out:
        out.write(b"CREATE TABLE events ();\n" * 5000)
    cut = tmp_path / "lsf-20260101-020000Z.sql.gz"
    cut.write_bytes(whole.read_bytes()[: whole.stat().st_size // 2])

    monkeypatch.setenv("LSF_DATABASE_URL", "postgresql+psycopg://lsf:lsf@db.invalid:5432/lsf_track")
    get_settings.cache_clear()

    def no_psql(*args, **kwargs):
        raise AssertionError(f"psql was run: {args}")

    monkeypatch.setattr(subprocess, "run", no_psql)
    monkeypatch.setattr(subprocess, "Popen", no_psql)
    try:
        with pytest.raises(SystemExit) as stopped:
            manage.cmd_restore(SimpleNamespace(source=str(cut), reset=True, yes=True, list=False))
    finally:
        get_settings.cache_clear()
    assert stopped.value.code == 1
    assert "Nothing was changed" in capsys.readouterr().err
