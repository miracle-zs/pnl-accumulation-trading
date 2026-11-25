"""FastAPI application for the 15-minute Periodic Review Trader Dashboard."""

from __future__ import annotations

from pathlib import Path
import subprocess
import threading
import time
from typing import Literal

from fastapi import FastAPI, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .service import DashboardQueryService

_UPTIME_CACHE_TTL_SECONDS = 60.0
_uptime_cache_lock = threading.Lock()
_uptime_cache: tuple[float, int | None] | None = None


def _query_service_start_monotonic_us() -> int | None:
    try:
        result = subprocess.run(
            (
                "systemctl",
                "show",
                "pnl-trader.service",
                "--property=ActiveState",
                "--property=ExecMainStartTimestampMonotonic",
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except FileNotFoundError:
        return None

    properties = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if "=" in line
    )
    if properties["ActiveState"] != "active":
        return None

    started_at_monotonic_us = int(properties["ExecMainStartTimestampMonotonic"])
    if started_at_monotonic_us <= 0:
        raise ValueError(
            f"active pnl-trader.service has invalid start timestamp: {started_at_monotonic_us}"
        )
    return started_at_monotonic_us


def _trading_service_uptime_seconds() -> int | None:
    """Read the trading service start time from systemd, cached across dashboard polls."""
    global _uptime_cache
    now = time.monotonic()
    with _uptime_cache_lock:
        if _uptime_cache is not None and now - _uptime_cache[0] < _UPTIME_CACHE_TTL_SECONDS:
            started_at_monotonic_us = _uptime_cache[1]
        else:
            started_at_monotonic_us = _query_service_start_monotonic_us()
            _uptime_cache = (now, started_at_monotonic_us)
    if started_at_monotonic_us is None:
        return None
    elapsed_seconds = int(now - started_at_monotonic_us / 1_000_000)
    if elapsed_seconds < 0:
        raise ValueError(f"computed negative service uptime: {elapsed_seconds}")
    return elapsed_seconds


def create_app(data_dir: Path | str) -> FastAPI:
    data_dir = Path(data_dir).expanduser().resolve()
    db_path = data_dir / "state.sqlite3"
    service = DashboardQueryService(db_path)

    app = FastAPI(
        title="PNL Accumulation Trading Dashboard",
        description="Read-only local dashboard for the 15-minute periodic review trading ledger",
        version="1.0.0",
    )
    app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)

    @app.get("/api/dashboard")
    def get_dashboard_snapshot(
        period: Literal["24h", "7d", "30d"] = "7d",
        fills_limit: int = Query(default=12, ge=1, le=500),
    ) -> dict[str, object]:
        snapshot = service.get_dashboard_snapshot(period=period, fills_limit=fills_limit)
        overview = snapshot["overview"]
        assert isinstance(overview, dict)
        overview["trading_uptime_seconds"] = _trading_service_uptime_seconds()
        return snapshot

    @app.get("/api/overview")
    def get_overview() -> dict[str, object]:
        overview = service.get_overview()
        overview["trading_uptime_seconds"] = _trading_service_uptime_seconds()
        return overview

    @app.get("/api/equity")
    def get_equity(
        period: Literal["24h", "7d", "30d"] = "7d",
    ) -> dict[str, object]:
        return service.get_equity_history(period)

    @app.get("/api/analytics")
    def get_analytics() -> dict[str, object]:
        return service.get_analytics()

    @app.get("/api/tier-distribution")
    def get_tier_distribution() -> dict[str, object]:
        return service.get_tier_distribution()

    @app.get("/api/positions")
    def get_positions() -> list[dict[str, object]]:
        return service.get_positions()

    @app.get("/api/fills")
    def get_fills(
        limit: int = Query(default=50, ge=1, le=500),
        side: Literal["BUY", "SELL"] | None = None,
    ) -> list[dict[str, object]]:
        return service.get_recent_fills(limit=limit, side=side)

    @app.get("/", response_class=HTMLResponse)
    def get_dashboard() -> HTMLResponse:
        html_path = Path(__file__).parent / "static" / "index.html"
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    @app.get("/tailwind.css")
    def get_dashboard_stylesheet() -> FileResponse:
        css_path = Path(__file__).parent / "static" / "tailwind.css"
        return FileResponse(css_path, media_type="text/css")

    js_dir = Path(__file__).parent / "static" / "js"
    app.mount("/js", StaticFiles(directory=js_dir), name="js")

    return app
