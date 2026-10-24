from contextlib import closing, redirect_stdout
from io import StringIO
import json
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pnl_trader.adapters.sqlite import (
    SQLiteStateStore,
    create_online_backup,
    verify_backup,
)
from pnl_trader.__main__ import main


class SQLiteBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "state.sqlite3"
        self.store = SQLiteStateStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_online_backup_contains_committed_wal_state_and_verifies(self) -> None:
        self.store.create_review_run(
            run_id="run-1",
            account_id="primary",
            strategy_version="v1",
            slot_start_ms=900_000,
            started_at_ms=900_005,
        )
        destination = self.root / "backups" / "latest.sqlite3"

        backup_path = self.store.backup_to(destination)

        self.assertEqual(backup_path, destination.resolve())
        with closing(sqlite3.connect(backup_path)) as connection, connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0],
                1,
            )
        self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)
        result = verify_backup(backup_path)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["integrity"], "ok")
        self.assertEqual(result["foreign_key_violations"], 0)
        self.assertEqual(result["restore_rehearsal"], "passed")
        self.assertEqual(result["restored_schema_version"], result["schema_version"])

    def test_standalone_backup_does_not_overwrite_without_explicit_flag(self) -> None:
        destination = self.root / "pre-upgrade.sqlite3"
        destination.write_bytes(b"keep this backup")

        with self.assertRaises(FileExistsError):
            create_online_backup(self.db_path, destination)

        self.assertEqual(destination.read_bytes(), b"keep this backup")

    def test_backup_refuses_to_target_the_live_database(self) -> None:
        with self.assertRaisesRegex(ValueError, "differ from the live database"):
            self.store.backup_to(self.db_path, overwrite=True)

    def test_verify_backup_rejects_a_corrupt_file(self) -> None:
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"not a SQLite database")

        with self.assertRaises(sqlite3.DatabaseError):
            verify_backup(corrupt)

    def test_cli_creates_and_verifies_a_backup(self) -> None:
        backup_output = StringIO()
        with patch.object(sys, "argv", ["pnl-trader", "backup", "--data-dir", str(self.root)]):
            with redirect_stdout(backup_output):
                main()
        created = json.loads(backup_output.getvalue())
        self.assertEqual(created["status"], "created")

        verify_output = StringIO()
        with patch.object(
            sys,
            "argv",
            ["pnl-trader", "verify-backup", "--path", created["path"]],
        ):
            with redirect_stdout(verify_output):
                main()
        verified = json.loads(verify_output.getvalue())
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["integrity"], "ok")
        self.assertEqual(verified["restore_rehearsal"], "passed")


if __name__ == "__main__":
    unittest.main()
