"""Coordinate one 15-minute review across the eligible symbols."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import logging
import time
from typing import Callable

from ..domain.models import FULL_CLOSE_ACTIONS, Lifecycle
from .execution import ExecutionCoordinator, ExecutionResult
from .execution_models import IntentStatus
from .ports import AccountEquitySnapshotPort, ReviewBatchSourcePort, StateStorePort
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
        account_equity: AccountEquitySnapshotPort | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._execution = execution
        self._symbol_reviews = symbol_reviews
        self._batch_source = batch_source
        self._account_equity = account_equity
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    async def run_slot(
        self,
        *,
        slot_start_ms: int,
    ) -> ReviewRunResult:
        await self._execution.recover_pending()
        batch = await self._batch_source.load_review_batch(
            slot_start_ms,
        )
        result = await self._run_batch(batch)
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

    async def _run_batch(self, batch: ReviewBatch) -> ReviewRunResult:
        counts: Counter[str] = Counter()
        ordered = sorted(
            batch.candidates,
            key=lambda candidate: (candidate.priority, candidate.symbol),
        )

        for candidate in ordered:
            try:
                result = await self._symbol_reviews.review_symbol(
                    symbol=candidate.symbol,
                    slot_start_ms=batch.slot_start_ms,
                    admitted=candidate.admitted,
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
                            close_snapshot=result.close_snapshot,
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
