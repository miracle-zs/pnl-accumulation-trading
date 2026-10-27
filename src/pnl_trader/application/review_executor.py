"""Compose one strategy review with durable order execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
from uuid import NAMESPACE_URL, uuid5

from ..domain.models import BUY_ACTIONS, Action, Lifecycle, Observation
from ..domain.policy import StrategyPolicy
from .execution import ExecutionCoordinator, ExecutionResult
from .exchange_errors import UnsupportedLeverageError
from .execution_models import IntentStatus, OrderIntent
from .market_models import FifteenMinuteCloseSnapshot
from .order_planning import MarketOrderPlan, MarketOrderPlanner
from .ports import ExchangeGatewayPort, StateStorePort
from .store_errors import (
    ActiveIntentConflict,
    IntentConflict,
)

logger = logging.getLogger(__name__)
LEVERAGE_PREFERENCE = (10, 5, 3)


class ReviewDisposition(str, Enum):
    NO_ACTION = "NO_ACTION"
    LEVERAGE_UNAVAILABLE = "LEVERAGE_UNAVAILABLE"
    INTENT_CONFLICT = "INTENT_CONFLICT"
    ORDER_PENDING = "ORDER_PENDING"
    ORDER_RESOLVED = "ORDER_RESOLVED"
    EXECUTION = "EXECUTION"


@dataclass(frozen=True, slots=True)
class SymbolReviewResult:
    disposition: ReviewDisposition
    plan: MarketOrderPlan | None = None
    execution: ExecutionResult | None = None
    close_snapshot: FifteenMinuteCloseSnapshot | None = None


class SymbolReviewExecutor:
    """Run one symbol through policy, sizing and durable order execution."""

    def __init__(
        self,
        *,
        store: StateStorePort,
        exchange: ExchangeGatewayPort,
        execution: ExecutionCoordinator,
    ) -> None:
        self._store = store
        self._exchange = exchange
        self._execution = execution
        self._policy = StrategyPolicy()
        self._planner = MarketOrderPlanner()
        self._symbol_leverages: dict[str, int] = {}
        self._unsupported_leverage_symbols: set[str] = set()

    async def review_symbol(
        self,
        *,
        symbol: str,
        slot_start_ms: int,
        admitted: bool,
        close_snapshot: FifteenMinuteCloseSnapshot | None = None,
    ) -> SymbolReviewResult:
        """Review one action for a symbol in a 15-minute slot."""

        state = self._store.load_symbol_state(symbol)
        intent_key = f"{slot_start_ms}:{symbol}"
        if state.lifecycle is Lifecycle.REOPEN_PENDING:
            intent_key += ":reopen"
        intent_id = str(uuid5(NAMESPACE_URL, f"pnl-periodic-review:{intent_key}"))
        existing = await self._resolve_prior_symbol_intent(
            symbol=symbol,
            current_intent_id=intent_id,
        )
        if existing is not None:
            return existing

        if close_snapshot is None:
            candle = await self._exchange.get_fifteen_minute_close(
                symbol,
                slot_start_ms,
            )
        else:
            candle = close_snapshot
        observation = Observation(
            admitted=admitted,
            close_price=candle.close_price,
        )
        decision = self._policy.decide(state, observation)
        if decision.action is Action.HOLD:
            return SymbolReviewResult(disposition=ReviewDisposition.NO_ACTION)

        rules = await self._exchange.get_instrument_rules(symbol)
        plan = self._planner.plan(decision, state, rules, candle)

        if plan.action in BUY_ACTIONS and not await self._ensure_symbol_leverage(symbol):
            return SymbolReviewResult(
                disposition=ReviewDisposition.LEVERAGE_UNAVAILABLE,
                plan=plan,
            )

        intent = OrderIntent(
            intent_id=intent_id,
            symbol=state.symbol,
            action=plan.action,
            created_at_ms=slot_start_ms,
            target_cumulative_sold_qty=plan.target_cumulative_sold_qty,
        )
        try:
            self._store.create_order_intent(intent)
        except IntentConflict:
            return SymbolReviewResult(
                disposition=ReviewDisposition.INTENT_CONFLICT,
                plan=plan,
            )
        except ActiveIntentConflict:
            return SymbolReviewResult(disposition=ReviewDisposition.ORDER_PENDING, plan=plan)

        execution_result = await self._execution.execute_prepared(intent, plan.quantity)
        return SymbolReviewResult(
            disposition=ReviewDisposition.EXECUTION,
            plan=plan,
            execution=execution_result,
            close_snapshot=candle,
        )

    async def _ensure_symbol_leverage(self, symbol: str) -> bool:
        if symbol in self._symbol_leverages:
            return True
        if symbol in self._unsupported_leverage_symbols:
            return False

        for leverage in LEVERAGE_PREFERENCE:
            try:
                actual_leverage = await self._exchange.set_leverage(symbol, leverage)
            except UnsupportedLeverageError:
                logger.info(
                    "requested leverage is unsupported: symbol=%s leverage=%sx",
                    symbol,
                    leverage,
                )
                continue

            self._symbol_leverages[symbol] = actual_leverage
            logger.info(
                "configured Binance leverage: symbol=%s leverage=%sx",
                symbol,
                actual_leverage,
            )
            return True

        self._unsupported_leverage_symbols.add(symbol)
        logger.warning(
            "symbol skipped because Binance rejected 10x, 5x and 3x leverage: symbol=%s",
            symbol,
        )
        return False

    async def _resolve_prior_symbol_intent(
        self,
        *,
        symbol: str,
        current_intent_id: str,
    ) -> SymbolReviewResult | None:
        active = self._store.list_active_order_intents(symbol)
        if active:
            record = active[0]
            same_slot = record.intent.intent_id == current_intent_id
            if record.status is IntentStatus.PREPARED:
                if same_slot:
                    return None
                self._store.transition_intent(
                    record.intent.intent_id,
                    expected=IntentStatus.PREPARED,
                    target=IntentStatus.CANCELED,
                )
            else:
                return SymbolReviewResult(disposition=ReviewDisposition.ORDER_PENDING)

        current_status = self._store.get_intent_status(current_intent_id)
        if current_status in _TERMINAL_INTENT_STATUSES:
            return SymbolReviewResult(
                disposition=ReviewDisposition.ORDER_RESOLVED,
                execution=ExecutionResult(
                    intent_id=current_intent_id,
                    status=current_status,
                ),
            )
        return None

_TERMINAL_INTENT_STATUSES = {
    IntentStatus.FILLED,
    IntentStatus.REJECTED,
    IntentStatus.CANCELED,
    IntentStatus.EXPIRED,
}
