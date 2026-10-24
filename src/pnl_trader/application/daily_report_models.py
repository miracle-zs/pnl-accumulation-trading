"""Immutable inputs and revision metadata for daily financial reports."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..domain.models import SymbolState, TradeFillRecord
from .income_models import IncomeLedgerRecord, IncomeSyncCursor


@dataclass(frozen=True, slots=True)
class ReportAccountSnapshot:
    fetched_at_ms: int
    equity: Decimal
    wallet_balance: Decimal
    available_balance: Decimal
    unrealized_pnl: Decimal
    initial_margin: Decimal
    maintenance_margin: Decimal
    multi_assets_mode: bool

    def __post_init__(self) -> None:
        if self.fetched_at_ms < 0:
            raise ValueError("fetched_at_ms must be non-negative")
        for field in (
            "equity",
            "wallet_balance",
            "available_balance",
            "unrealized_pnl",
            "initial_margin",
            "maintenance_margin",
        ):
            value = getattr(self, field)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise TypeError(f"{field} must be a finite Decimal")
        if not isinstance(self.multi_assets_mode, bool):
            raise TypeError("multi_assets_mode must be bool")


@dataclass(frozen=True, slots=True)
class TradeHistoryCoverage:
    symbol: str
    coverage_from_ms: int | None
    reconciled_until_ms: int | None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        for value in (self.coverage_from_ms, self.reconciled_until_ms):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError("trade history coverage times must be non-negative integers")
        if (
            self.coverage_from_ms is not None
            and self.reconciled_until_ms is not None
            and self.coverage_from_ms > self.reconciled_until_ms
        ):
            raise ValueError("trade coverage cannot start after it ends")


@dataclass(frozen=True, slots=True)
class DailyReportInputs:
    account_id: str
    strategy_version: str
    period_start_ms: int
    period_end_ms: int
    generated_at_ms: int
    account_snapshots: tuple[ReportAccountSnapshot, ...]
    income_cursor: IncomeSyncCursor | None
    income_records: tuple[IncomeLedgerRecord, ...]
    trade_fills: tuple[TradeFillRecord, ...]
    trade_symbols: tuple[str, ...]
    trade_history_coverage: tuple[TradeHistoryCoverage, ...]
    symbol_states: tuple[SymbolState, ...]
    review_counts: tuple[tuple[str, int], ...]
    active_intent_counts: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if not self.account_id or not self.strategy_version:
            raise ValueError("account_id and strategy_version must not be empty")
        if (
            self.period_start_ms < 0
            or self.period_end_ms <= self.period_start_ms
            or self.generated_at_ms < self.period_end_ms
        ):
            raise ValueError("daily report input window is invalid")
        if tuple(sorted(self.trade_symbols)) != self.trade_symbols:
            raise ValueError("trade_symbols must be sorted")
        if len(set(self.trade_symbols)) != len(self.trade_symbols):
            raise ValueError("trade_symbols must not contain duplicates")


@dataclass(frozen=True, slots=True)
class DailyReportRevision:
    account_id: str
    strategy_version: str
    period_start_ms: int
    period_end_ms: int
    revision: int
    status: str
    content_hash: str
    generated_at_ms: int
    changed: bool
