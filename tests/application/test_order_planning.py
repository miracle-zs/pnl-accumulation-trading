from decimal import Decimal
import unittest

from pnl_trader.application import (
    InstrumentRules,
    MarkPriceSnapshot,
    MarketOrderPlanner,
    MarketQuantityRules,
    OrderPlanningError,
    OrderPlanRejection,
)
from pnl_trader.domain import Action, Decision, Lifecycle, SymbolState, Tier


D = Decimal


def market_rules(*, max_qty: str = "1000") -> InstrumentRules:
    return InstrumentRules(
        symbol="TESTUSDT",
        contract_type="PERPETUAL",
        status="TRADING",
        quote_asset="USDT",
        margin_asset="USDT",
        market_quantity=MarketQuantityRules(
            min_qty=D("0.01"),
            max_qty=D(max_qty),
            step_size=D("0.01"),
            step_origin=D("0"),
        ),
        min_market_notional=D("5"),
    )


class MarketOrderPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = MarketOrderPlanner()
        self.rules = market_rules()

    def test_buy_quantity_is_floored_without_exceeding_the_notional_budget(self) -> None:
        state = SymbolState(symbol="TESTUSDT")

        plan = self.planner.plan(
            Decision(Action.OPEN_T1, "open initial position", buy_notional=D("10")),
            state,
            self.rules,
            MarkPriceSnapshot("TESTUSDT", D("3"), 1),
        )

        self.assertEqual(plan.quantity, D("3.33"))
        self.assertLessEqual(plan.estimated_notional, D("10"))
        self.assertEqual(plan.estimated_notional, D("9.99"))

    def test_subminimum_take_profit_batch_merges_to_full_close(self) -> None:
        state = SymbolState(
            symbol="TESTUSDT",
            cycle_id=1,
            tier=Tier.T2,
            lifecycle=Lifecycle.OPEN,
            qty=D("0.5"),
            avg_entry_price=D("60"),
            tier_entry_qty=D("0.5"),
            tier_anchor_price=D("50"),
        )

        plan = self.planner.plan(
            Decision(Action.CLOSE_T2_BATCH1, "first batch", close_qty=D("0.25")),
            state,
            self.rules,
            MarkPriceSnapshot("TESTUSDT", D("10"), 1),
        )

        self.assertEqual(plan.source_action, Action.CLOSE_T2_BATCH1)
        self.assertEqual(plan.action, Action.CLOSE_T2_FULL)
        self.assertEqual(plan.quantity, D("0.5"))
        self.assertEqual(plan.target_cumulative_sold_qty, D("0.5"))
        self.assertEqual(plan.estimated_notional, D("5.0"))
        self.assertTrue(plan.merged_to_full_close)

    def test_below_minimum_batch_is_rejected_when_full_close_is_also_below_minimum(self) -> None:
        state = SymbolState(
            symbol="TESTUSDT",
            cycle_id=1,
            tier=Tier.T2,
            lifecycle=Lifecycle.OPEN,
            qty=D("0.5"),
            avg_entry_price=D("60"),
            tier_entry_qty=D("0.5"),
            tier_anchor_price=D("50"),
        )

        with self.assertRaises(OrderPlanningError) as raised:
            self.planner.plan(
                Decision(Action.CLOSE_T2_BATCH1, "first batch", close_qty=D("0.25")),
                state,
                self.rules,
                MarkPriceSnapshot("TESTUSDT", D("9"), 1),
            )

        self.assertEqual(raised.exception.reason, OrderPlanRejection.BELOW_MIN_NOTIONAL)


if __name__ == "__main__":
    unittest.main()
