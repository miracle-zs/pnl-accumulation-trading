"""Command-line entry point for the Binance USDⓈ-M Futures service."""

from __future__ import annotations

import asyncio
import argparse
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path

from .adapters.sqlite import SQLiteStateStore, create_online_backup, verify_backup
from .runtime import (
    RuntimeConfig,
    run_application,
    validate_runtime_config,
)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(prog="pnl-trader")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser(
        "run",
        help="start the Binance mainnet service; this may submit real orders",
    )
    commands.add_parser(
        "validate-config",
        help="validate local runtime values and asset classifications without network access",
    )
    backup_parser = commands.add_parser(
        "backup",
        help="create an online SQLite backup without migrating the source database",
    )
    backup_parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("PNL_DATA_DIR", "./var")).expanduser(),
    )
    backup_parser.add_argument("--destination", type=Path)
    backup_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing destination atomically after validation",
    )
    backup_verify_parser = commands.add_parser(
        "verify-backup",
        help="check a SQLite backup and rehearse restoring it to a temporary database",
    )
    backup_verify_parser.add_argument("--path", type=Path, required=True)
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return
    if args.command == "validate-config":
        try:
            config = RuntimeConfig.from_environment()
            result = validate_runtime_config(config)
        except Exception as exc:
            logging.getLogger(__name__).error(
                "runtime configuration validation failed (%s): %s",
                type(exc).__name__,
                exc,
            )
            raise SystemExit(1) from None
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "backup":
        source_path = args.data_dir.expanduser().resolve() / "state.sqlite3"
        if not source_path.is_file():
            parser.error(f"state database does not exist: {source_path}")
        if args.destination is None:
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            destination = args.data_dir.expanduser() / "backups" / f"state-{timestamp}.sqlite3"
        else:
            destination = args.destination.expanduser()
        try:
            backup_path = create_online_backup(
                source_path,
                destination,
                overwrite=args.overwrite,
            )
        except Exception as exc:
            logging.getLogger(__name__).error(
                "SQLite online backup failed (%s)", type(exc).__name__
            )
            raise SystemExit(1) from None
        print(json.dumps({"status": "created", "path": str(backup_path)}, ensure_ascii=False, indent=2))
        return
    if args.command == "verify-backup":
        try:
            result = verify_backup(args.path)
        except Exception as exc:
            logging.getLogger(__name__).error(
                "SQLite backup verification failed (%s)", type(exc).__name__
            )
            raise SystemExit(1) from None
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "run":
        try:
            config = RuntimeConfig.from_environment()
            asyncio.run(run_application(config))
        except KeyboardInterrupt:
            logging.getLogger(__name__).info("shutdown requested")
        except Exception:
            logging.getLogger(__name__).exception(
                "trading application stopped during startup or runtime"
            )
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
