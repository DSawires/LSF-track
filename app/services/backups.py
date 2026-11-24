"""Which database dumps to keep, and whether one can be read.

`manage.py backup` writes `backups/lsf-<UTC stamp>.sql.gz` to storage once a
day and prunes the old ones; `manage.py restore` loads one back. The two
decisions that can lose data live here, as plain functions with tests:

Pruning. Only files this job wrote are ever counted or deleted -- a
`before-upgrade.sql.gz` someone put beside them is not ours to remove. And a
dump is never deleted to make room for one less than half its size. A
container that starts against an empty database (a lost volume, a wrong
connection string) dumps it straight away and again every night; counting
alone, each of those evicted one real backup, and a week of them emptied the
shelf. Held dumps stay until someone deletes them by hand, and the job says so
every night it holds one.

Reading. A restore with `--reset` drops the schema first. A dump cut short by
a full disk gunzips partway and then fails -- after the drop. So it is read
to its end before anything is dropped.
"""

from __future__ import annotations

import gzip
import re
import zlib

DUMP_KEY = re.compile(r"backups/lsf-(\d{8}-\d{6})Z\.sql\.gz")


def dump_keys(keys) -> list[str]:
    """The dumps among a storage listing, oldest first. The stamp in the name
    sorts chronologically."""
    return sorted(key for key in keys if DUMP_KEY.fullmatch(key))


def prune_plan(sizes: dict[str, int | None], keep: int) -> tuple[list[str], list[str]]:
    """(delete, held) for a listing of `backups/` with each file's size.

    `keep` newest dumps stay, always. Of the older ones, those more than twice
    the size of the newest are held rather than deleted. A size that is not
    known (None) is never a reason to hold. `keep` <= 0 keeps everything.
    """
    ours = dump_keys(sizes)
    if keep <= 0 or len(ours) <= keep:
        return [], []
    newest = sizes.get(ours[-1])
    delete: list[str] = []
    held: list[str] = []
    for key in ours[:-keep]:
        size = sizes.get(key)
        if newest is not None and size is not None and size > 2 * newest:
            held.append(key)
        else:
            delete.append(key)
    return delete, held


def unreadable(path: str) -> str | None:
    """Why this dump cannot be restored from, or None if it reads to its end."""
    total = 0
    try:
        with gzip.open(path, "rb") as dump:
            while chunk := dump.read(1024 * 1024):
                total += len(chunk)
    except (OSError, EOFError, zlib.error) as exc:
        return f"it is not a complete gzip file ({exc})"
    if total == 0:
        return "it is empty"
    return None
