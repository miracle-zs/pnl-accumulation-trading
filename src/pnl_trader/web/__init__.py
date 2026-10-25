"""Web dashboard package for PNL Accumulation Trader."""

from .app import create_app
from .service import DashboardQueryService

__all__ = ["create_app", "DashboardQueryService"]
