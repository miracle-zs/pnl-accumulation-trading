"""Coordinate one 15-minute review across the eligible symbols."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
import logging
from typing import TYPE_CHECKING, Callable, Mapping

from ..clock import current_epoch_ms
from ..domain.models import FULL_CLOSE_ACTIONS, Lifecycle
from .errors import (
    ExchangeAccessError,
    ExchangeOperationError,
    ExchangeResponseError,
)
from .execution import ExecutionCoordinator, ExecutionResult
from .models import IntentStatus, ReviewBatch
from .order_planning import OrderBelowMinimum, OrderRuleUnavailable
from .ports import (
    ExchangeGatewayPort,
    ReviewBatchSourcePort,
)
from .review_executor import (
    ReviewDisposition,
    SymbolReviewExecutor,
    SymbolReviewResult,
)

if TYPE_CHECKING:
    from ..adapters.sqlite.store import SQLiteStateStore


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


def _extract_review_issue(
    result: SymbolReviewResult,
    *,
    occurred_at_ms: int,
) -> tuple[str, str, int] | None:
    disposition = result.disposition
    if disposition is ReviewDisposition.LEVERAGE_UNAVAILABLE:
        return (disposition.value, "币安不支持 10x、5x 或 3x 杠杆，已跳过开仓", occurred_at_ms)
    if disposition is ReviewDisposition.INTENT_CONFLICT:
        return (disposition.value, "本地订单意图与策略状态冲突", occurred_at_ms)
    if disposition is ReviewDisposition.ORDER_PENDING:
        return (disposition.value, "上一笔订单仍在等待币安确认", occurred_at_ms)
    if disposition is ReviewDisposition.NO_ACTION:
        return None
    execution = result.execution
    if execution is None:
        raise RuntimeError(f"{disposition.value} result is missing its execution")
    if execution.status not in _EXECUTION_ISSUE_STATUSES:
        return None
    detail = execution.detail or f"订单状态：{execution.status.value}"
    return (f"ORDER_{execution.status.value}", detail, occurred_at_ms)


@dataclass(frozen=True, slots=True)
class ReviewRunResult:
    candidate_count: int
    result_counts: tuple[tuple[str, int], ...]


class ReviewRunCoordinator:
    """Recover orders, reconcile one snapshot, then review symbols in order."""

    def __init__(
        self,
        *,
        store: SQLiteStateStore,
        execution: ExecutionCoordinator,
        symbol_reviews: SymbolReviewExecutor,
        batch_source: ReviewBatchSourcePort,
        exchange: ExchangeGatewayPort,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._execution = execution
        self._symbol_reviews = symbol_reviews
        self._batch_source = batch_source
        self._exchange = exchange
        self._clock_ms = current_epoch_ms if clock_ms is None else clock_ms

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

        self._store.clear_dashboard_issues(("系统",))
        return result

    async def _run_slot(
        self,
        *,
        slot_start_ms: int,
    ) -> ReviewRunResult:
        await self._execution.recover_pending()
        batch, latest_prices = await asyncio.gather(
            self._batch_source.load_review_batch(slot_start_ms),
            self._exchange.get_latest_prices(),
        )
        price_captured_at_ms = self._clock_ms()
        result = await self._run_batch(batch, latest_prices)
        await self._execution.recover_full_close_fills()
        open_symbols = {
            state.symbol
            for state in self._store.list_symbol_states()
            if state.lifecycle in {Lifecycle.OPEN, Lifecycle.CLOSING}
        }
        self._store.record_market_prices(
            {
                symbol: price
                for symbol, price in latest_prices.items()
                if symbol in open_symbols
            },
            captured_at_ms=price_captured_at_ms,
        )
        try:
            snapshot = await self._exchange.get_account_snapshot()
        except (
            ExchangeAccessError,
            ExchangeOperationError,
            ExchangeResponseError,
        ) as exc:
            logger.warning(
                "account equity snapshot unavailable; review finished: %s",
                type(exc).__name__,
            )
        else:
            self._store.record_account_equity_snapshot(
                captured_at_ms=self._clock_ms(),
                snapshot=snapshot,
            )
        return result

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
        states_by_symbol = {
            state.symbol: state for state in self._store.list_symbol_states()
        }
        active_intents_by_symbol = {
            record.intent.symbol: record
            for record in self._store.list_active_order_intents()
        }
        issue_upserts: dict[str, tuple[str, str, int]] = {}
        resolved_symbols: set[str] = set()

        for candidate in ordered:
            price = latest_prices.get(candidate.symbol)
            if price is None:
                logger.error("latest price missing for symbol=%s", candidate.symbol)
                issue_upserts[candidate.symbol] = (
                    "PRICE_MISSING",
                    "本轮未获取到该标的最新价",
                    self._clock_ms(),
                )
                counts["ERROR"] += 1
                continue
            try:
                result = await self._symbol_reviews.review_symbol(
                    symbol=candidate.symbol,
                    slot_start_ms=batch.slot_start_ms,
                    admitted=candidate.admitted,
                    price=price,
                    state=states_by_symbol[candidate.symbol],
                    active_intent=active_intents_by_symbol.get(candidate.symbol),
                    active_intent_loaded=True,
                )
                counts[result.disposition.value] += 1
                _log_execution_issue(candidate.symbol, result.execution)
                issue = _extract_review_issue(result, occurred_at_ms=self._clock_ms())
                if issue is None:
                    resolved_symbols.add(candidate.symbol)
                else:
                    issue_upserts[candidate.symbol] = issue
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
                            state=state,
                            active_intent=None,
                            active_intent_loaded=True,
                        )
                        counts[reopen_result.disposition.value] += 1
                        _log_execution_issue(candidate.symbol, reopen_result.execution)
                        reopen_issue = _extract_review_issue(
                            reopen_result, occurred_at_ms=self._clock_ms()
                        )
                        if reopen_issue is None:
                            resolved_symbols.add(candidate.symbol)
                        else:
                            resolved_symbols.discard(candidate.symbol)
                            issue_upserts[candidate.symbol] = reopen_issue
            except OrderBelowMinimum as exc:
                resolved_symbols.discard(candidate.symbol)
                logger.warning("symbol order below exchange minimum: symbol=%s reason=%s", candidate.symbol, exc)
                issue_upserts[candidate.symbol] = (
                    "ORDER_BELOW_MINIMUM",
                    str(exc),
                    self._clock_ms(),
                )
                counts["ERROR"] += 1
            except (OrderRuleUnavailable, ExchangeOperationError) as exc:
                resolved_symbols.discard(candidate.symbol)
                logger.warning(
                    "symbol review operation failed: symbol=%s reason=%s",
                    candidate.symbol,
                    exc,
                )
                issue_upserts[candidate.symbol] = (
                    "REVIEW_ERROR",
                    "本轮处理异常，请查看服务日志",
                    self._clock_ms(),
                )
                counts["ERROR"] += 1
        self._store.sync_dashboard_issues(
            upserts=issue_upserts,
            clear_symbols=tuple(sorted(resolved_symbols)),
        )
        return ReviewRunResult(
            candidate_count=len(batch.candidates),
            result_counts=tuple(sorted(counts.items())),
        )
