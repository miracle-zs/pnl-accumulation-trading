"""Immutable records and progress markers for exchange income history."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


MAX_INCOME_HISTORY_PAGE_SIZE = 1_000


@dataclass(frozen=True, slots=True)
class IncomeLedgerRecord:
    """One exchange income event, kept in its original asset and sign."""

    transaction_id: str
    income_type: str
    asset: str
    amount: Decimal
    occurred_at_ms: int
    symbol: str | None = None
    info: str | None = None
    trade_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("transaction_id", "income_type", "asset"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.amount, Decimal) or not self.amount.is_finite():
            raise TypeError("amount must be a finite Decimal")
        if isinstance(self.occurred_at_ms, bool) or not isinstance(self.occurred_at_ms, int):
            raise TypeError("occurred_at_ms must be an integer")
        if self.occurred_at_ms < 0:
            raise ValueError("occurred_at_ms must be non-negative")
        for name in ("symbol", "info", "trade_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be a non-empty string or None")


@dataclass(frozen=True, slots=True)
class IncomeSyncCursor:
    """Contiguous income-history coverage for one account."""

    cursor: str
    reconciled_until_ms: int
    coverage_from_ms: int | None

    def __post_init__(self) -> None:
        if not self.cursor:
            raise ValueError("cursor must not be empty")
        if (
            isinstance(self.reconciled_until_ms, bool)
            or not isinstance(self.reconciled_until_ms, int)
            or self.reconciled_until_ms < 0
        ):
            raise ValueError("reconciled_until_ms must be non-negative")
        if self.coverage_from_ms is not None and (
            isinstance(self.coverage_from_ms, bool)
            or not isinstance(self.coverage_from_ms, int)
            or self.coverage_from_ms < 0
        ):
            raise ValueError("coverage_from_ms must be non-negative")
        if (
            self.coverage_from_ms is not None
            and self.coverage_from_ms > self.reconciled_until_ms
        ):
            raise ValueError("coverage_from_ms cannot exceed reconciled_until_ms")


@dataclass(frozen=True, slots=True)
class IncomeSyncResult:
    """Outcome of a bounded synchronization run."""

    account_id: str
    requested_from_ms: int
    requested_until_ms: int
    covered_from_ms: int
    reconciled_until_ms: int
    fetched_count: int
    inserted_count: int
    request_count: int
    complete: bool

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("account_id must not be empty")
        for name in (
            "requested_from_ms",
            "requested_until_ms",
            "covered_from_ms",
            "reconciled_until_ms",
            "fetched_count",
            "inserted_count",
            "request_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.requested_until_ms < self.requested_from_ms:
            raise ValueError("requested_until_ms must not precede requested_from_ms")
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be a bool")
