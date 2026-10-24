"""Consistent, atomic backups for the file-backed SQLite state store."""

from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile


def create_online_backup(
    source_path: str | Path,
    destination: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Back up an existing database without migrating or modifying the source."""

    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {source}")
    uri = f"{source.as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        return _write_backup_from_connection(
            connection,
            source,
            destination,
            overwrite=overwrite,
        )


def _write_backup_from_connection(
    source_connection: sqlite3.Connection,
    source_path: str | Path,
    destination: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Write and validate a temporary SQLite snapshot, then publish it atomically."""

    source = Path(source_path).expanduser().resolve()
    requested_destination = Path(destination).expanduser().absolute()
    if requested_destination.is_symlink():
        raise ValueError("backup destination must not be a symbolic link")
    target = requested_destination.resolve()
    if source == target:
        raise ValueError("backup destination must differ from the live database")
    if target.exists() and not overwrite:
        raise FileExistsError(f"backup destination already exists: {target}")

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with closing(sqlite3.connect(temporary)) as backup_connection:
            source_connection.backup(backup_connection, pages=256, sleep=0.01)
            integrity = backup_connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                detail = "missing result" if integrity is None else str(integrity[0])
                raise sqlite3.DatabaseError(f"backup integrity check failed: {detail}")
            foreign_key_error = backup_connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchone()
            if foreign_key_error is not None:
                raise sqlite3.DatabaseError("backup contains a foreign key violation")

        os.chmod(temporary, 0o600)
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        if overwrite:
            os.replace(temporary, target)
        else:
            # A hard link makes the no-overwrite guarantee atomic if another
            # backup process publishes the same destination concurrently.
            os.link(temporary, target)
            temporary.unlink()
        return target
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def verify_backup(path: str | Path) -> dict[str, object]:
    """Check a backup and rehearse opening a temporary restored copy."""

    backup = Path(path).expanduser().resolve()
    if not backup.is_file():
        raise FileNotFoundError(f"backup file does not exist: {backup}")
    uri = f"{backup.as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            detail = "missing result" if integrity is None else str(integrity[0])
            raise sqlite3.DatabaseError(f"backup integrity check failed: {detail}")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise sqlite3.DatabaseError("backup contains a foreign key violation")
        try:
            schema_version = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise sqlite3.DatabaseError("backup has no readable schema migration history") from exc
        if schema_version is None:
            raise sqlite3.DatabaseError("backup has no applied schema migrations")

        with tempfile.TemporaryDirectory(prefix="pnl-backup-restore-") as directory:
            restored_path = Path(directory) / "state.sqlite3"
            with closing(sqlite3.connect(restored_path)) as restored_connection:
                connection.backup(restored_connection, pages=256, sleep=0.01)

            # Exercise the same schema checks and migrations used by the live
            # application, while keeping all writes inside the temporary copy.
            from .store import SQLiteStateStore

            with SQLiteStateStore(restored_path):
                pass

            restored_uri = f"{restored_path.as_uri()}?mode=ro"
            with closing(sqlite3.connect(restored_uri, uri=True)) as restored_connection:
                restored_integrity = restored_connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()
                if restored_integrity is None or restored_integrity[0] != "ok":
                    raise sqlite3.DatabaseError(
                        "temporary restored database failed integrity check"
                    )
                restored_schema_version = restored_connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                if restored_schema_version is None or restored_schema_version < schema_version:
                    raise sqlite3.DatabaseError(
                        "temporary restored database lost schema migration history"
                    )
    return {
        "status": "verified",
        "path": str(backup),
        "integrity": "ok",
        "foreign_key_violations": 0,
        "schema_version": int(schema_version),
        "restore_rehearsal": "passed",
        "restored_schema_version": int(restored_schema_version),
    }
