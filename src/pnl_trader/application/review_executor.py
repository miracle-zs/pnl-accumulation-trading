"""Compose one strategy review with durable order execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import time
from typing import Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from ..domain.models import Action, Decision, Lifecycle, Observation, SymbolState
from ..domain.policy import StrategyPolicy
from .execution import ExecutionCoordinator, ExecutionResolution, ExecutionResult
from .execution_models import (
    IntentStatus,
    MarketOrderRequest,
    OrderAttempt,
    OrderIntent,
)
from .market_models import FifteenMinuteCloseSnapshot
from .order_planning import MarketOrderPlan, MarketOrderPlanner, OrderPlanningError
from .ports import ExchangeGatewayPort, StateStorePort
from .store_errors import (
    ActiveIntentConflict,
    IntentConflict,
    IntentTransitionError,
    StateVersionConflict,
)


class ReviewDisposition(str, Enum):
    NO_ACTION = "NO_ACTION"
    STALE_PRICE = "STALE_PRICE"
    STATE_CHANGED = "STATE_CHANGED"
    INTENT_CONFLICT = "INTENT_CONFLICT"
    ORDER_PENDING = "ORDER_PENDING"
    ORDER_RESOLVED = "ORDER_RESOLVED"
    EXECUTION = "EXECUTION"


@dataclass(frozen=True, slots=True)
class SymbolReviewResult:
    symbol: str
    disposition: ReviewDisposition
    decision: Decision | None = None
    plan: MarketOrderPlan | None = None
    price: FifteenMinuteCloseSnapshot | None = None
    execution: ExecutionResult | None = None
    detail: str | None = None


class ReviewExecutorError(RuntimeError):
    """Raised when the review workflow's persisted state violates its contract."""


class SymbolReviewExecutor:
    """Run one symbol through policy, sizing and durable order execution."""

    def __init__(
        self,
        *,
        store: StateStorePort,
        exchange: ExchangeGatewayPort,
        execution: ExecutionCoordinator,
        policy: StrategyPolicy | None = None,
        planner: MarketOrderPlanner | None = None,
        clock_ms: Callable[[], int] | None = None,
        future_clock_skew_ms: int = 1_000,
    ) -> None:
        if future_clock_skew_ms < 0:
            raise ValueError("clock skew must be non-negative")
        self._store = store
        self._exchange = exchange
        self._execution = execution
        self._policy = policy or StrategyPolicy()
        self._planner = planner or MarketOrderPlanner()
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._future_clock_skew_ms = future_clock_skew_ms

    async def review_symbol(
        self,
        *,
        account_id: str,
        symbol: str,
        strategy_version: str,
        slot_start_ms: int,
        signals: Observation,
    ) -> SymbolReviewResult:
        """Review one action for a symbol in a 15-minute slot."""

        if not account_id or not symbol or not strategy_version:
            raise ValueError("account_id, symbol and strategy_version must not be empty")
        if slot_start_ms < 0 or slot_start_ms % 900_000 != 0:
            raise ValueError("slot_start_ms must be a UTC 15-minute boundary")

        state = self._store.load_symbol_state(account_id, symbol)
        if state is None:
            raise ReviewExecutorError(
                f"symbol state for {account_id}/{symbol} must be initialized before review"
            )
        business_key = f"{strategy_version}:{slot_start_ms}:{symbol}"
        if state.lifecycle is Lifecycle.REOPEN_PENDING:
            business_key += ":reopen"
        intent_id = str(
            uuid5(NAMESPACE_URL, f"pnl-periodic-review:{account_id}:{business_key}")
        )
        existing = await self._resolve_prior_symbol_intent(
            account_id=account_id,
            symbol=symbol,
            current_intent_id=intent_id,
        )
        if existing is not None:
            return existing

        candle = await self._exchange.get_fifteen_minute_close(symbol, slot_start_ms)
        now_ms = self._clock_ms()
        candle_age_ms = now_ms - candle.close_time_ms
        if (
            candle.symbol != symbol
            or candle.slot_start_ms != slot_start_ms
            or candle_age_ms < -self._future_clock_skew_ms
        ):
            return SymbolReviewResult(
                symbol=symbol,
                disposition=ReviewDisposition.STALE_PRICE,
                price=candle,
                detail=(
                    "15-minute close candle does not match the requested slot or is not yet closed"
                ),
            )

        observation = Observation(
            admitted=signals.admitted,
            reopen_authorized=signals.reopen_authorized,
            valid=signals.valid,
            scale_in_authorized=signals.scale_in_authorized,
            close_price=candle.close_price,
        )
        decision = self._policy.decide(state, observation)
        if decision.action is Action.HOLD:
            self._cancel_prepared_intent(account_id, intent_id)
            return SymbolReviewResult(
                symbol=symbol,
                disposition=ReviewDisposition.NO_ACTION,
                decision=decision,
                price=candle,
                detail=decision.reason,
            )

        rules = await self._exchange.get_instrument_rules(symbol)
        try:
            plan = self._planner.plan(decision, state, rules, candle)
        except OrderPlanningError as exc:
            self._cancel_prepared_intent(account_id, intent_id)
            return SymbolReviewResult(
                symbol=symbol,
                disposition=ReviewDisposition.NO_ACTION,
                decision=decision,
                price=candle,
                detail=str(exc),
            )

        intent = self._make_intent(
            account_id=account_id,
            state=state,
            plan=plan,
            decision=decision,
            business_key=business_key,
            intent_id=intent_id,
            slot_start_ms=slot_start_ms,
        )
        try:
            self._store.create_order_intent(intent)
        except StateVersionConflict as exc:
            self._cancel_prepared_intent(account_id, intent_id)
            return SymbolReviewResult(
                symbol=symbol,
                disposition=ReviewDisposition.STATE_CHANGED,
                decision=decision,
                plan=plan,
                price=candle,
                detail=str(exc),
            )
        except IntentConflict as exc:
            self._cancel_prepared_intent(account_id, intent_id)
            return SymbolReviewResult(
                symbol=symbol,
                disposition=ReviewDisposition.INTENT_CONFLICT,
                decision=decision,
                plan=plan,
                price=candle,
                detail=str(exc),
            )
        except ActiveIntentConflict as exc:
            return SymbolReviewResult(
                symbol=symbol,
                disposition=ReviewDisposition.ORDER_PENDING,
                decision=decision,
                plan=plan,
                price=candle,
                detail=str(exc),
            )

        client_order_id = _client_order_id(account_id, business_key)
        attempt = OrderAttempt(
            attempt_id=str(uuid5(UUID(intent_id), "attempt:1")),
            intent_id=intent_id,
            attempt_no=1,
            client_order_id=client_order_id,
            requested_qty=plan.quantity,
            sent_at_ms=self._clock_ms(),
        )
        request = MarketOrderRequest(
            symbol=symbol,
            side=plan.side,
            quantity=plan.quantity,
            client_order_id=client_order_id,
            reduce_only=plan.reduce_only,
        )
        execution_result = await self._execution.execute_prepared(intent, attempt, request)
        return SymbolReviewResult(
            symbol=symbol,
            disposition=ReviewDisposition.EXECUTION,
            decision=decision,
            plan=plan,
            price=candle,
            execution=execution_result,
            detail=execution_result.detail,
        )

    async def _resolve_prior_symbol_intent(
        self,
        *,
        account_id: str,
        symbol: str,
        current_intent_id: str,
    ) -> SymbolReviewResult | None:
        active = self._store.list_active_order_intents(account_id, symbol)
        if active:
            record = active[0]
            same_slot = record.intent.intent_id == current_intent_id
            if record.status is IntentStatus.PREPARED and record.attempt is None:
                if same_slot:
                    return None
                self._store.transition_intent(
                    record.intent.intent_id,
                    expected=IntentStatus.PREPARED,
                    target=IntentStatus.CANCELED,
                )
            else:
                return SymbolReviewResult(
                    symbol=symbol,
                    disposition=ReviewDisposition.ORDER_PENDING,
                    detail="active intent remained after the batch recovery pass",
                )

        current_status = self._store.get_intent_status(current_intent_id)
        if current_status in _TERMINAL_INTENT_STATUSES:
            return SymbolReviewResult(
                symbol=symbol,
                disposition=ReviewDisposition.ORDER_RESOLVED,
                execution=ExecutionResult(
                    intent_id=current_intent_id,
                    status=current_status,
                    resolution=ExecutionResolution.ALREADY_TERMINAL,
                ),
            )
        return None

    def _cancel_prepared_intent(self, account_id: str, intent_id: str) -> None:
        if self._store.get_intent_status(intent_id) is not IntentStatus.PREPARED:
            return
        active = self._store.list_active_order_intents(account_id)
        record = next((item for item in active if item.intent.intent_id == intent_id), None)
        if record is None:
            return
        if record.attempt is not None:
            return
        try:
            self._store.transition_intent(
                intent_id,
                expected=IntentStatus.PREPARED,
                target=IntentStatus.CANCELED,
            )
        except IntentTransitionError:
            return

    @staticmethod
    def _make_intent(
        *,
        account_id: str,
        state: SymbolState,
        plan: MarketOrderPlan,
        decision: Decision,
        business_key: str,
        intent_id: str,
        slot_start_ms: int,
    ) -> OrderIntent:
        buy_actions = {Action.OPEN_T1, Action.ADD_T2, Action.ADD_T3}
        return OrderIntent(
            intent_id=intent_id,
            account_id=account_id,
            symbol=state.symbol,
            cycle_id=state.cycle_id,
            action=plan.action,
            business_key=business_key,
            expected_state_version=state.version,
            created_at_ms=slot_start_ms,
            target_cumulative_sold_qty=(
                plan.target_cumulative_sold_qty
                if plan.action not in buy_actions
                else None
            ),
            buy_notional=(
                decision.buy_notional
                if plan.action in buy_actions
                else None
            ),
        )


_TERMINAL_INTENT_STATUSES = {
    IntentStatus.FILLED,
    IntentStatus.REJECTED,
    IntentStatus.CANCELED,
    IntentStatus.EXPIRED,
}


def _client_order_id(account_id: str, business_key: str) -> str:
    digest = sha256(f"{account_id}\0{business_key}".encode("utf-8")).hexdigest()[:28]
    return f"pnl-{digest}"
