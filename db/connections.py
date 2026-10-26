from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


DEFAULT_TIMEOUT_SECONDS = 15.0


def connect(
    path: str | Path,
    *,
    readonly: bool = False,
    row_factory=None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> sqlite3.Connection:
    """Open a consistently configured SQLite connection."""
    db_path = Path(path)
    if readonly:
        connection = sqlite3.connect(
            f"file:{db_path.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=timeout,
        )
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path, timeout=timeout)

    connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    connection.execute("PRAGMA foreign_keys = ON")
    if row_factory is not None:
        connection.row_factory = row_factory
    return connection


@contextmanager
def transaction(
    path: str | Path,
    *,
    immediate: bool = False,
    row_factory=None,
) -> Iterator[sqlite3.Connection]:
    """Commit atomically, roll back on failure, and always close."""
    connection = connect(path, row_factory=row_factory)
    try:
        if immediate:
            connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

@contextmanager
def read_connection(
    path: str | Path,
    *,
    row_factory=None,
) -> Iterator[sqlite3.Connection]:
    connection = connect(path, readonly=True, row_factory=row_factory)
    try:
        yield connection
    finally:
        connection.close()
