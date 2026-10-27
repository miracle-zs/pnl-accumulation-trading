from decimal import Decimal
import unittest

from pnl_trader.application.market_models import InstrumentRules, MarketQuantityRules
from pnl_trader.application.order_planning import MarketOrderPlanner
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
            Decision(Action.OPEN_T1, buy_notional=D("10")),
            state,
            self.rules,
            D("3"),
        )

        self.assertEqual(plan.quantity, D("3.33"))
        self.assertLessEqual(plan.quantity * D("3"), D("10"))
        self.assertEqual(plan.quantity * D("3"), D("9.99"))

    def test_subminimum_take_profit_batch_merges_to_full_close(self) -> None:
        state = SymbolState(
            symbol="TESTUSDT",
            tier=Tier.T2,
            lifecycle=Lifecycle.OPEN,
            qty=D("0.5"),
            avg_entry_price=D("60"),
            tier_entry_qty=D("0.5"),
            tier_anchor_price=D("50"),
        )

        plan = self.planner.plan(
            Decision(Action.CLOSE_T2_BATCH1, close_qty=D("0.25")),
            state,
            self.rules,
            D("10"),
        )

        self.assertEqual(plan.action, Action.CLOSE_T2_FULL)
        self.assertEqual(plan.quantity, D("0.5"))
        self.assertEqual(plan.target_cumulative_sold_qty, D("0.5"))
        self.assertEqual(plan.quantity * D("10"), D("5.0"))

    def test_below_minimum_batch_is_rejected_when_full_close_is_also_below_minimum(self) -> None:
        state = SymbolState(
            symbol="TESTUSDT",
            tier=Tier.T2,
            lifecycle=Lifecycle.OPEN,
            qty=D("0.5"),
            avg_entry_price=D("60"),
            tier_entry_qty=D("0.5"),
            tier_anchor_price=D("50"),
        )

        with self.assertRaises(ValueError):
            self.planner.plan(
                Decision(Action.CLOSE_T2_BATCH1, close_qty=D("0.25")),
                state,
                self.rules,
                D("9"),
            )



if __name__ == "__main__":
    unittest.main()
