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
from .order_planning import OrderBelowMinimum
from .ports import (
    AccountEquitySnapshotPort,
    MarketPriceSnapshotPort,
    ReviewBatchSourcePort,
    StateStorePort,
)
from .review_executor import SymbolReviewExecutor, SymbolReviewResult
from .review_run_models import ReviewBatch


logger = logging.getLogger(__name__)
_EXECUTION_ISSUE_STATUSES = {
    IntentStatus.PENDING,
    IntentStatus.UNKNOWN,
    IntentStatus.PARTIALLY_FILLED,
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


def _save_execution_issue(
    store: StateStorePort,
    *,
    symbol: str,
    result: ExecutionResult | None,
    occurred_at_ms: int,
) -> bool:
    if result is None or result.status not in _EXECUTION_ISSUE_STATUSES:
        return True
    detail = result.detail or f"订单状态：{result.status.value}"
    store.record_dashboard_issue(
        symbol=symbol,
        category=f"ORDER_{result.status.value}",
        detail=detail,
        occurred_at_ms=occurred_at_ms,
    )
    return False


def _save_review_result_issue(
    store: StateStorePort,
    *,
    symbol: str,
    result: SymbolReviewResult,
    occurred_at_ms: int,
) -> bool:
    disposition = result.disposition.value
    if disposition == "LEVERAGE_UNAVAILABLE":
        store.record_dashboard_issue(
            symbol=symbol,
            category=disposition,
            detail="币安不支持 10x、5x 或 3x 杠杆，已跳过开仓",
            occurred_at_ms=occurred_at_ms,
        )
        return False
    elif disposition == "INTENT_CONFLICT":
        store.record_dashboard_issue(
            symbol=symbol,
            category=disposition,
            detail="本地订单意图与策略状态冲突",
            occurred_at_ms=occurred_at_ms,
        )
        return False
    elif disposition == "ORDER_PENDING":
        store.record_dashboard_issue(
            symbol=symbol,
            category=disposition,
            detail="上一笔订单仍在等待币安确认",
            occurred_at_ms=occurred_at_ms,
        )
        return False
    else:
        return _save_execution_issue(
            store,
            symbol=symbol,
            result=result.execution,
            occurred_at_ms=occurred_at_ms,
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
        try:
            result = await self._run_slot(slot_start_ms=slot_start_ms)
        except Exception as exc:
            try:
                self._store.record_dashboard_issue(
                    symbol="系统",
                    category="REVIEW_RUN_ERROR",
                    detail=f"本轮巡检失败：{type(exc).__name__}",
                    occurred_at_ms=self._clock_ms(),
                )
            except Exception:
                logger.exception("failed to save whole-run dashboard issue")
            raise

        try:
            self._store.clear_dashboard_issues(("系统",))
        except Exception:
            logger.exception("failed to clear whole-run dashboard issue")
        return result

    async def _run_slot(
        self,
        *,
        slot_start_ms: int,
    ) -> ReviewRunResult:
        await self._execution.recover_pending()
        batch, latest_prices = await asyncio.gather(
            self._batch_source.load_review_batch(slot_start_ms),
            self._market_prices.get_latest_prices(),
        )
        price_captured_at_ms = self._clock_ms()
        result = await self._run_batch(batch, latest_prices)
        await self._execution.recover_full_close_fills()
        open_symbols = {
            state.symbol
            for state in self._store.list_symbol_states()
            if state.lifecycle in {Lifecycle.OPEN, Lifecycle.CLOSING}
        }
        try:
            self._store.record_market_prices(
                {symbol: price for symbol, price in latest_prices.items() if symbol in open_symbols},
                captured_at_ms=price_captured_at_ms,
            )
        except Exception as exc:
            logger.warning(
                "dashboard price sample unavailable; review finished: %s",
                type(exc).__name__,
            )
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
        resolved_symbols: set[str] = set()

        for candidate in ordered:
            price = latest_prices.get(candidate.symbol)
            if price is None:
                logger.error("latest price missing for symbol=%s", candidate.symbol)
                self._store.record_dashboard_issue(
                    symbol=candidate.symbol,
                    category="PRICE_MISSING",
                    detail="本轮未获取到该标的最新价",
                    occurred_at_ms=self._clock_ms(),
                )
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
                if _save_review_result_issue(
                    self._store,
                    symbol=candidate.symbol,
                    result=result,
                    occurred_at_ms=self._clock_ms(),
                ):
                    resolved_symbols.add(candidate.symbol)
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
                        if _save_review_result_issue(
                            self._store,
                            symbol=candidate.symbol,
                            result=reopen_result,
                            occurred_at_ms=self._clock_ms(),
                        ):
                            resolved_symbols.add(candidate.symbol)
                        else:
                            resolved_symbols.discard(candidate.symbol)
            except OrderBelowMinimum as exc:
                resolved_symbols.discard(candidate.symbol)
                logger.warning("symbol order below exchange minimum: symbol=%s reason=%s", candidate.symbol, exc)
                self._store.record_dashboard_issue(
                    symbol=candidate.symbol,
                    category="ORDER_BELOW_MINIMUM",
                    detail=str(exc),
                    occurred_at_ms=self._clock_ms(),
                )
                counts["ERROR"] += 1
            except Exception:
                resolved_symbols.discard(candidate.symbol)
                logger.exception("symbol review failed: symbol=%s", candidate.symbol)
                self._store.record_dashboard_issue(
                    symbol=candidate.symbol,
                    category="REVIEW_ERROR",
                    detail="本轮处理异常，请查看服务日志",
                    occurred_at_ms=self._clock_ms(),
                )
                counts["ERROR"] += 1
        self._store.clear_dashboard_issues(tuple(sorted(resolved_symbols)))
        return ReviewRunResult(
            candidate_count=len(batch.candidates),
            result_counts=tuple(sorted(counts.items())),
        )
