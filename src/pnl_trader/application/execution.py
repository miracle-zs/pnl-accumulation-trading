"""Durable order submission and recovery workflow."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, cast

from ..domain.models import BUY_ACTIONS, ExecutionReceipt, TradeFillRecord, ZERO
from .execution_models import (
    ExchangeOrderSnapshot,
    ExchangeOrderStatus,
    IntentStatus,
    MarketOrderRequest,
    OrderIntent,
    PersistedOrderIntent,
    SubmissionCertainty,
)
from .ports import ExchangeGatewayPort, StateStorePort


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    intent_id: str
    status: IntentStatus
    detail: str | None = None


class ExecutionCoordinator:
    """Submit at most one order attempt and reconcile it before advancing state.

    ``execute_prepared`` accepts a persisted intent plus its already-sized market
    order. The attempt is committed before the gateway is called. A duplicate
    invocation never submits again. ``recover_pending`` queries existing
    attempts by client order ID and never creates orders.
    """

    def __init__(
        self,
        *,
        store: StateStorePort,
        exchange: ExchangeGatewayPort,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._exchange = exchange
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    async def execute_prepared(
        self,
        intent: OrderIntent,
        requested_qty: Decimal,
    ) -> ExecutionResult:
        """Submit an order only for an active PREPARED intent.

        The caller must first persist ``intent`` with the StateStore. If the
        intent is no longer PREPARED, this method returns its current state;
        recovery is a separate workflow.
        """

        request = MarketOrderRequest(
            symbol=intent.symbol,
            side="BUY" if intent.action in BUY_ACTIONS else "SELL",
            quantity=requested_qty,
            client_order_id=intent.intent_id,
        )
        active_intents = self._store.list_active_order_intents(intent.symbol)
        active = (
            active_intents[0]
            if active_intents
            and active_intents[0].intent.intent_id == intent.intent_id
            else None
        )
        if active is None:
            status = self._store.get_intent_status(intent.intent_id)
            if status is None:
                raise RuntimeError("order intent has not been persisted")
            return ExecutionResult(
                intent_id=intent.intent_id,
                status=status,
            )
        if active.intent != intent:
            raise RuntimeError("caller intent differs from the persisted intent")
        if active.status is not IntentStatus.PREPARED:
            return ExecutionResult(
                intent_id=active.intent.intent_id,
                status=active.status,
                detail="attempt already exists; reconcile it instead of submitting again",
            )

        self._store.create_order_attempt(intent.intent_id, requested_qty=requested_qty)

        submission = await self._exchange.submit_market_order(request)

        if submission.certainty is SubmissionCertainty.UNKNOWN:
            self._move_to_unknown(intent.intent_id, IntentStatus.PENDING)
            return ExecutionResult(
                intent_id=intent.intent_id,
                status=IntentStatus.UNKNOWN,
                detail=submission.detail,
            )
        if submission.certainty is SubmissionCertainty.REJECTED:
            return self._apply_no_fill(
                intent,
                expected=IntentStatus.PENDING,
                terminal_status=IntentStatus.REJECTED,
                detail=submission.detail,
            )
        order = cast(ExchangeOrderSnapshot, submission.order)
        return await self._consume_order_snapshot(
            intent,
            requested_qty,
            order,
            expected_status=IntentStatus.PENDING,
        )

    async def recover_pending(self) -> None:
        """Reconcile active attempts by client order ID without resubmitting."""

        for record in self._store.list_active_order_intents():
            await self._recover_record(record)

    async def _recover_record(self, record: PersistedOrderIntent) -> None:
        if record.status is IntentStatus.PREPARED:
            return
        requested_qty = record.requested_qty
        if requested_qty is None:
            raise RuntimeError("in-flight intent has no persisted order attempt")

        lookup_error: str | None = None
        try:
            order = await self._exchange.lookup_order(
                record.intent.symbol,
                record.intent.intent_id,
            )
        except Exception as exc:
            order = None
            lookup_error = type(exc).__name__

        if order is None:
            logger.warning(
                "order recovery unresolved: symbol=%s intent_id=%s lookup=%s",
                record.intent.symbol,
                record.intent.intent_id,
                lookup_error or "NOT_FOUND",
            )
            self._move_to_unknown(record.intent.intent_id, record.status)
            return
        await self._consume_order_snapshot(
            record.intent,
            requested_qty,
            order,
            expected_status=record.status,
        )

    async def _consume_order_snapshot(
        self,
        intent: OrderIntent,
        requested_qty: Decimal,
        order: ExchangeOrderSnapshot,
        *,
        expected_status: IntentStatus,
    ) -> ExecutionResult:
        if order.executed_qty > requested_qty:
            raise RuntimeError("exchange execution exceeds the requested quantity")
        if order.status is ExchangeOrderStatus.NEW:
            if expected_status is IntentStatus.PARTIALLY_FILLED:
                self._move_to_unknown(intent.intent_id, expected_status)
                return ExecutionResult(
                    intent_id=intent.intent_id,
                    status=IntentStatus.UNKNOWN,
                    detail="exchange order snapshot regressed after a partial fill",
                )
            if expected_status is IntentStatus.PENDING:
                return ExecutionResult(
                    intent_id=intent.intent_id,
                    status=IntentStatus.PENDING,
                )
            return self._record_active_status(
                intent.intent_id,
                expected_status,
                IntentStatus.PENDING,
            )
        if order.status is ExchangeOrderStatus.PARTIALLY_FILLED:
            return self._record_active_status(
                intent.intent_id,
                expected_status,
                IntentStatus.PARTIALLY_FILLED,
            )
        if order.executed_qty == ZERO:
            terminal_status = _TERMINAL_STATUS_BY_EXCHANGE[order.status]
            return self._apply_no_fill(
                intent,
                expected=expected_status,
                terminal_status=terminal_status,
            )

        try:
            fills = await self._exchange.list_order_fills(
                intent.symbol,
                order,
                "BUY" if intent.action in BUY_ACTIONS else "SELL",
            )
        except Exception as exc:
            self._move_to_unknown(intent.intent_id, expected_status)
            return ExecutionResult(
                intent_id=intent.intent_id,
                status=IntentStatus.UNKNOWN,
                detail=f"terminal order fills are not yet available ({type(exc).__name__})",
            )

        receipt = ExecutionReceipt(
            action=intent.action,
            filled_qty=order.executed_qty,
            quote_qty=sum((fill.price * fill.qty for fill in fills), ZERO),
            target_cumulative_sold_qty=intent.target_cumulative_sold_qty,
        )
        terminal_status = _TERMINAL_STATUS_BY_EXCHANGE[order.status]
        self._store.commit_confirmed_execution(
            intent_id=intent.intent_id,
            receipt=receipt,
            fills=fills,
            expected_intent_status=expected_status,
            terminal_status=terminal_status,
            updated_at_ms=self._clock_ms(),
        )
        return ExecutionResult(
            intent_id=intent.intent_id,
            status=terminal_status,
        )

    def _apply_no_fill(
        self,
        intent: OrderIntent,
        *,
        expected: IntentStatus,
        terminal_status: IntentStatus,
        detail: str | None = None,
    ) -> ExecutionResult:
        self._store.resolve_no_fill(
            intent.intent_id,
            expected=expected,
            terminal_status=terminal_status,
        )
        return ExecutionResult(
            intent_id=intent.intent_id,
            status=terminal_status,
            detail=detail,
        )

    def _record_active_status(
        self,
        intent_id: str,
        expected: IntentStatus,
        target: IntentStatus,
    ) -> ExecutionResult:
        self._store.transition_intent(
            intent_id,
            expected=expected,
            target=target,
        )
        return ExecutionResult(
            intent_id=intent_id,
            status=target,
        )

    def _move_to_unknown(self, intent_id: str, expected: IntentStatus) -> None:
        if expected is IntentStatus.UNKNOWN:
            return
        self._store.transition_intent(
            intent_id,
            expected=expected,
            target=IntentStatus.UNKNOWN,
        )

_TERMINAL_STATUS_BY_EXCHANGE = {
    ExchangeOrderStatus.FILLED: IntentStatus.FILLED,
    ExchangeOrderStatus.REJECTED: IntentStatus.REJECTED,
    ExchangeOrderStatus.CANCELED: IntentStatus.CANCELED,
    ExchangeOrderStatus.EXPIRED: IntentStatus.EXPIRED,
    ExchangeOrderStatus.EXPIRED_IN_MATCH: IntentStatus.EXPIRED,
}
