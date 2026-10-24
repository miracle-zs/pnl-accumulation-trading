"""Bounded account trade-history backfill and financial-ledger reconciliation."""

from __future__ import annotations

import time
from collections import deque
from decimal import Decimal
from typing import Callable, Protocol

from ..domain.models import TradeFillRecord
from .income_models import IncomeLedgerRecord, IncomeSyncCursor
from .market_models import ExchangeInfoSnapshot
from .trade_history_models import (
    DAY_MS,
    MAX_TRADE_HISTORY_PAGE_SIZE,
    MAX_TRADE_HISTORY_RANGE_MS,
    TradeHistoryCursor,
    TradeHistorySyncResult,
    TradeHistorySymbolUnavailable,
    TradeIncomeMismatch,
    TradeIncomeReconciliationResult,
)


class TradeHistoryPort(Protocol):
    async def get_account_trade_history(
        self,
        symbol: str,
        start_time_ms: int,
        end_time_ms: int,
        *,
        limit: int = MAX_TRADE_HISTORY_PAGE_SIZE,
    ) -> tuple[TradeFillRecord, ...]: ...


class TradeHistoryCatalogPort(Protocol):
    async def get_exchange_info_snapshot(self) -> ExchangeInfoSnapshot: ...


class TradeHistoryStorePort(Protocol):
    def load_trade_history_cursor(
        self, account_id: str, symbol: str
    ) -> TradeHistoryCursor | None: ...

    def load_trade_history_rotation_cursor(self, account_id: str) -> str | None: ...

    def list_trade_history_symbols(self, account_id: str) -> tuple[str, ...]: ...

    def save_trade_history_rotation_cursor(
        self, account_id: str, next_symbol: str, *, updated_at_ms: int
    ) -> None: ...

    def record_trade_history_page(
        self,
        account_id: str,
        symbol: str,
        records: tuple[TradeFillRecord, ...],
        *,
        coverage_from_ms: int,
        reconciled_until_ms: int,
        next_symbol: str,
        updated_at_ms: int,
    ) -> int: ...

    def trade_history_window_is_covered(
        self,
        account_id: str,
        symbols: tuple[str, ...],
        start_time_ms: int,
        end_time_ms: int,
    ) -> bool: ...

    def trade_history_watermark(
        self, account_id: str, symbols: tuple[str, ...], *, default_ms: int
    ) -> int: ...

    def list_trade_fills(
        self,
        account_id: str,
        start_time_ms: int,
        end_time_ms: int,
    ) -> tuple[TradeFillRecord, ...]: ...

    def load_income_sync_cursor(self, account_id: str) -> IncomeSyncCursor | None: ...

    def list_income_records(
        self,
        account_id: str,
        start_time_ms: int,
        end_time_ms: int,
    ) -> tuple[IncomeLedgerRecord, ...]: ...


class _RequestBudgetReached(RuntimeError):
    pass


class TradeHistoryRangeOverflow(RuntimeError):
    """A one-millisecond history window is still capped and cannot be advanced."""


class _RequestBudget:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.used = 0

    def consume(self) -> None:
        if self.used >= self.maximum:
            raise _RequestBudgetReached
        self.used += 1


class _SyncWindowCounts:
    def __init__(self) -> None:
        self.fetched = 0
        self.inserted = 0
        self.completed_pages = 0


class TradeHistorySynchronizer:
    """Sync per-symbol fills with explicit coverage and a bounded API budget."""

    def __init__(
        self,
        *,
        history: TradeHistoryPort,
        catalog: TradeHistoryCatalogPort,
        store: TradeHistoryStorePort,
        overlap_ms: int = DAY_MS,
        initial_lookback_ms: int = 89 * DAY_MS,
        chunk_size_ms: int = MAX_TRADE_HISTORY_RANGE_MS,
        max_requests_per_run: int = 64,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if overlap_ms < 0 or initial_lookback_ms <= 0:
            raise ValueError("history lookback must be positive and overlap non-negative")
        if not 0 < chunk_size_ms <= MAX_TRADE_HISTORY_RANGE_MS:
            raise ValueError("chunk_size_ms must be between 1 ms and seven days")
        if max_requests_per_run <= 0:
            raise ValueError("max_requests_per_run must be positive")
        self._history = history
        self._catalog = catalog
        self._store = store
        self._overlap_ms = overlap_ms
        self._initial_lookback_ms = initial_lookback_ms
        self._chunk_size_ms = chunk_size_ms
        self._max_requests_per_run = max_requests_per_run
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    async def sync_to(
        self,
        account_id: str,
        *,
        through_ms: int | None = None,
    ) -> TradeHistorySyncResult:
        if not account_id:
            raise ValueError("account_id must not be empty")
        end_ms = self._clock_ms() if through_ms is None else through_ms
        if isinstance(end_ms, bool) or not isinstance(end_ms, int) or end_ms < 0:
            raise ValueError("through_ms must be a non-negative integer")

        snapshot = await self._catalog.get_exchange_info_snapshot()
        symbols = tuple(
            sorted(
                {instrument.symbol for instrument in snapshot.instruments}
                | set(self._store.list_trade_history_symbols(account_id))
            )
        )
        coverage_from_ms = max(0, end_ms - self._initial_lookback_ms)
        if not symbols:
            return TradeHistorySyncResult(
                account_id=account_id,
                candidate_symbol_count=0,
                processed_symbol_count=0,
                fetched_count=0,
                inserted_count=0,
                request_count=0,
                covered_from_ms=coverage_from_ms,
                requested_until_ms=end_ms,
                reconciled_until_ms=end_ms,
                candidate_symbols=(),
                blocked_symbols=(),
                complete=True,
            )

        rotation = self._store.load_trade_history_rotation_cursor(account_id)
        start_index = symbols.index(rotation) if rotation in symbols else 0
        ordered = symbols[start_index:] + symbols[:start_index]
        symbol_indexes = {symbol: index for index, symbol in enumerate(ordered)}
        queue = deque(ordered)
        budget = _RequestBudget(self._max_requests_per_run)
        fetched_count = 0
        inserted_count = 0
        processed_symbols: set[str] = set()
        blocked_symbols: set[str] = set()

        while queue and budget.used < budget.maximum:
            symbol = queue.popleft()
            prior = self._store.load_trade_history_cursor(account_id, symbol)
            if prior is not None and end_ms < prior.reconciled_until_ms:
                raise ValueError("through_ms cannot precede a saved symbol watermark")
            if prior is None or prior.reconciled_until_ms < coverage_from_ms:
                symbol_coverage_from_ms = coverage_from_ms
                start_ms = coverage_from_ms
                prior_watermark_ms = coverage_from_ms
            else:
                symbol_coverage_from_ms = (
                    prior.coverage_from_ms
                    if prior.coverage_from_ms is not None
                    else coverage_from_ms
                )
                if prior.reconciled_until_ms < end_ms - self._overlap_ms:
                    start_ms = prior.reconciled_until_ms + 1
                else:
                    start_ms = max(
                        symbol_coverage_from_ms,
                        prior.reconciled_until_ms - self._overlap_ms,
                    )
                prior_watermark_ms = prior.reconciled_until_ms
            if start_ms > end_ms:
                continue
            window_end_ms = min(start_ms + self._chunk_size_ms, end_ms)
            next_symbol = ordered[(symbol_indexes[symbol] + 1) % len(ordered)]
            counts = _SyncWindowCounts()
            try:
                await self._fetch_and_store_range(
                    account_id,
                    symbol,
                    start_ms,
                    window_end_ms,
                    symbol_coverage_from_ms,
                    next_symbol,
                    budget,
                    counts,
                )
            except _RequestBudgetReached:
                fetched_count += counts.fetched
                inserted_count += counts.inserted
                if counts.completed_pages:
                    processed_symbols.add(symbol)
                self._store.save_trade_history_rotation_cursor(
                    account_id,
                    next_symbol,
                    updated_at_ms=self._clock_ms(),
                )
                break
            except TradeHistoryRangeOverflow:
                fetched_count += counts.fetched
                inserted_count += counts.inserted
                if counts.completed_pages:
                    processed_symbols.add(symbol)
                blocked_symbols.add(symbol)
                self._store.save_trade_history_rotation_cursor(
                    account_id,
                    next_symbol,
                    updated_at_ms=self._clock_ms(),
                )
                continue
            except TradeHistorySymbolUnavailable:
                fetched_count += counts.fetched
                inserted_count += counts.inserted
                if counts.completed_pages:
                    processed_symbols.add(symbol)
                blocked_symbols.add(symbol)
                self._store.save_trade_history_rotation_cursor(
                    account_id,
                    next_symbol,
                    updated_at_ms=self._clock_ms(),
                )
                continue

            fetched_count += counts.fetched
            inserted_count += counts.inserted
            processed_symbols.add(symbol)
            if window_end_ms > prior_watermark_ms and window_end_ms < end_ms:
                queue.append(symbol)

        complete = (
            not blocked_symbols
            and self._store.trade_history_window_is_covered(
                account_id,
                symbols,
                coverage_from_ms,
                end_ms,
            )
        )
        reconciled_until_ms = max(
            coverage_from_ms,
            self._store.trade_history_watermark(
                account_id,
                symbols,
                default_ms=coverage_from_ms,
            ),
        )
        return TradeHistorySyncResult(
            account_id=account_id,
            candidate_symbol_count=len(symbols),
            processed_symbol_count=len(processed_symbols),
            fetched_count=fetched_count,
            inserted_count=inserted_count,
            request_count=budget.used,
            covered_from_ms=coverage_from_ms,
            requested_until_ms=end_ms,
            reconciled_until_ms=min(reconciled_until_ms, end_ms),
            candidate_symbols=symbols,
            blocked_symbols=tuple(sorted(blocked_symbols)),
            complete=complete,
        )

    async def _fetch_and_store_range(
        self,
        account_id: str,
        symbol: str,
        start_ms: int,
        end_ms: int,
        coverage_from_ms: int,
        next_symbol: str,
        budget: _RequestBudget,
        counts: _SyncWindowCounts,
    ) -> None:
        budget.consume()
        records = await self._history.get_account_trade_history(
            symbol,
            start_ms,
            end_ms,
            limit=MAX_TRADE_HISTORY_PAGE_SIZE,
        )
        if len(records) > MAX_TRADE_HISTORY_PAGE_SIZE:
            raise ValueError("trade history adapter returned more rows than requested")
        if any(
            record.symbol != symbol
            or record.traded_at_ms < start_ms
            or record.traded_at_ms > end_ms
            for record in records
        ):
            raise ValueError("trade history adapter returned a fill outside its window")
        if len(records) < MAX_TRADE_HISTORY_PAGE_SIZE:
            counts.fetched += len(records)
            counts.inserted += self._store.record_trade_history_page(
                account_id,
                symbol,
                records,
                coverage_from_ms=coverage_from_ms,
                reconciled_until_ms=end_ms,
                next_symbol=next_symbol,
                updated_at_ms=self._clock_ms(),
            )
            counts.completed_pages += 1
            return
        if start_ms == end_ms:
            raise TradeHistoryRangeOverflow(
                "trade history remains capped at one millisecond; cursor was not advanced"
            )
        midpoint_ms = (start_ms + end_ms) // 2
        await self._fetch_and_store_range(
            account_id,
            symbol,
            start_ms,
            midpoint_ms,
            coverage_from_ms,
            next_symbol,
            budget,
            counts,
        )
        await self._fetch_and_store_range(
            account_id,
            symbol,
            midpoint_ms + 1,
            end_ms,
            coverage_from_ms,
            next_symbol,
            budget,
            counts,
        )


class TradeIncomeReconciler:
    """Compare duplicated trade facts while keeping income out of PnL totals."""

    def __init__(self, store: TradeHistoryStorePort) -> None:
        self._store = store

    def reconcile(
        self,
        account_id: str,
        symbols: tuple[str, ...],
        *,
        start_time_ms: int,
        end_time_ms: int,
    ) -> TradeIncomeReconciliationResult:
        if not account_id:
            raise ValueError("account_id must not be empty")
        if start_time_ms < 0 or end_time_ms < start_time_ms:
            raise ValueError("reconciliation window is invalid")
        income_cursor = self._store.load_income_sync_cursor(account_id)
        income_covered = (
            income_cursor is not None
            and income_cursor.coverage_from_ms is not None
            and income_cursor.coverage_from_ms <= start_time_ms
            and income_cursor.reconciled_until_ms >= end_time_ms
        )
        trade_covered = self._store.trade_history_window_is_covered(
            account_id,
            symbols,
            start_time_ms,
            end_time_ms,
        )
        fills = self._store.list_trade_fills(account_id, start_time_ms, end_time_ms)
        incomes = self._store.list_income_records(account_id, start_time_ms, end_time_ms)
        return reconcile_trade_income_records(
            account_id=account_id,
            symbols=symbols,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            income_covered=income_covered,
            trade_covered=trade_covered,
            fills=fills,
            incomes=incomes,
        )


def reconcile_trade_income_records(
    *,
    account_id: str,
    symbols: tuple[str, ...],
    start_time_ms: int,
    end_time_ms: int,
    income_covered: bool,
    trade_covered: bool,
    fills: tuple[TradeFillRecord, ...],
    incomes: tuple[IncomeLedgerRecord, ...],
) -> TradeIncomeReconciliationResult:
    """Compare one already-loaded window so reports share one ledger view."""

    coverage_complete = income_covered and trade_covered
    indexed: dict[tuple[str, str, str], list[IncomeLedgerRecord]] = {}
    for income in incomes:
        if income.symbol is None or income.trade_id is None:
            continue
        if income.income_type not in {"COMMISSION", "REALIZED_PNL"}:
            continue
        key = (income.income_type, income.symbol, income.trade_id)
        indexed.setdefault(key, []).append(income)

    mismatches: list[TradeIncomeMismatch] = []
    used_income_ids: set[tuple[str, str, str]] = set()
    matched_commission_count = 0
    matched_realized_pnl_count = 0
    missing_trade_events = 0
    for fill in fills:
        expected_by_type = (
            ("COMMISSION", fill.commission_asset, -fill.commission),
            ("REALIZED_PNL", fill.margin_asset or "", fill.realized_pnl),
        )
        for income_type, asset, expected_amount in expected_by_type:
            if expected_amount == Decimal("0"):
                continue
            key = (income_type, fill.symbol, fill.exchange_trade_id)
            candidates = indexed.get(key, [])
            matching = [event for event in candidates if not asset or event.asset == asset]
            if not matching:
                if coverage_complete:
                    missing_trade_events += 1
                continue
            actual_amount = sum((event.amount for event in matching), Decimal("0"))
            used_income_ids.update(
                (event.transaction_id, event.income_type, event.asset)
                for event in matching
            )
            if actual_amount != expected_amount:
                mismatches.append(
                    TradeIncomeMismatch(
                        income_type=income_type,
                        symbol=fill.symbol,
                        trade_id=fill.exchange_trade_id,
                        asset=matching[0].asset,
                        trade_amount=str(expected_amount),
                        income_amount=str(actual_amount),
                    )
                )
            elif income_type == "COMMISSION":
                matched_commission_count += 1
            else:
                matched_realized_pnl_count += 1

    unmatched_income_count: int | None = None
    unmatched_trade_count: int | None = None
    if coverage_complete:
        unmatched_income_count = sum(
            1
            for income in incomes
            if income.income_type in {"COMMISSION", "REALIZED_PNL"}
            and income.symbol is not None
            and income.trade_id is not None
            and (income.transaction_id, income.income_type, income.asset)
            not in used_income_ids
        )
        unmatched_trade_count = missing_trade_events

    return TradeIncomeReconciliationResult(
        account_id=account_id,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
        coverage_complete=coverage_complete,
        matched_commission_count=matched_commission_count,
        matched_realized_pnl_count=matched_realized_pnl_count,
        amount_mismatches=tuple(mismatches),
        unmatched_trade_event_count=unmatched_trade_count,
        unmatched_income_event_count=unmatched_income_count,
    )
