"""Bounded, overlap-safe synchronization of exchange income history."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Protocol

from .income_models import (
    MAX_INCOME_HISTORY_PAGE_SIZE,
    IncomeLedgerRecord,
    IncomeSyncCursor,
    IncomeSyncResult,
)


DAY_MS = 86_400_000


class IncomeHistoryPort(Protocol):
    async def get_income_history(
        self,
        start_time_ms: int,
        end_time_ms: int,
        *,
        limit: int = MAX_INCOME_HISTORY_PAGE_SIZE,
    ) -> tuple[IncomeLedgerRecord, ...]: ...


class IncomeLedgerStorePort(Protocol):
    def load_income_sync_cursor(self, account_id: str) -> IncomeSyncCursor | None: ...

    def record_income_page(
        self,
        account_id: str,
        records: tuple[IncomeLedgerRecord, ...],
        *,
        coverage_from_ms: int,
        reconciled_until_ms: int,
    ) -> int: ...


class IncomeHistoryRangeOverflow(RuntimeError):
    """The exchange page limit is still reached at millisecond resolution."""


@dataclass(slots=True)
class _RequestBudget:
    limit: int
    used: int = 0

    def consume(self) -> None:
        if self.used >= self.limit:
            raise _RequestBudgetReached
        self.used += 1


class _RequestBudgetReached(RuntimeError):
    pass


class IncomeLedgerSynchronizer:
    """Fetch income in bounded daily windows and commit only complete windows.

    The exchange endpoint caps each response at 1,000 rows. A saturated time
    range is bisected until every leaf is below that cap. A leaf still at the
    cap for one millisecond cannot be proven complete and therefore does not
    advance the watermark.
    """

    def __init__(
        self,
        *,
        history: IncomeHistoryPort,
        store: IncomeLedgerStorePort,
        overlap_ms: int = DAY_MS,
        initial_lookback_ms: int = 89 * DAY_MS,
        chunk_size_ms: int = DAY_MS,
        max_requests_per_run: int = 16,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if overlap_ms < 0 or initial_lookback_ms <= 0 or chunk_size_ms <= 0:
            raise ValueError("income sync windows must be positive; overlap may be zero")
        if max_requests_per_run <= 0:
            raise ValueError("max_requests_per_run must be positive")
        self._history = history
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
    ) -> IncomeSyncResult:
        """Reconcile through a fixed time, resuming safely after bounded runs."""

        if not account_id:
            raise ValueError("account_id must not be empty")
        end_ms = self._clock_ms() if through_ms is None else through_ms
        if isinstance(end_ms, bool) or not isinstance(end_ms, int) or end_ms < 0:
            raise ValueError("through_ms must be a non-negative integer")

        prior = self._store.load_income_sync_cursor(account_id)
        retention_floor_ms = max(0, end_ms - self._initial_lookback_ms)
        if prior is None:
            coverage_from_ms = retention_floor_ms
            start_ms = retention_floor_ms
        else:
            if end_ms < prior.reconciled_until_ms:
                raise ValueError("through_ms cannot precede the saved income watermark")
            if prior.reconciled_until_ms < retention_floor_ms:
                # The API no longer retains the missing interval. Move the
                # continuous-coverage boundary forward instead of claiming it.
                coverage_from_ms = retention_floor_ms
                start_ms = retention_floor_ms
            else:
                coverage_from_ms = (
                    prior.coverage_from_ms
                    if prior.coverage_from_ms is not None
                    else retention_floor_ms
                )
                start_ms = max(
                    coverage_from_ms,
                    prior.reconciled_until_ms - self._overlap_ms,
                )

        budget = _RequestBudget(self._max_requests_per_run)
        fetched_count = 0
        inserted_count = 0
        reconciled_until_ms = (
            prior.reconciled_until_ms if prior is not None else coverage_from_ms
        )
        complete = True
        window_start_ms = start_ms

        while window_start_ms <= end_ms:
            window_end_ms = min(window_start_ms + self._chunk_size_ms - 1, end_ms)
            try:
                records = await self._fetch_complete_range(
                    window_start_ms,
                    window_end_ms,
                    budget,
                )
            except _RequestBudgetReached:
                complete = False
                break

            fetched_count += len(records)
            inserted_count += self._store.record_income_page(
                account_id,
                records,
                coverage_from_ms=coverage_from_ms,
                reconciled_until_ms=window_end_ms,
            )
            reconciled_until_ms = max(reconciled_until_ms, window_end_ms)
            window_start_ms = window_end_ms + 1

        return IncomeSyncResult(
            account_id=account_id,
            requested_from_ms=start_ms,
            requested_until_ms=end_ms,
            covered_from_ms=coverage_from_ms,
            reconciled_until_ms=reconciled_until_ms,
            fetched_count=fetched_count,
            inserted_count=inserted_count,
            request_count=budget.used,
            complete=complete and reconciled_until_ms >= end_ms,
        )

    async def _fetch_complete_range(
        self,
        start_ms: int,
        end_ms: int,
        budget: _RequestBudget,
    ) -> tuple[IncomeLedgerRecord, ...]:
        budget.consume()
        records = await self._history.get_income_history(
            start_ms,
            end_ms,
            limit=MAX_INCOME_HISTORY_PAGE_SIZE,
        )
        if len(records) > MAX_INCOME_HISTORY_PAGE_SIZE:
            raise ValueError("income history adapter returned more rows than requested")
        if any(
            record.occurred_at_ms < start_ms or record.occurred_at_ms > end_ms
            for record in records
        ):
            raise ValueError("income history adapter returned an event outside the requested window")
        if len(records) < MAX_INCOME_HISTORY_PAGE_SIZE:
            return records
        if start_ms == end_ms:
            raise IncomeHistoryRangeOverflow(
                "income history remains capped at one millisecond; refusing to advance the cursor"
            )

        midpoint_ms = (start_ms + end_ms) // 2
        left = await self._fetch_complete_range(start_ms, midpoint_ms, budget)
        right = await self._fetch_complete_range(midpoint_ms + 1, end_ms, budget)
        return left + right
