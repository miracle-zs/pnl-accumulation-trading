"""Bounded account trade-history synchronization values."""

from __future__ import annotations

from dataclasses import dataclass


DAY_MS = 86_400_000
MAX_TRADE_HISTORY_PAGE_SIZE = 1_000
MAX_TRADE_HISTORY_RANGE_MS = 7 * DAY_MS


class TradeHistorySymbolUnavailable(RuntimeError):
    """The exchange no longer accepts trade-history queries for this symbol."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        super().__init__(f"trade history is unavailable for symbol {symbol}")


@dataclass(frozen=True, slots=True)
class TradeHistoryCursor:
    """Contiguous coverage marker for one account and symbol."""

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
            raise ValueError("reconciled_until_ms must be a non-negative integer")
        if self.coverage_from_ms is not None and (
            isinstance(self.coverage_from_ms, bool)
            or not isinstance(self.coverage_from_ms, int)
            or self.coverage_from_ms < 0
        ):
            raise ValueError("coverage_from_ms must be a non-negative integer or None")
        if (
            self.coverage_from_ms is not None
            and self.coverage_from_ms > self.reconciled_until_ms
        ):
            raise ValueError("coverage_from_ms cannot exceed reconciled_until_ms")


@dataclass(frozen=True, slots=True)
class TradeHistorySyncResult:
    """Outcome of one bounded pass over currently known contract symbols."""

    account_id: str
    candidate_symbol_count: int
    processed_symbol_count: int
    fetched_count: int
    inserted_count: int
    request_count: int
    covered_from_ms: int
    requested_until_ms: int
    reconciled_until_ms: int
    candidate_symbols: tuple[str, ...]
    blocked_symbols: tuple[str, ...]
    complete: bool

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("account_id must not be empty")
        for name in (
            "candidate_symbol_count",
            "processed_symbol_count",
            "fetched_count",
            "inserted_count",
            "request_count",
            "covered_from_ms",
            "requested_until_ms",
            "reconciled_until_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.reconciled_until_ms < self.covered_from_ms:
            raise ValueError("reconciled_until_ms must not precede covered_from_ms")
        if self.requested_until_ms < self.covered_from_ms:
            raise ValueError("requested_until_ms must not precede covered_from_ms")
        if self.reconciled_until_ms > self.requested_until_ms:
            raise ValueError("reconciled_until_ms cannot exceed requested_until_ms")
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be a bool")
        if not isinstance(self.candidate_symbols, tuple) or any(
            not isinstance(symbol, str) or not symbol for symbol in self.candidate_symbols
        ):
            raise TypeError("candidate_symbols must be a tuple of non-empty strings")
        if len(set(self.candidate_symbols)) != len(self.candidate_symbols):
            raise ValueError("candidate_symbols must not contain duplicates")
        if self.candidate_symbol_count != len(self.candidate_symbols):
            raise ValueError("candidate_symbol_count must match candidate_symbols")
        if not isinstance(self.blocked_symbols, tuple) or any(
            not isinstance(symbol, str) or not symbol for symbol in self.blocked_symbols
        ):
            raise TypeError("blocked_symbols must be a tuple of non-empty strings")
        if not set(self.blocked_symbols).issubset(self.candidate_symbols):
            raise ValueError("blocked_symbols must be part of candidate_symbols")

    @property
    def blocked_symbol_count(self) -> int:
        return len(self.blocked_symbols)


@dataclass(frozen=True, slots=True)
class TradeIncomeMismatch:
    """A fill and income event share an identity but disagree on amount."""

    income_type: str
    symbol: str
    trade_id: str
    asset: str
    trade_amount: str
    income_amount: str


@dataclass(frozen=True, slots=True)
class TradeIncomeReconciliationResult:
    """Auditable comparison of trade-fill amounts with income-history events."""

    account_id: str
    start_time_ms: int
    end_time_ms: int
    coverage_complete: bool
    matched_commission_count: int
    matched_realized_pnl_count: int
    amount_mismatches: tuple[TradeIncomeMismatch, ...]
    unmatched_trade_event_count: int | None
    unmatched_income_event_count: int | None

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("account_id must not be empty")
        if self.start_time_ms < 0 or self.end_time_ms < self.start_time_ms:
            raise ValueError("reconciliation window is invalid")
        for name in ("matched_commission_count", "matched_realized_pnl_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("unmatched_trade_event_count", "unmatched_income_event_count"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")
        if not isinstance(self.amount_mismatches, tuple) or any(
            not isinstance(item, TradeIncomeMismatch) for item in self.amount_mismatches
        ):
            raise TypeError("amount_mismatches must be a tuple of TradeIncomeMismatch")
        if not isinstance(self.coverage_complete, bool):
            raise TypeError("coverage_complete must be a bool")
        if self.coverage_complete and (
            self.unmatched_trade_event_count is None
            or self.unmatched_income_event_count is None
        ):
            raise ValueError("complete coverage requires unmatched-event counts")
        if not self.coverage_complete and (
            self.unmatched_trade_event_count is not None
            or self.unmatched_income_event_count is not None
        ):
            raise ValueError("partial coverage cannot claim unmatched-event counts")
