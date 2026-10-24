"""Pure pre-submit sizing and exchange-rule validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from ..domain.models import Action, Decision, Lifecycle, SymbolState, Tier, ZERO
from .market_models import InstrumentRules, MarkPriceSnapshot


class OrderPlanRejection(str, Enum):
    NON_EXECUTABLE_ACTION = "NON_EXECUTABLE_ACTION"
    MARKET_DATA_MISMATCH = "MARKET_DATA_MISMATCH"
    SYMBOL_NOT_TRADING = "SYMBOL_NOT_TRADING"
    UNSUPPORTED_CONTRACT = "UNSUPPORTED_CONTRACT"
    UNSUPPORTED_ASSET = "UNSUPPORTED_ASSET"
    INVALID_POSITION = "INVALID_POSITION"
    INVALID_QUANTITY = "INVALID_QUANTITY"
    BELOW_MIN_QTY = "BELOW_MIN_QTY"
    ABOVE_MAX_QTY = "ABOVE_MAX_QTY"
    BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"
    ABOVE_MAX_NOTIONAL = "ABOVE_MAX_NOTIONAL"


class OrderPlanningError(ValueError):
    def __init__(self, reason: OrderPlanRejection, detail: str) -> None:
        self.reason = reason
        super().__init__(f"{reason.value}: {detail}")


@dataclass(frozen=True, slots=True)
class MarketOrderPlan:
    source_action: Action
    action: Action
    quantity: Decimal
    requested_quantity: Decimal
    estimated_notional: Decimal
    target_cumulative_sold_qty: Decimal | None
    merged_to_full_close: bool
    reason: str

    def __post_init__(self) -> None:
        for name in ("quantity", "requested_quantity", "estimated_notional"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
                raise ValueError(f"{name} must be a positive finite Decimal")
        if self.action not in _BUY_ACTIONS | _CLOSE_ACTIONS:
            raise ValueError("a market order plan must contain an executable action")
        if (self.action in _BUY_ACTIONS) != (self.target_cumulative_sold_qty is None):
            raise ValueError("only sell plans may define a cumulative close target")
        if self.target_cumulative_sold_qty is not None and (
            not isinstance(self.target_cumulative_sold_qty, Decimal)
            or not self.target_cumulative_sold_qty.is_finite()
            or self.target_cumulative_sold_qty <= ZERO
        ):
            raise ValueError("target_cumulative_sold_qty must be positive and finite")

    @property
    def side(self) -> str:
        return "BUY" if self.action in _BUY_ACTIONS else "SELL"

    @property
    def reduce_only(self) -> bool:
        return self.side == "SELL"


class MarketOrderPlanner:
    """Convert a pure strategy decision into one legal market-order quantity."""

    def plan(
        self,
        decision: Decision,
        state: SymbolState,
        rules: InstrumentRules,
        price: MarkPriceSnapshot,
    ) -> MarketOrderPlan:
        if decision.action not in _BUY_ACTIONS | _CLOSE_ACTIONS:
            raise OrderPlanningError(
                OrderPlanRejection.NON_EXECUTABLE_ACTION,
                "HOLD has no market order",
            )
        if rules.symbol != state.symbol or price.symbol != state.symbol:
            raise OrderPlanningError(
                OrderPlanRejection.MARKET_DATA_MISMATCH,
                "state, instrument rules and mark price must refer to the same symbol",
            )
        if rules.status != "TRADING":
            raise OrderPlanningError(
                OrderPlanRejection.SYMBOL_NOT_TRADING,
                f"symbol status is {rules.status}",
            )
        if rules.contract_type != "PERPETUAL":
            raise OrderPlanningError(
                OrderPlanRejection.UNSUPPORTED_CONTRACT,
                f"contract type is {rules.contract_type}",
            )
        if rules.quote_asset != "USDT" or rules.margin_asset != "USDT":
            raise OrderPlanningError(
                OrderPlanRejection.UNSUPPORTED_ASSET,
                "this strategy only plans USDT-quoted, USDT-margined contracts",
            )

        if decision.action in _BUY_ACTIONS:
            if not _buy_action_matches_state(decision.action, state):
                raise OrderPlanningError(
                    OrderPlanRejection.INVALID_POSITION,
                    "buy decision does not match the current symbol tier and lifecycle",
                )
            if decision.buy_notional is None:
                raise OrderPlanningError(
                    OrderPlanRejection.INVALID_QUANTITY,
                    "buy decision is missing its notional budget",
                )
            requested_qty = decision.buy_notional / price.mark_price
            quantity = rules.market_quantity.align_down(requested_qty)
            self._validate_quantity(quantity, rules, price.mark_price)
            return MarketOrderPlan(
                source_action=decision.action,
                action=decision.action,
                quantity=quantity,
                requested_quantity=requested_qty,
                estimated_notional=quantity * price.mark_price,
                target_cumulative_sold_qty=None,
                merged_to_full_close=False,
                reason=decision.reason,
            )

        if decision.close_qty is None:
            raise OrderPlanningError(
                OrderPlanRejection.INVALID_QUANTITY,
                "close decision is missing its incremental quantity",
            )
        if (
            state.lifecycle not in {Lifecycle.OPEN, Lifecycle.QUARANTINED}
            or state.qty <= ZERO
            or not _action_matches_tier(decision.action, state.tier)
            or decision.close_qty > state.qty
        ):
            raise OrderPlanningError(
                OrderPlanRejection.INVALID_POSITION,
                "close decision does not match a known open position",
            )
        if decision.action not in _BATCH_CLOSE_ACTIONS and decision.close_qty != state.qty:
            raise OrderPlanningError(
                OrderPlanRejection.INVALID_QUANTITY,
                "a full-close action must request the entire remaining position",
            )

        quantity = rules.market_quantity.align_down(decision.close_qty)
        if decision.action not in _BATCH_CLOSE_ACTIONS and quantity != state.qty:
            raise OrderPlanningError(
                OrderPlanRejection.INVALID_QUANTITY,
                "the complete remaining position is not aligned to the market quantity grid",
            )
        merged = False
        action = decision.action
        if quantity <= ZERO:
            if decision.action not in _BATCH_CLOSE_ACTIONS:
                self._validate_quantity(quantity, rules, price.mark_price)
            else:
                action, quantity = self._merge_batch_to_full_close(
                    state, rules, price.mark_price
                )
                merged = True
        else:
            self._validate_quantity(
                quantity,
                rules,
                price.mark_price,
                check_min_notional=False,
            )
        planned_notional = quantity * price.mark_price
        if not merged and self._below_min_notional(planned_notional, rules):
            if decision.action in _BATCH_CLOSE_ACTIONS:
                action, quantity = self._merge_batch_to_full_close(
                    state, rules, price.mark_price
                )
                merged = True
                planned_notional = quantity * price.mark_price
            else:
                self._validate_quantity(quantity, rules, price.mark_price)

        if merged:
            planned_notional = quantity * price.mark_price
        target = state.tier_sold_qty + quantity
        return MarketOrderPlan(
            source_action=decision.action,
            action=action,
            quantity=quantity,
            requested_quantity=decision.close_qty,
            estimated_notional=planned_notional,
            target_cumulative_sold_qty=target,
            merged_to_full_close=merged,
            reason=(
                f"{decision.reason}; merged to full close because the batch is below market minimum"
                if merged
                else decision.reason
            ),
        )

    @classmethod
    def _merge_batch_to_full_close(
        cls,
        state: SymbolState,
        rules: InstrumentRules,
        mark_price: Decimal,
    ) -> tuple[Action, Decimal]:
        action = _FULL_CLOSE_BY_TIER[state.tier]
        quantity = rules.market_quantity.align_down(state.qty)
        cls._validate_quantity(quantity, rules, mark_price)
        if quantity != state.qty:
            raise OrderPlanningError(
                OrderPlanRejection.INVALID_QUANTITY,
                "the complete remaining position is not aligned to the market quantity grid",
            )
        return action, quantity

    @staticmethod
    def _below_min_notional(notional: Decimal, rules: InstrumentRules) -> bool:
        minimum = rules.min_market_notional
        return minimum is not None and notional < minimum

    @classmethod
    def _validate_quantity(
        cls,
        quantity: Decimal,
        rules: InstrumentRules,
        mark_price: Decimal,
        *,
        check_min_notional: bool = True,
    ) -> None:
        market_rules = rules.market_quantity
        if quantity <= ZERO:
            raise OrderPlanningError(
                OrderPlanRejection.BELOW_MIN_QTY,
                "aligned market quantity is zero or below minQty",
            )
        if quantity > market_rules.max_qty:
            raise OrderPlanningError(
                OrderPlanRejection.ABOVE_MAX_QTY,
                f"{quantity} exceeds market maxQty {market_rules.max_qty}; "
                "split planning is required",
            )
        if not market_rules.contains(quantity):
            reason = (
                OrderPlanRejection.BELOW_MIN_QTY
                if quantity < market_rules.min_qty
                else OrderPlanRejection.INVALID_QUANTITY
            )
            raise OrderPlanningError(reason, "quantity is outside the market quantity grid")
        notional = quantity * mark_price
        if check_min_notional and cls._below_min_notional(notional, rules):
            raise OrderPlanningError(
                OrderPlanRejection.BELOW_MIN_NOTIONAL,
                f"estimated market notional {notional} is below {rules.min_market_notional}",
            )
        maximum = rules.max_market_notional
        if maximum is not None and notional > maximum:
            raise OrderPlanningError(
                OrderPlanRejection.ABOVE_MAX_NOTIONAL,
                f"estimated market notional {notional} exceeds {maximum}",
            )


_BUY_ACTIONS = {Action.OPEN_T1, Action.ADD_T2, Action.ADD_T3}
_CLOSE_ACTIONS = {
    Action.CLOSE_T1,
    Action.CLOSE_T2_BATCH1,
    Action.CLOSE_T2_FULL,
    Action.CLOSE_T3_TO_ONE_THIRD,
    Action.CLOSE_T3_TO_TWO_THIRDS,
    Action.CLOSE_T3_FULL,
}
_BATCH_CLOSE_ACTIONS = {
    Action.CLOSE_T2_BATCH1,
    Action.CLOSE_T3_TO_ONE_THIRD,
    Action.CLOSE_T3_TO_TWO_THIRDS,
}
_FULL_CLOSE_BY_TIER = {Tier.T2: Action.CLOSE_T2_FULL, Tier.T3: Action.CLOSE_T3_FULL}


def _action_matches_tier(action: Action, tier: Tier) -> bool:
    return (
        (action is Action.CLOSE_T1 and tier is Tier.T1)
        or (action in {Action.CLOSE_T2_BATCH1, Action.CLOSE_T2_FULL} and tier is Tier.T2)
        or (
            action in {
                Action.CLOSE_T3_TO_ONE_THIRD,
                Action.CLOSE_T3_TO_TWO_THIRDS,
                Action.CLOSE_T3_FULL,
            }
            and tier is Tier.T3
        )
    )


def _buy_action_matches_state(action: Action, state: SymbolState) -> bool:
    return (
        (
            action is Action.OPEN_T1
            and state.tier is Tier.NONE
            and state.lifecycle in {Lifecycle.IDLE, Lifecycle.REOPEN_PENDING}
        )
        or (
            action is Action.ADD_T2
            and state.tier is Tier.T1
            and state.lifecycle in {Lifecycle.OPEN, Lifecycle.QUARANTINED}
        )
        or (
            action is Action.ADD_T3
            and state.tier is Tier.T2
            and state.lifecycle in {Lifecycle.OPEN, Lifecycle.QUARANTINED}
        )
    )
