"""Pure pre-submit sizing and exchange-rule validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..domain.models import (
    BATCH_CLOSE_ACTIONS,
    BUY_ACTIONS,
    CLOSE_ACTIONS,
    Action,
    Decision,
    FULL_CLOSE_BY_TIER,
    SymbolState,
    ZERO,
)
from .market_models import InstrumentRules


class OrderRuleUnavailable(ValueError):
    """Exchange quantity and contract rules prevent this order plan."""


class OrderBelowMinimum(OrderRuleUnavailable):
    """Order cannot satisfy the exchange's minimum quantity or notional."""


@dataclass(frozen=True, slots=True)
class MarketOrderPlan:
    action: Action
    quantity: Decimal
    target_cumulative_sold_qty: Decimal | None


def plan_market_order(
    decision: Decision,
    state: SymbolState,
    rules: InstrumentRules,
    price: Decimal,
) -> MarketOrderPlan:
    """Convert a pure strategy decision into one legal market-order quantity."""
    if decision.action is Action.HOLD:
        raise ValueError(
            "HOLD has no market order",
        )
    if rules.symbol != state.symbol:
        raise ValueError(
            "state and instrument rules must refer to the same symbol",
        )
    # PRE_DELIVERING allows reducing or closing positions, but no buys.
    if rules.status != "TRADING" and not (
        rules.status == "PRE_DELIVERING" and decision.action in CLOSE_ACTIONS
    ):
        raise OrderRuleUnavailable(
            f"symbol status is {rules.status}",
        )
    if rules.contract_type != "PERPETUAL":
        raise OrderRuleUnavailable(
            f"contract type is {rules.contract_type}",
        )
    if rules.quote_asset != "USDT" or rules.margin_asset != "USDT":
        raise OrderRuleUnavailable(
            "this strategy only plans USDT-quoted, USDT-margined contracts",
        )

    if decision.action in BUY_ACTIONS:
        if decision.buy_notional is None:
            raise ValueError(
                "buy decision is missing its notional budget",
            )
        requested_qty = decision.buy_notional / price
        quantity = rules.market_quantity.align_down(requested_qty)
        _validate_quantity(quantity, rules, price)
        return MarketOrderPlan(
            action=decision.action,
            quantity=quantity,
            target_cumulative_sold_qty=None,
        )

    batch_close = decision.action in BATCH_CLOSE_ACTIONS
    if decision.close_qty is None:
        raise ValueError(
            "close decision is missing its incremental quantity",
        )
    if state.qty <= ZERO or decision.close_qty > state.qty:
        raise ValueError(
            "close quantity requires a non-empty position and cannot exceed it",
        )
    if not batch_close and decision.close_qty != state.qty:
        raise ValueError(
            "a full-close action must request the entire remaining position",
        )

    quantity = rules.market_quantity.align_down(decision.close_qty)
    if not batch_close and quantity != state.qty:
        raise ValueError(
            "the complete remaining position is not aligned to the market quantity grid",
        )
    action = decision.action
    if batch_close:
        merge_to_full_close = quantity <= ZERO
        if not merge_to_full_close:
            _validate_quantity(
                quantity,
                rules,
                price,
                check_min_notional=False,
            )
            merge_to_full_close = quantity * price < rules.min_market_notional
        if merge_to_full_close:
            action = FULL_CLOSE_BY_TIER[state.tier]
            quantity = rules.market_quantity.align_down(state.qty)
            _validate_quantity(quantity, rules, price, check_min_notional=False)
            if quantity != state.qty:
                raise OrderRuleUnavailable(
                    "the complete remaining position is not aligned to the market quantity grid",
                )
    else:
        # Full closes are reduce-only and exempt from MIN_NOTIONAL.
        _validate_quantity(quantity, rules, price, check_min_notional=False)
    target = state.tier_sold_qty + quantity
    return MarketOrderPlan(
        action=action,
        quantity=quantity,
        target_cumulative_sold_qty=target,
    )


def _validate_quantity(
    quantity: Decimal,
    rules: InstrumentRules,
    reference_price: Decimal,
    *,
    check_min_notional: bool = True,
) -> None:
    market_rules = rules.market_quantity
    if quantity <= ZERO:
        raise OrderBelowMinimum(
            "aligned market quantity is zero or below minQty",
        )
    if quantity > market_rules.max_qty:
        raise OrderRuleUnavailable(
            f"{quantity} exceeds market maxQty {market_rules.max_qty}; "
            "split planning is required",
        )
    notional = quantity * reference_price
    if check_min_notional and notional < rules.min_market_notional:
        raise OrderBelowMinimum(
            f"estimated market notional {notional} is below {rules.min_market_notional}",
        )
