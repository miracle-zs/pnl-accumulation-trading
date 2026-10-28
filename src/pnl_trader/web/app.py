"""FastAPI application for the 15-minute Periodic Review Trader Dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse

from .service import DashboardQueryService


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

    @app.get("/api/overview")
    def get_overview() -> dict[str, Any]:
        return service.get_overview()

    @app.get("/api/equity")
    def get_equity(
        period: Literal["24h", "7d", "30d"] = "7d",
    ) -> dict[str, Any]:
        return service.get_equity_history(period)

    @app.get("/api/analytics")
    def get_analytics() -> dict[str, Any]:
        return service.get_analytics()

    @app.get("/api/tier-distribution")
    def get_tier_distribution() -> dict[str, Any]:
        return service.get_tier_distribution()

    @app.get("/api/positions")
    def get_positions() -> list[dict[str, Any]]:
        return service.get_positions()

    @app.get("/api/fills")
    def get_fills(
        limit: int = Query(default=50, ge=1, le=500),
        side: Literal["BUY", "SELL"] | None = None,
    ) -> list[dict[str, Any]]:
        return service.get_recent_fills(limit=limit, side=side)

    @app.get("/", response_class=HTMLResponse)
    def get_dashboard() -> HTMLResponse:
        html_path = Path(__file__).parent / "static" / "index.html"
        if not html_path.is_file():
            return HTMLResponse("<h1>Dashboard HTML not found</h1>", status_code=404)
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    return app
