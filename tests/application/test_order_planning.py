from decimal import Decimal
import unittest

from pnl_trader.application.market_models import InstrumentRules, MarketQuantityRules
from pnl_trader.application.order_planning import MarketOrderPlanner
from pnl_trader.domain import (
    Action, Decision, Lifecycle, Observation, StrategyPolicy, SymbolState, Tier,
)


D = Decimal


def market_rules(*, max_qty: str = "1000", min_notional: str = "5") -> InstrumentRules:
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
        min_market_notional=D(min_notional),
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

    def test_buy_below_symbol_minimum_notional_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "below 20"):
            self.planner.plan(
                Decision(Action.OPEN_T1, buy_notional=D("10")),
                SymbolState(symbol="TESTUSDT"),
                market_rules(min_notional="20"),
                D("3"),
            )

    def test_t3_writeoff_can_close_below_minimum_notional(self) -> None:
        for batches, qty in ((0, "3"), (1, "2"), (2, "1")):
            with self.subTest(completed_batches=batches):
                state = SymbolState(
                    symbol="TESTUSDT",
                    tier=Tier.T3,
                    lifecycle=Lifecycle.OPEN,
                    qty=D(qty),
                    avg_entry_price=D("10"),
                    tier_entry_qty=D("3"),
                    tier_anchor_price=D("5"),
                    completed_batches=batches,
                )
                price = D("1.6")
                decision = StrategyPolicy().decide(
                    state, Observation(admitted=True, price=price)
                )

                plan = self.planner.plan(decision, state, self.rules, price)

                self.assertEqual(plan.action, Action.CLOSE_T3_FULL)
                self.assertEqual(plan.quantity, state.qty)
                self.assertEqual(plan.target_cumulative_sold_qty, state.tier_entry_qty)
                self.assertLess(plan.quantity * price, self.rules.min_market_notional)

    def test_full_close_still_rejects_quantity_above_market_maximum(self) -> None:
        state = SymbolState(
            symbol="TESTUSDT", tier=Tier.T3, lifecycle=Lifecycle.OPEN,
            qty=D("3"), avg_entry_price=D("10"), tier_entry_qty=D("3"),
            tier_anchor_price=D("5"),
        )
        decision = StrategyPolicy().decide(
            state, Observation(admitted=True, price=D("1.6"))
        )

        with self.assertRaisesRegex(ValueError, "exceeds market maxQty"):
            self.planner.plan(decision, state, market_rules(max_qty="2"), D("1.6"))

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

    def test_below_minimum_batch_can_merge_when_full_close_is_also_below_minimum(self) -> None:
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
            D("9"),
        )

        self.assertEqual(plan.action, Action.CLOSE_T2_FULL)
        self.assertEqual(plan.quantity, D("0.5"))
        self.assertLess(plan.quantity * D("9"), self.rules.min_market_notional)



if __name__ == "__main__":
    unittest.main()
