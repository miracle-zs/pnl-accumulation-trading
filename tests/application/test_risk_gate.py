from decimal import Decimal
import unittest

from pnl_trader.application import (
    AccountRiskGate,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    InstrumentRules,
    MaintenanceMarginBracket,
    MaintenanceMarginBracketSnapshot,
    MarkPriceSnapshot,
    MarketOrderPlanner,
    MarketQuantityRules,
)
from pnl_trader.domain import Action, Decision, SymbolState


D = Decimal


class _SnapshotSource:
    def __init__(self, snapshot: AccountRiskSnapshot) -> None:
        self.snapshot = snapshot

    async def get_account_risk_snapshot(self, account_id: str, symbol: str):
        return self.snapshot


class _BracketSource:
    async def get_maintenance_margin_bracket_snapshot(
        self,
        account_id: str,
        symbol: str,
    ) -> MaintenanceMarginBracketSnapshot:
        return MaintenanceMarginBracketSnapshot(
            symbol=symbol,
            brackets=(
                MaintenanceMarginBracket(
                    bracket_number=1,
                    notional_floor=D("0"),
                    notional_cap=D("1000000"),
                    maintenance_margin_rate=D("0.01"),
                    maintenance_amount=D("0"),
                ),
            ),
            notional_coefficient=D("1"),
            fetched_at_ms=1_000,
        )


class AccountRiskGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.state = SymbolState(symbol="TESTUSDT")
        self.price = MarkPriceSnapshot("TESTUSDT", D("100"), 1_000)
        self.rules = InstrumentRules(
            symbol="TESTUSDT",
            contract_type="PERPETUAL",
            status="TRADING",
            quote_asset="USDT",
            margin_asset="USDT",
            market_quantity=MarketQuantityRules(
                min_qty=D("0.001"),
                max_qty=D("1000"),
                step_size=D("0.001"),
                step_origin=D("0"),
            ),
            min_market_notional=D("5"),
        )
        self.decision = Decision(
            Action.OPEN_T1,
            "open initial position",
            buy_notional=D("10"),
        )
        self.plan = MarketOrderPlanner().plan(
            self.decision,
            self.state,
            self.rules,
            self.price,
        )

    def snapshot(
        self,
        *,
        fetched_at_ms: int = 1_000,
        available_balance: str = "100",
        total_maintenance_margin: str = "0",
        symbol_position_qty: str = "0",
        symbol_maintenance_margin: str = "0",
    ) -> AccountRiskSnapshot:
        return AccountRiskSnapshot(
            account_id="primary",
            symbol="TESTUSDT",
            wallet_balance=D("100"),
            unrealized_pnl=D("0"),
            available_balance=D(available_balance),
            total_initial_margin=D("0"),
            total_maintenance_margin=D(total_maintenance_margin),
            effective_leverage=D("10"),
            margin_type="CROSSED",
            multi_assets_mode=False,
            dual_side_position=False,
            fetched_at_ms=fetched_at_ms,
            symbol_position_qty=D(symbol_position_qty),
            symbol_maintenance_margin=D(symbol_maintenance_margin),
        )

    def make_gate(self, snapshot: AccountRiskSnapshot, now_ms: int = 1_000):
        return AccountRiskGate(
            snapshots=_SnapshotSource(snapshot),
            brackets=_BracketSource(),
            policy=AccountRiskPolicy(
                max_maintenance_margin_ratio=D("0.50"),
                reserve_buffer=D("10"),
                taker_fee_rate=D("0.0005"),
                slippage_buffer_rate=D("0.005"),
                max_initial_margin_utilization=D("0.70"),
                max_snapshot_age_ms=30_000,
                max_bracket_age_ms=86_400_000,
            ),
            clock_ms=lambda: now_ms,
        )

    async def authorize(self, gate: AccountRiskGate):
        return await gate.authorize_order(
            account_id="primary",
            state=self.state,
            decision=self.decision,
            plan=self.plan,
            price=self.price,
            rules=self.rules,
        )

    async def test_fresh_healthy_snapshot_authorizes_with_execution_buffer(self) -> None:
        gate = self.make_gate(self.snapshot())

        authorization = await self.authorize(gate)

        self.assertTrue(authorization.approved)
        self.assertEqual(authorization.reserved_margin, D("1.055"))
        self.assertEqual(authorization.max_active_reserved_margin, D("70.00"))

    async def test_stale_account_snapshot_fails_closed(self) -> None:
        gate = self.make_gate(self.snapshot(fetched_at_ms=0), now_ms=100_000)

        authorization = await self.authorize(gate)

        self.assertFalse(authorization.approved)
        self.assertIn("snapshot is stale", authorization.reason)
        self.assertEqual(authorization.reserved_margin, D("0"))

    async def test_reserve_buffer_is_excluded_from_spendable_order_capacity(self) -> None:
        gate = self.make_gate(self.snapshot(available_balance="10"))

        authorization = await self.authorize(gate)

        self.assertFalse(authorization.approved)
        self.assertIn("spendable account capacity", authorization.reason)
        self.assertEqual(authorization.reserved_margin, D("0"))

    async def test_projected_maintenance_ratio_over_limit_denies_increase(self) -> None:
        gate = self.make_gate(
            self.snapshot(
                total_maintenance_margin="49.9",
                symbol_maintenance_margin="0",
            ),
        )

        authorization = await self.authorize(gate)

        self.assertFalse(authorization.approved)
        self.assertIn("projected maintenance margin ratio", authorization.reason)
        self.assertEqual(authorization.reserved_margin, D("0"))

    async def test_exchange_position_mismatch_denies_increase(self) -> None:
        gate = self.make_gate(self.snapshot(symbol_position_qty="0.01"))

        authorization = await self.authorize(gate)

        self.assertFalse(authorization.approved)
        self.assertIn("position quantity changed", authorization.reason)
        self.assertEqual(authorization.reserved_margin, D("0"))


if __name__ == "__main__":
    unittest.main()
