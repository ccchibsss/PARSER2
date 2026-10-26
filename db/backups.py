from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from db.connections import connect


def backup_sqlite(
    source: str | Path,
    destination: str | Path,
) -> Path:
    """Create a transactionally consistent backup, including active WAL data."""
    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    source_connection = connect(source_path, readonly=True)
    destination_connection = sqlite3.connect(destination_path)
    try:
        source_connection.backup(destination_connection)
        result = destination_connection.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"SQLite backup verification failed: {result!r}")
    finally:
        destination_connection.close()
        source_connection.close()
    return destination_path


def create_rotating_backup(
    source: str | Path,
    backup_directory: str | Path,
    *,
    keep: int = 14,
    timestamp: datetime | None = None,
) -> Path:
    source_path = Path(source)
    backup_dir = Path(backup_directory)
    stamp = (timestamp or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    destination = backup_dir / f"{source_path.stem}_{stamp}{source_path.suffix}"
    backup_sqlite(source_path, destination)

    candidates = sorted(
        backup_dir.glob(f"{source_path.stem}_*{source_path.suffix}"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for old_backup in candidates[max(keep, 1):]:
        old_backup.unlink()
    return destination

