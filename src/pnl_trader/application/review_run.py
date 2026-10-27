"""Coordinate one 15-minute review across the eligible symbols."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
import logging
import time
from typing import Callable, Mapping

from ..domain.models import FULL_CLOSE_ACTIONS, Lifecycle
from .execution import ExecutionCoordinator, ExecutionResult
from .execution_models import IntentStatus
from .ports import (
    AccountEquitySnapshotPort,
    MarketPriceSnapshotPort,
    ReviewBatchSourcePort,
    StateStorePort,
)
from .review_executor import SymbolReviewExecutor
from .review_run_models import ReviewBatch


logger = logging.getLogger(__name__)
_EXECUTION_ISSUE_STATUSES = {
    IntentStatus.UNKNOWN,
    IntentStatus.REJECTED,
    IntentStatus.CANCELED,
    IntentStatus.EXPIRED,
}


def _log_execution_issue(symbol: str, result: ExecutionResult | None) -> None:
    if result is None or result.status not in _EXECUTION_ISSUE_STATUSES:
        return
    logger.warning(
        "market order did not complete normally: symbol=%s intent_id=%s status=%s detail=%s",
        symbol,
        result.intent_id,
        result.status.value,
        result.detail,
    )


@dataclass(frozen=True, slots=True)
class ReviewRunResult:
    candidate_count: int
    result_counts: tuple[tuple[str, int], ...]


class ReviewRunCoordinator:
    """Recover orders, reconcile one snapshot, then review symbols in order."""

    def __init__(
        self,
        *,
        store: StateStorePort,
        execution: ExecutionCoordinator,
        symbol_reviews: SymbolReviewExecutor,
        batch_source: ReviewBatchSourcePort,
        market_prices: MarketPriceSnapshotPort,
        account_equity: AccountEquitySnapshotPort | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._execution = execution
        self._symbol_reviews = symbol_reviews
        self._batch_source = batch_source
        self._market_prices = market_prices
        self._account_equity = account_equity
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    async def run_slot(
        self,
        *,
        slot_start_ms: int,
    ) -> ReviewRunResult:
        await self._execution.recover_pending()
        batch, latest_prices = await asyncio.gather(
            self._batch_source.load_review_batch(slot_start_ms),
            self._market_prices.get_latest_prices(),
        )
        result = await self._run_batch(batch, latest_prices)
        await self._execution.recover_full_close_fills()
        await self._record_account_equity_snapshot()
        return result

    async def _record_account_equity_snapshot(self) -> None:
        if self._account_equity is None:
            return
        try:
            snapshot = await self._account_equity.get_account_snapshot()
            self._store.record_account_equity_snapshot(
                captured_at_ms=self._clock_ms(),
                snapshot=snapshot,
            )
        except Exception as exc:
            logger.warning(
                "account equity snapshot unavailable; review finished: %s",
                type(exc).__name__,
            )

    async def _run_batch(
        self,
        batch: ReviewBatch,
        latest_prices: Mapping[str, Decimal],
    ) -> ReviewRunResult:
        counts: Counter[str] = Counter()
        ordered = sorted(
            batch.candidates,
            key=lambda candidate: (candidate.priority, candidate.symbol),
        )

        for candidate in ordered:
            price = latest_prices.get(candidate.symbol)
            if price is None:
                logger.error("latest price missing for symbol=%s", candidate.symbol)
                counts["ERROR"] += 1
                continue
            try:
                result = await self._symbol_reviews.review_symbol(
                    symbol=candidate.symbol,
                    slot_start_ms=batch.slot_start_ms,
                    admitted=candidate.admitted,
                    price=price,
                )
                counts[result.disposition.value] += 1
                _log_execution_issue(candidate.symbol, result.execution)
                if (
                    result.plan is not None
                    and result.plan.action in FULL_CLOSE_ACTIONS
                    and candidate.admitted
                ):
                    state = self._store.load_symbol_state(
                        candidate.symbol,
                    )
                    if state.lifecycle is Lifecycle.REOPEN_PENDING:
                        reopen_result = await self._symbol_reviews.review_symbol(
                            symbol=candidate.symbol,
                            slot_start_ms=batch.slot_start_ms,
                            admitted=candidate.admitted,
                            price=price,
                        )
                        counts[reopen_result.disposition.value] += 1
                        _log_execution_issue(candidate.symbol, reopen_result.execution)
            except Exception:
                logger.exception("symbol review failed: symbol=%s", candidate.symbol)
                counts["ERROR"] += 1
        return ReviewRunResult(
            candidate_count=len(batch.candidates),
            result_counts=tuple(sorted(counts.items())),
        )
