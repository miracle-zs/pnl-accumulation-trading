"""Command-line entry point for the Binance USDⓈ-M Futures service."""

from __future__ import annotations

import asyncio
import argparse
import json
import logging
import os
from pathlib import Path

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
        help="validate local runtime values without network access",
    )
    web_parser = commands.add_parser(
        "web",
        help="start the local web dashboard to monitor positions and performance",
    )
    web_parser.add_argument(
        "--data-dir",
        type=Path,
        help="directory containing state.sqlite3 (or set PNL_DATA_DIR)",
    )
    web_parser.add_argument("--port", type=int, default=8000, help="port to bind")
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
    if args.command == "web":
        try:
            import uvicorn
            from .web import create_app
            data_dir = args.data_dir or os.environ.get("PNL_DATA_DIR", "").strip()
            if not data_dir:
                parser.error("web requires --data-dir or PNL_DATA_DIR")
            app = create_app(data_dir=data_dir)
            print(f"\nPNL Trader Dashboard running at http://127.0.0.1:{args.port}")
            uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
        except KeyboardInterrupt:
            print("\nDashboard stopped.")
        except Exception as exc:
            logging.getLogger(__name__).error(
                "Failed to start web dashboard (%s): %s", type(exc).__name__, exc
            )
            raise SystemExit(1) from None
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
