"""FastAPI application for the 15-minute Periodic Review Trader Dashboard."""

from __future__ import annotations

from pathlib import Path
import subprocess
import threading
import time
from typing import Literal

from fastapi import FastAPI, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .service import DashboardQueryService

_UPTIME_CACHE_TTL_SECONDS = 60.0
_uptime_cache_lock = threading.Lock()
_uptime_cache: tuple[float, int | None] | None = None

_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Content-Security-Policy", _CONTENT_SECURITY_POLICY),
)


class _SecurityAndCacheHeadersMiddleware:
    """Attach HTTP security headers and explicit Cache-Control policies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope["path"]

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for header, value in _SECURITY_HEADERS:
                    headers[header] = value
                if path.startswith("/api/"):
                    headers["Cache-Control"] = "no-store"
                elif message["status"] < 400:
                    if path == "/tailwind.css" or path.startswith("/js/"):
                        headers["Cache-Control"] = "public, max-age=3600"
                    elif path.startswith("/fonts/"):
                        headers["Cache-Control"] = "public, max-age=604800"
            await send(message)

        await self.app(scope, receive, send_with_headers)


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
        properties = dict(
            line.split("=", 1)
            for line in result.stdout.splitlines()
            if "=" in line
        )
        if properties["ActiveState"] != "active":
            return None
        started_at_monotonic_us = int(properties["ExecMainStartTimestampMonotonic"])
    except (subprocess.SubprocessError, OSError, KeyError, ValueError):
        return None

    if started_at_monotonic_us <= 0:
        return None
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
        return None
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
    app.add_middleware(_SecurityAndCacheHeadersMiddleware)

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

    @app.get("/")
    def get_dashboard(request: Request) -> Response:
        html_path = Path(__file__).parent / "static" / "index.html"
        response = FileResponse(
            html_path,
            media_type="text/html",
            headers={"Cache-Control": "no-cache"},
            stat_result=html_path.stat(),
        )
        etag = response.headers["etag"]
        if_none_match = request.headers.get("if-none-match")
        if if_none_match and (
            if_none_match.strip() == "*"
            or etag in [tag.strip().removeprefix("W/") for tag in if_none_match.split(",")]
        ):
            return Response(
                status_code=304,
                headers={
                    "ETag": etag,
                    "Last-Modified": response.headers["last-modified"],
                    "Cache-Control": "no-cache",
                },
            )
        return response

    @app.get("/tailwind.css")
    def get_dashboard_stylesheet() -> FileResponse:
        css_path = Path(__file__).parent / "static" / "tailwind.css"
        return FileResponse(css_path, media_type="text/css")

    js_dir = Path(__file__).parent / "static" / "js"
    app.mount("/js", StaticFiles(directory=js_dir), name="js")

    fonts_dir = Path(__file__).parent / "static" / "fonts"
    app.mount("/fonts", StaticFiles(directory=fonts_dir), name="fonts")

    return app
