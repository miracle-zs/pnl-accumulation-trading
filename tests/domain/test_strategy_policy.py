from decimal import Decimal
import unittest

from pnl_trader.domain import (
    Action,
    ExecutionReceipt,
    InvalidExecution,
    Lifecycle,
    Observation,
    StrategyPolicy,
    SymbolState,
    Tier,
    apply_confirmed_execution,
)


D = Decimal


def open_state(
    tier: Tier,
    *,
    qty: str,
    entry_qty: str,
    average: str,
    anchor: str,
    completed_batches: int = 0,
) -> SymbolState:
    return SymbolState(
        symbol="TESTUSDT",
        tier=tier,
        lifecycle=Lifecycle.OPEN,
        qty=D(qty),
        avg_entry_price=D(average),
        tier_entry_qty=D(entry_qty),
        tier_anchor_price=D(anchor),
        completed_batches=completed_batches,
    )


class StrategyPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = StrategyPolicy()

    def decide(self, state: SymbolState, price: str, *, admitted: bool = False):
        return self.policy.decide(
            state,
            Observation(admitted=admitted, close_price=D(price)),
        )

    def test_t1_takes_profit_at_exact_threshold(self) -> None:
        state = open_state(Tier.T1, qty="0.1", entry_qty="0.1", average="100", anchor="100")

        decision = self.decide(state, "110")

        self.assertEqual(decision.action, Action.CLOSE_T1)
        self.assertEqual(decision.close_qty, D("0.1"))

    def test_t1_adds_t2_at_exact_half_price(self) -> None:
        state = open_state(Tier.T1, qty="0.1", entry_qty="0.1", average="100", anchor="100")

        decision = self.decide(state, "50", admitted=True)

        self.assertEqual(decision.action, Action.ADD_T2)
        self.assertEqual(decision.buy_notional, D("20.0"))

    def test_t2_first_batch_targets_half_of_tier_entry_quantity(self) -> None:
        state = open_state(Tier.T2, qty="0.5", entry_qty="0.5", average="60", anchor="50")

        decision = self.decide(state, "72")

        self.assertEqual(decision.action, Action.CLOSE_T2_BATCH1)
        self.assertEqual(decision.close_qty, D("0.25"))

    def test_t2_full_exit_takes_priority_over_first_batch(self) -> None:
        state = open_state(Tier.T2, qty="0.5", entry_qty="0.5", average="60", anchor="50")

        decision = self.decide(state, "90")

        self.assertEqual(decision.action, Action.CLOSE_T2_FULL)
        self.assertEqual(decision.close_qty, D("0.5"))

    def test_t2_1_full_exit_closes_remaining_half_at_50_percent(self) -> None:
        state = open_state(
            Tier.T2,
            qty="0.25",
            entry_qty="0.5",
            average="60",
            anchor="50",
            completed_batches=1,
        )

        decision = self.decide(state, "90")

        self.assertEqual(decision.action, Action.CLOSE_T2_FULL)
        self.assertEqual(decision.close_qty, D("0.25"))

    def test_t3_jumps_to_cumulative_two_thirds_target(self) -> None:
        state = open_state(Tier.T3, qty="3", entry_qty="3", average="10", anchor="5")

        decision = self.decide(state, "15")

        self.assertEqual(decision.action, Action.CLOSE_T3_TO_TWO_THIRDS)
        self.assertEqual(decision.close_qty, D("2"))

    def test_t3_jumps_directly_to_full_exit_above_100_percent(self) -> None:
        state = open_state(Tier.T3, qty="3", entry_qty="3", average="10", anchor="5")

        decision = self.decide(state, "21")

        self.assertEqual(decision.action, Action.CLOSE_T3_FULL)
        self.assertEqual(decision.close_qty, D("3"))

    def test_t3_second_batch_only_closes_the_remaining_target_delta(self) -> None:
        state = open_state(
            Tier.T3,
            qty="2",
            entry_qty="3",
            average="10",
            anchor="5",
            completed_batches=1,
        )

        decision = self.decide(state, "15")

        self.assertEqual(decision.action, Action.CLOSE_T3_TO_TWO_THIRDS)
        self.assertEqual(decision.close_qty, D("1"))

    def test_t3_never_adds_another_tier(self) -> None:
        state = open_state(Tier.T3, qty="3", entry_qty="3", average="10", anchor="5")

        decision = self.decide(state, "2.5")

        self.assertEqual(decision.action, Action.HOLD)

    def test_t3_writeoff_closes_every_t3_batch_state(self) -> None:
        states = (
            open_state(Tier.T3, qty="3", entry_qty="3", average="10", anchor="5"),
            open_state(
                Tier.T3,
                qty="2",
                entry_qty="3",
                average="10",
                anchor="5",
                completed_batches=1,
            ),
            open_state(
                Tier.T3,
                qty="1",
                entry_qty="3",
                average="10",
                anchor="5",
                completed_batches=2,
            ),
        )

        for state in states:
            with self.subTest(sold=state.tier_sold_qty):
                decision = self.decide(state, "1.66")

                self.assertEqual(decision.action, Action.CLOSE_T3_FULL)
                self.assertEqual(decision.close_qty, state.qty)

    def test_idle_symbol_requires_admission_to_open(self) -> None:
        state = SymbolState(symbol="TESTUSDT")

        decision = self.decide(state, "100", admitted=True)

        self.assertEqual(decision.action, Action.OPEN_T1)
        self.assertEqual(decision.buy_notional, D("10.0"))

    def test_reopen_pending_requires_admission(self) -> None:
        state = SymbolState(symbol="TESTUSDT", lifecycle=Lifecycle.REOPEN_PENDING)

        decision = self.decide(state, "100")

        self.assertEqual(decision.action, Action.HOLD)

    def test_reopen_pending_opens_t1_when_admitted(self) -> None:
        state = SymbolState(symbol="TESTUSDT", lifecycle=Lifecycle.REOPEN_PENDING)

        decision = self.decide(
            state,
            "100",
            admitted=True,
        )

        self.assertEqual(decision.action, Action.OPEN_T1)
        self.assertEqual(decision.buy_notional, D("10.0"))


class ConfirmedExecutionTransitionTests(unittest.TestCase):
    def test_t2_fill_updates_weighted_average_and_uses_fill_price_as_anchor(self) -> None:
        t1 = apply_confirmed_execution(
            SymbolState(symbol="TESTUSDT"),
            ExecutionReceipt(Action.OPEN_T1, D("0.1"), D("10")),
        )

        t2 = apply_confirmed_execution(
            t1,
            ExecutionReceipt(Action.ADD_T2, D("0.4"), D("20")),
        )

        self.assertEqual(t2.tier, Tier.T2)
        self.assertEqual(t2.qty, D("0.5"))
        self.assertEqual(t2.avg_entry_price, D("60"))
        self.assertEqual(t2.tier_anchor_price, D("50"))
        self.assertEqual(t2.tier_entry_qty, D("0.5"))

    def test_partial_batch_does_not_complete_until_rounded_target_is_filled(self) -> None:
        state = open_state(Tier.T2, qty="0.5", entry_qty="0.5", average="60", anchor="50")

        partial = apply_confirmed_execution(
            state,
            ExecutionReceipt(
                Action.CLOSE_T2_BATCH1,
                D("0.2"),
                D("14"),
                target_cumulative_sold_qty=D("0.25"),
            ),
        )
        complete = apply_confirmed_execution(
            partial,
            ExecutionReceipt(
                Action.CLOSE_T2_BATCH1,
                D("0.05"),
                D("3.5"),
                target_cumulative_sold_qty=D("0.25"),
            ),
        )

        self.assertEqual(partial.completed_batches, 0)
        self.assertEqual(complete.completed_batches, 1)
        self.assertEqual(complete.tier_sold_qty, D("0.25"))
        self.assertEqual(complete.qty, D("0.25"))

    def test_full_close_moves_to_reopen_pending(self) -> None:
        state = open_state(Tier.T2, qty="0.25", entry_qty="0.5", average="60", anchor="50", completed_batches=1)

        flat = apply_confirmed_execution(
            state,
            ExecutionReceipt(
                Action.CLOSE_T2_FULL,
                D("0.25"),
                D("20"),
                target_cumulative_sold_qty=D("0.5"),
            ),
        )

        self.assertEqual(flat.lifecycle, Lifecycle.REOPEN_PENDING)
        self.assertEqual(flat.tier, Tier.NONE)
        self.assertEqual(flat.qty, D("0"))

    def test_sell_fill_cannot_exceed_known_long_quantity(self) -> None:
        state = open_state(Tier.T1, qty="0.1", entry_qty="0.1", average="100", anchor="100")

        with self.assertRaises(InvalidExecution):
            apply_confirmed_execution(state, ExecutionReceipt(Action.CLOSE_T1, D("0.2"), D("22")))


if __name__ == "__main__":
    unittest.main()
