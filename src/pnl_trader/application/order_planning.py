"""Pure pre-submit sizing and exchange-rule validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..domain.models import (
    BATCH_CLOSE_ACTIONS,
    BUY_ACTIONS,
    Action,
    Decision,
    SymbolState,
    Tier,
    ZERO,
)
from .market_models import InstrumentRules


@dataclass(frozen=True, slots=True)
class MarketOrderPlan:
    action: Action
    quantity: Decimal
    target_cumulative_sold_qty: Decimal | None


class MarketOrderPlanner:
    """Convert a pure strategy decision into one legal market-order quantity."""

    def plan(
        self,
        decision: Decision,
        state: SymbolState,
        rules: InstrumentRules,
        price: Decimal,
    ) -> MarketOrderPlan:
        if decision.action is Action.HOLD:
            raise ValueError(
                "HOLD has no market order",
            )
        if rules.symbol != state.symbol:
            raise ValueError(
                "state and instrument rules must refer to the same symbol",
            )
        if rules.status != "TRADING":
            raise ValueError(
                f"symbol status is {rules.status}",
            )
        if rules.contract_type != "PERPETUAL":
            raise ValueError(
                f"contract type is {rules.contract_type}",
            )
        if rules.quote_asset != "USDT" or rules.margin_asset != "USDT":
            raise ValueError(
                "this strategy only plans USDT-quoted, USDT-margined contracts",
            )

        if decision.action in BUY_ACTIONS:
            if decision.buy_notional is None:
                raise ValueError(
                    "buy decision is missing its notional budget",
                )
            requested_qty = decision.buy_notional / price
            quantity = rules.market_quantity.align_down(requested_qty)
            self._validate_quantity(quantity, rules, price)
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
            if quantity <= ZERO:
                action, quantity = self._merge_batch_to_full_close(
                    state, rules, price
                )
            else:
                self._validate_quantity(
                    quantity,
                    rules,
                    price,
                    check_min_notional=False,
                )
                if self._below_min_notional(quantity * price, rules):
                    action, quantity = self._merge_batch_to_full_close(
                        state, rules, price
                    )
        else:
            self._validate_quantity(quantity, rules, price)
        target = state.tier_sold_qty + quantity
        return MarketOrderPlan(
            action=action,
            quantity=quantity,
            target_cumulative_sold_qty=target,
        )

    @classmethod
    def _merge_batch_to_full_close(
        cls,
        state: SymbolState,
        rules: InstrumentRules,
        reference_price: Decimal,
    ) -> tuple[Action, Decimal]:
        action = _FULL_CLOSE_BY_TIER[state.tier]
        quantity = rules.market_quantity.align_down(state.qty)
        cls._validate_quantity(quantity, rules, reference_price)
        if quantity != state.qty:
            raise ValueError(
                "the complete remaining position is not aligned to the market quantity grid",
            )
        return action, quantity

    @staticmethod
    def _below_min_notional(notional: Decimal, rules: InstrumentRules) -> bool:
        return notional < rules.min_market_notional

    @classmethod
    def _validate_quantity(
        cls,
        quantity: Decimal,
        rules: InstrumentRules,
        reference_price: Decimal,
        *,
        check_min_notional: bool = True,
    ) -> None:
        market_rules = rules.market_quantity
        if quantity <= ZERO:
            raise ValueError(
                "aligned market quantity is zero or below minQty",
            )
        if quantity > market_rules.max_qty:
            raise ValueError(
                f"{quantity} exceeds market maxQty {market_rules.max_qty}; "
                "split planning is required",
            )
        notional = quantity * reference_price
        if check_min_notional and cls._below_min_notional(notional, rules):
            raise ValueError(
                f"estimated market notional {notional} is below {rules.min_market_notional}",
            )
_FULL_CLOSE_BY_TIER = {Tier.T2: Action.CLOSE_T2_FULL, Tier.T3: Action.CLOSE_T3_FULL}
