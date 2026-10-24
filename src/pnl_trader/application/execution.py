"""Durable order submission and recovery workflow."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Callable
import time

from ..domain.models import Action, ExecutionReceipt, TradeFillRecord, ZERO
from .execution_models import (
    ExchangeOrderSnapshot,
    ExchangeOrderStatus,
    IntentStatus,
    MarketOrderRequest,
    OrderAttempt,
    OrderIntent,
    PersistedOrderIntent,
    SubmissionCertainty,
)
from .ports import ExchangeGatewayPort, StateStorePort


class ExecutionResolution(str, Enum):
    PREPARED = "PREPARED"
    AWAITING_EXCHANGE = "AWAITING_EXCHANGE"
    UNKNOWN = "UNKNOWN"
    FILLS_APPLIED = "FILLS_APPLIED"
    NO_FILL_APPLIED = "NO_FILL_APPLIED"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    intent_id: str
    status: IntentStatus
    resolution: ExecutionResolution
    detail: str | None = None


class ExecutionInvariantError(RuntimeError):
    """Raised when exchange evidence conflicts with the persisted intent."""


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
        notification_channel: str | None = None,
    ) -> None:
        self._store = store
        self._exchange = exchange
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._notification_channel = notification_channel
        self._order_lock = asyncio.Lock()

    async def execute_prepared(
        self,
        intent: OrderIntent,
        attempt: OrderAttempt,
        request: MarketOrderRequest,
    ) -> ExecutionResult:
        """Run one prepared order through the serialized execution path."""

        async with self._order_lock:
            return await self._execute_prepared_locked(intent, attempt, request)

    async def _execute_prepared_locked(
        self,
        intent: OrderIntent,
        attempt: OrderAttempt,
        request: MarketOrderRequest,
    ) -> ExecutionResult:
        """Submit an order only for an active PREPARED intent.

        The caller must first persist ``intent`` with the StateStore. If the
        attempt already exists, this method returns its current state and does
        not call ``submit_market_order`` again; recovery is a separate workflow.
        """

        self._validate_request(intent, attempt, request)
        active = self._find_active_intent(intent.intent_id, intent.account_id)
        if active is None:
            status = self._store.get_intent_status(intent.intent_id)
            if status is None:
                raise ExecutionInvariantError("order intent has not been persisted")
            if status in _ACTIVE_STATUSES:
                raise ExecutionInvariantError("active intent is missing from the recovery query")
            return ExecutionResult(
                intent_id=intent.intent_id,
                status=status,
                resolution=ExecutionResolution.ALREADY_TERMINAL,
            )
        if active.intent != intent:
            raise ExecutionInvariantError("caller intent differs from the persisted intent")
        if active.status is not IntentStatus.PREPARED:
            return self._pending_result(active)
        if active.attempt is not None:
            raise ExecutionInvariantError("PREPARED intent already has a submission attempt")

        created = self._store.create_order_attempt(attempt)
        if not created:
            refreshed = self._find_active_intent(intent.intent_id, intent.account_id)
            if refreshed is None:
                status = self._store.get_intent_status(intent.intent_id)
                if status is None:
                    raise ExecutionInvariantError("intent disappeared after attempt creation")
                return ExecutionResult(
                    intent_id=intent.intent_id,
                    status=status,
                    resolution=ExecutionResolution.ALREADY_TERMINAL,
                )
            return self._pending_result(refreshed)

        try:
            submission = await self._exchange.submit_market_order(request)
        except Exception as exc:
            self._move_to_unknown(intent.intent_id, IntentStatus.SUBMITTING)
            return ExecutionResult(
                intent_id=intent.intent_id,
                status=IntentStatus.UNKNOWN,
                resolution=ExecutionResolution.UNKNOWN,
                detail=f"submission outcome unknown ({type(exc).__name__}); lookup required",
            )

        if submission.certainty is SubmissionCertainty.UNKNOWN:
            self._move_to_unknown(intent.intent_id, IntentStatus.SUBMITTING)
            return ExecutionResult(
                intent_id=intent.intent_id,
                status=IntentStatus.UNKNOWN,
                resolution=ExecutionResolution.UNKNOWN,
                detail=submission.detail or "submission outcome unknown; lookup required",
            )
        if submission.certainty is SubmissionCertainty.REJECTED:
            return self._apply_no_fill(
                intent,
                attempt,
                expected=IntentStatus.SUBMITTING,
                terminal_status=IntentStatus.REJECTED,
                exchange_order_id=None,
                detail=submission.detail,
            )
        if submission.order is None:
            raise ExecutionInvariantError("accepted submission is missing its order snapshot")
        return await self._consume_order_snapshot(
            intent,
            attempt,
            submission.order,
            expected_status=IntentStatus.SUBMITTING,
        )

    async def recover_pending(
        self,
        account_id: str | None = None,
        symbol: str | None = None,
    ) -> tuple[ExecutionResult, ...]:
        """Reconcile active attempts by client order ID without resubmitting."""

        results: list[ExecutionResult] = []
        for record in self._store.list_active_order_intents(account_id, symbol):
            results.append(await self._recover_record(record))
        return tuple(results)

    async def _recover_record(self, record: PersistedOrderIntent) -> ExecutionResult:
        async with self._order_lock:
            current = self._find_active_intent(
                record.intent.intent_id,
                record.intent.account_id,
            )
            if current is None:
                status = self._store.get_intent_status(record.intent.intent_id)
                if status is None:
                    raise ExecutionInvariantError("intent disappeared during recovery")
                return ExecutionResult(
                    intent_id=record.intent.intent_id,
                    status=status,
                    resolution=ExecutionResolution.ALREADY_TERMINAL,
                )
            return await self._recover_record_locked(current)

    async def _recover_record_locked(self, record: PersistedOrderIntent) -> ExecutionResult:
        if record.status is IntentStatus.PREPARED and record.attempt is None:
            return self._pending_result(record)
        attempt = record.attempt
        if attempt is None:
            raise ExecutionInvariantError("in-flight intent has no persisted order attempt")
        if attempt.status is not record.status:
            raise ExecutionInvariantError("intent and order attempt statuses are inconsistent")
        if attempt.sent_at_ms is None:
            raise ExecutionInvariantError("persisted order attempt is missing its submission timestamp")

        try:
            order = await self._exchange.lookup_order(
                record.intent.symbol,
                attempt.client_order_id,
            )
        except Exception as exc:
            self._move_to_unknown(record.intent.intent_id, record.status)
            return ExecutionResult(
                intent_id=record.intent.intent_id,
                status=IntentStatus.UNKNOWN,
                resolution=ExecutionResolution.UNKNOWN,
                detail=f"order lookup failed ({type(exc).__name__}); keep the intent reserved",
            )

        if order is None:
            self._move_to_unknown(record.intent.intent_id, record.status)
            return ExecutionResult(
                intent_id=record.intent.intent_id,
                status=IntentStatus.UNKNOWN,
                resolution=ExecutionResolution.UNKNOWN,
                detail="exchange did not return the order; keep the intent reserved and continue lookup",
            )
        return await self._consume_order_snapshot(
            record.intent,
            OrderAttempt(
                attempt_id=attempt.attempt_id,
                intent_id=record.intent.intent_id,
                attempt_no=attempt.attempt_no,
                client_order_id=attempt.client_order_id,
                requested_qty=attempt.requested_qty,
                sent_at_ms=attempt.sent_at_ms,
            ),
            order,
            expected_status=record.status,
        )

    async def _consume_order_snapshot(
        self,
        intent: OrderIntent,
        attempt: OrderAttempt,
        order: ExchangeOrderSnapshot,
        *,
        expected_status: IntentStatus,
    ) -> ExecutionResult:
        if order.executed_qty > attempt.requested_qty:
            raise ExecutionInvariantError("exchange execution exceeds the requested quantity")
        if order.status is ExchangeOrderStatus.NEW:
            if expected_status is IntentStatus.PARTIALLY_FILLED:
                self._move_to_unknown(intent.intent_id, expected_status)
                return ExecutionResult(
                    intent_id=intent.intent_id,
                    status=IntentStatus.UNKNOWN,
                    resolution=ExecutionResolution.UNKNOWN,
                    detail="exchange order snapshot regressed after a partial fill",
                )
            return self._record_active_status(
                intent.intent_id,
                expected_status,
                IntentStatus.ACKNOWLEDGED,
                order.exchange_order_id,
            )
        if order.status is ExchangeOrderStatus.PARTIALLY_FILLED:
            return self._record_active_status(
                intent.intent_id,
                expected_status,
                IntentStatus.PARTIALLY_FILLED,
                order.exchange_order_id,
            )
        if order.status is ExchangeOrderStatus.REJECTED or order.executed_qty == ZERO:
            terminal_status = _TERMINAL_STATUS_BY_EXCHANGE[order.status]
            return self._apply_no_fill(
                intent,
                attempt,
                expected=expected_status,
                terminal_status=terminal_status,
                exchange_order_id=order.exchange_order_id,
            )

        try:
            fills = await self._exchange.list_order_fills(
                intent.symbol,
                attempt.client_order_id,
            )
        except Exception as exc:
            self._move_to_unknown(intent.intent_id, expected_status)
            return ExecutionResult(
                intent_id=intent.intent_id,
                status=IntentStatus.UNKNOWN,
                resolution=ExecutionResolution.UNKNOWN,
                detail=f"terminal order fills are not yet available ({type(exc).__name__})",
            )

        receipt = ExecutionReceipt(
            action=intent.action,
            filled_qty=order.executed_qty,
            quote_qty=order.cumulative_quote,
            target_cumulative_sold_qty=intent.target_cumulative_sold_qty,
        )
        terminal_status = _TERMINAL_STATUS_BY_EXCHANGE[order.status]
        applied = self._store.commit_confirmed_execution(
            receipt_id=f"execution:{attempt.attempt_id}",
            intent_id=intent.intent_id,
            receipt=receipt,
            fills=fills,
            expected_intent_status=expected_status,
            terminal_status=terminal_status,
            applied_at_ms=self._clock_ms(),
            exchange_order_id=order.exchange_order_id,
            notification_channel=self._notification_channel,
        )
        return ExecutionResult(
            intent_id=intent.intent_id,
            status=terminal_status,
            resolution=ExecutionResolution.FILLS_APPLIED,
            detail=None if applied else "execution receipt was already applied",
        )

    def _apply_no_fill(
        self,
        intent: OrderIntent,
        attempt: OrderAttempt,
        *,
        expected: IntentStatus,
        terminal_status: IntentStatus,
        exchange_order_id: str | None,
        detail: str | None = None,
    ) -> ExecutionResult:
        self._store.resolve_no_fill(
            intent.intent_id,
            receipt_id=f"execution:{attempt.attempt_id}",
            expected=expected,
            terminal_status=terminal_status,
            resolved_at_ms=self._clock_ms(),
            confirmed_filled_qty=ZERO,
            confirmed_quote_qty=ZERO,
            exchange_order_id=exchange_order_id,
        )
        return ExecutionResult(
            intent_id=intent.intent_id,
            status=terminal_status,
            resolution=ExecutionResolution.NO_FILL_APPLIED,
            detail=detail,
        )

    def _record_active_status(
        self,
        intent_id: str,
        expected: IntentStatus,
        target: IntentStatus,
        exchange_order_id: str | None,
    ) -> ExecutionResult:
        self._store.transition_intent(
            intent_id,
            expected=expected,
            target=target,
            exchange_order_id=exchange_order_id,
        )
        return ExecutionResult(
            intent_id=intent_id,
            status=target,
            resolution=ExecutionResolution.AWAITING_EXCHANGE,
        )

    def _move_to_unknown(self, intent_id: str, expected: IntentStatus) -> None:
        if expected is IntentStatus.UNKNOWN:
            return
        self._store.transition_intent(
            intent_id,
            expected=expected,
            target=IntentStatus.UNKNOWN,
        )

    def _find_active_intent(
        self,
        intent_id: str,
        account_id: str,
    ) -> PersistedOrderIntent | None:
        return next(
            (
                record
                for record in self._store.list_active_order_intents(account_id)
                if record.intent.intent_id == intent_id
            ),
            None,
        )

    @staticmethod
    def _pending_result(record: PersistedOrderIntent) -> ExecutionResult:
        resolution = (
            ExecutionResolution.PREPARED
            if record.status is IntentStatus.PREPARED
            else ExecutionResolution.UNKNOWN
            if record.status is IntentStatus.UNKNOWN
            else ExecutionResolution.AWAITING_EXCHANGE
        )
        detail = (
            "revalidate the plan and risk before creating a submit attempt"
            if record.status is IntentStatus.PREPARED
            else "attempt already exists; reconcile it instead of submitting again"
        )
        return ExecutionResult(
            intent_id=record.intent.intent_id,
            status=record.status,
            resolution=resolution,
            detail=detail,
        )

    @staticmethod
    def _validate_request(
        intent: OrderIntent,
        attempt: OrderAttempt,
        request: MarketOrderRequest,
    ) -> None:
        if attempt.intent_id != intent.intent_id:
            raise ExecutionInvariantError("attempt belongs to a different intent")
        if (
            request.symbol != intent.symbol
            or request.client_order_id != attempt.client_order_id
            or request.quantity != attempt.requested_qty
        ):
            raise ExecutionInvariantError("market order request does not match its persisted attempt")
        buy_actions = {Action.OPEN_T1, Action.ADD_T2, Action.ADD_T3}
        expected_side = "BUY" if intent.action in buy_actions else "SELL"
        if request.side != expected_side or request.reduce_only != (expected_side == "SELL"):
            raise ExecutionInvariantError("market order side does not match the strategy action")


_ACTIVE_STATUSES = {
    IntentStatus.PREPARED,
    IntentStatus.SUBMITTING,
    IntentStatus.UNKNOWN,
    IntentStatus.ACKNOWLEDGED,
    IntentStatus.PARTIALLY_FILLED,
}

_TERMINAL_STATUS_BY_EXCHANGE = {
    ExchangeOrderStatus.FILLED: IntentStatus.FILLED,
    ExchangeOrderStatus.REJECTED: IntentStatus.REJECTED,
    ExchangeOrderStatus.CANCELED: IntentStatus.CANCELED,
    ExchangeOrderStatus.EXPIRED: IntentStatus.EXPIRED,
    ExchangeOrderStatus.EXPIRED_IN_MATCH: IntentStatus.EXPIRED,
}
