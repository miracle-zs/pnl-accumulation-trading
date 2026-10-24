"""Coordinate one 15-minute review across the eligible symbols."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
import logging
import time
from typing import Callable

from ..domain.models import Action, Lifecycle
from .execution import ExecutionCoordinator, ExecutionResult
from .ports import ProcessLockPort, ReviewBatchSourcePort, StateStorePort
from .review_executor import SymbolReviewExecutor
from .review_run_models import ReviewBatch, ReviewCandidate, ReviewPriority


logger = logging.getLogger(__name__)
_FULL_CLOSE_ACTIONS = {
    Action.CLOSE_T1,
    Action.CLOSE_T2_FULL,
    Action.CLOSE_T3_FULL,
}


class ReviewRunError(RuntimeError):
    """The review cannot safely start or continue."""


@dataclass(frozen=True, slots=True)
class ReviewRunResult:
    candidate_count: int
    processed_count: int
    skipped_count: int
    failure_count: int
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
        process_lock: ProcessLockPort,
        clock_ms: Callable[[], int] | None = None,
        max_run_budget_ms: int = 120_000,
        max_batch_snapshot_age_ms: int = 30_000,
        future_clock_skew_ms: int = 1_000,
    ) -> None:
        if max_run_budget_ms <= 0 or max_batch_snapshot_age_ms <= 0:
            raise ValueError("run budget and snapshot age must be positive")
        if future_clock_skew_ms < 0:
            raise ValueError("future clock skew must be non-negative")
        self._store = store
        self._execution = execution
        self._symbol_reviews = symbol_reviews
        self._batch_source = batch_source
        self._process_lock = process_lock
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._max_run_budget_ms = max_run_budget_ms
        self._max_batch_snapshot_age_ms = max_batch_snapshot_age_ms
        self._future_clock_skew_ms = future_clock_skew_ms
        self._run_lock = asyncio.Lock()

    async def run_slot(
        self,
        *,
        account_id: str,
        strategy_version: str,
        slot_start_ms: int,
    ) -> ReviewRunResult:
        if not account_id or not strategy_version:
            raise ValueError("account_id and strategy_version must not be empty")
        if slot_start_ms < 0 or slot_start_ms % 900_000 != 0:
            raise ValueError("slot_start_ms must be a UTC 15-minute boundary")
        self._ensure_process_lock_held()

        async with self._run_lock:
            self._ensure_process_lock_held()
            await self._recover_pending(account_id)
            batch = await self._batch_source.load_review_batch(
                account_id,
                strategy_version,
                slot_start_ms,
            )
            if (
                batch.account_id != account_id
                or batch.strategy_version != strategy_version
                or batch.slot_start_ms != slot_start_ms
            ):
                raise ReviewRunError("batch source returned a different review slot")
            return await self._run_batch(batch)

    async def recover_pending_on_startup(
        self,
        account_id: str,
    ) -> tuple[ExecutionResult, ...]:
        if not account_id:
            raise ValueError("account_id must not be empty")
        self._ensure_process_lock_held()
        async with self._run_lock:
            self._ensure_process_lock_held()
            return await self._recover_pending(account_id)

    async def _recover_pending(self, account_id: str) -> tuple[ExecutionResult, ...]:
        try:
            return await self._execution.recover_pending(account_id)
        except Exception as exc:
            raise ReviewRunError(
                f"order recovery failed ({type(exc).__name__}); review was not started"
            ) from exc

    async def _run_batch(self, batch: ReviewBatch) -> ReviewRunResult:
        started_at_ms = self._clock_ms()
        self._validate_batch(batch, started_at_ms)
        counts: Counter[str] = Counter()
        processed_count = 0
        skipped_count = 0
        failure_count = 0
        ordered = self._ordered_candidates(batch)

        for index, candidate in enumerate(ordered):
            if self._clock_ms() - started_at_ms >= self._max_run_budget_ms:
                skipped_count = len(ordered) - index
                counts["SKIPPED_BUDGET"] += skipped_count
                break
            self._ensure_process_lock_held()
            try:
                result = await self._symbol_reviews.review_symbol(
                    account_id=batch.account_id,
                    symbol=candidate.symbol,
                    strategy_version=batch.strategy_version,
                    slot_start_ms=batch.slot_start_ms,
                    signals=candidate.signals,
                    reconcile_active_intents=False,
                )
                counts[result.disposition.value] += 1
                if (
                    result.plan is not None
                    and result.plan.action in _FULL_CLOSE_ACTIONS
                    and candidate.signals.admitted
                    and candidate.signals.reopen_authorized
                ):
                    state = self._store.load_symbol_state(
                        batch.account_id,
                        candidate.symbol,
                    )
                    if state is not None and state.lifecycle is Lifecycle.REOPEN_PENDING:
                        self._ensure_process_lock_held()
                        reopen_result = await self._symbol_reviews.review_symbol(
                            account_id=batch.account_id,
                            symbol=candidate.symbol,
                            strategy_version=batch.strategy_version,
                            slot_start_ms=batch.slot_start_ms,
                            signals=candidate.signals,
                            reconcile_active_intents=False,
                        )
                        counts[reopen_result.disposition.value] += 1
            except Exception as exc:
                logger.exception("symbol review failed: symbol=%s", candidate.symbol)
                counts["ERROR"] += 1
                failure_count += 1
            processed_count += 1
            self._store.save_review_rotation_cursor(
                batch.account_id,
                batch.strategy_version,
                candidate.priority,
                candidate.symbol,
                self._clock_ms(),
            )

        return ReviewRunResult(
            candidate_count=len(batch.candidates),
            processed_count=processed_count,
            skipped_count=skipped_count,
            failure_count=failure_count,
            result_counts=tuple(sorted(counts.items())),
        )

    def _ensure_process_lock_held(self) -> None:
        if not self._process_lock.is_held:
            raise ReviewRunError("the application process lock must be held")

    def _validate_batch(self, batch: ReviewBatch, now_ms: int) -> None:
        age_ms = now_ms - batch.sampled_at_ms
        if age_ms > self._max_batch_snapshot_age_ms:
            raise ReviewRunError(f"review snapshot is stale ({age_ms}ms old)")
        if age_ms < -self._future_clock_skew_ms:
            raise ReviewRunError("review snapshot timestamp is in the future")
        slot_close_time_ms = batch.slot_start_ms + 899_999
        if now_ms < slot_close_time_ms:
            raise ReviewRunError("review slot has not closed yet")
        if now_ms - slot_close_time_ms >= self._max_run_budget_ms:
            raise ReviewRunError("review slot is too old to start new actions")

    def _ordered_candidates(self, batch: ReviewBatch) -> tuple[ReviewCandidate, ...]:
        result: list[ReviewCandidate] = []
        for priority in ReviewPriority:
            group = sorted(
                (
                    candidate
                    for candidate in batch.candidates
                    if candidate.priority is priority
                ),
                key=lambda candidate: candidate.symbol,
            )
            cursor = self._store.load_review_rotation_cursor(
                batch.account_id,
                batch.strategy_version,
                priority,
            )
            if cursor is not None:
                index = next(
                    (i for i, candidate in enumerate(group) if candidate.symbol == cursor),
                    None,
                )
                if index is not None:
                    offset = (index + 1) % len(group)
                    group = group[offset:] + group[:offset]
            result.extend(group)
        return tuple(result)
