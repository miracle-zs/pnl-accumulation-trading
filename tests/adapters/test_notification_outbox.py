from contextlib import closing
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import asyncio

from pnl_trader.adapters.sqlite import IntentStatus, OrderAttempt, OrderIntent, SQLiteStateStore
from pnl_trader.application.execution_models import RiskAuthorization
from pnl_trader.application.market_models import (
    InstrumentRules,
    MarkPriceSnapshot,
    MarketQuantityRules,
)
from pnl_trader.application.order_planning import MarketOrderPlan
from pnl_trader.application.risk import AccountRiskGate
from pnl_trader.application.risk_models import AccountRiskSnapshot
from pnl_trader.application.risk_models import AccountRiskPolicy
from pnl_trader.domain import Action, Decision, ExecutionReceipt, SymbolState, TradeFillRecord


D = Decimal


class NotificationOutboxStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "state.sqlite3"
        self.store = SQLiteStateStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_schema_migrates_to_v10_and_risk_alerts_are_episode_deduplicated(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            version = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
        self.assertEqual(version, 10)

        self.store.save_account_risk_snapshot(
            risk_snapshot(71, 1),
            notification_channel="telegram",
            max_initial_margin_utilization=D("0.70"),
        )
        self.store.save_account_risk_snapshot(
            risk_snapshot(75, 2),
            notification_channel="telegram",
            max_initial_margin_utilization=D("0.70"),
        )
        self.store.save_account_risk_snapshot(
            risk_snapshot(69, 3),
            notification_channel="telegram",
            max_initial_margin_utilization=D("0.70"),
        )
        self.store.save_account_risk_snapshot(
            risk_snapshot(72, 4),
            notification_channel="telegram",
            max_initial_margin_utilization=D("0.70"),
        )

        notifications = self.store.load_due_notifications(
            "telegram", now_ms=10, limit=10
        )
        self.assertEqual(len(notifications), 2)
        self.assertEqual(
            [item.event_type for item in notifications],
            ["risk_circuit_breaker", "risk_circuit_breaker"],
        )
        self.assertTrue(all(item.priority == 100 for item in notifications))
        self.assertIn("暂停新的开仓和补仓", notifications[0].payload_json)

    def test_risk_gate_snapshot_does_not_reset_an_active_alert(self) -> None:
        class SnapshotSource:
            def __init__(self) -> None:
                self.snapshots = [risk_snapshot(71, 1_000), risk_snapshot(75, 1_000)]

            async def get_account_risk_snapshot(self, account_id: str, symbol: str):
                return self.snapshots.pop(0)

        gate = AccountRiskGate(
            snapshots=SnapshotSource(),
            store=self.store,
            policy=AccountRiskPolicy(
                max_maintenance_margin_ratio=D("0.50"),
                reserve_buffer=D("0"),
                taker_fee_rate=D("0"),
                slippage_buffer_rate=D("0"),
            ),
            notification_channel="telegram",
            clock_ms=lambda: 1_000,
        )
        state = SymbolState(symbol="BTCUSDT")
        decision = Decision(
            action=Action.OPEN_T1,
            reason="test",
            buy_notional=D("10"),
        )
        plan = MarketOrderPlan(
            source_action=Action.OPEN_T1,
            action=Action.OPEN_T1,
            quantity=D("0.1"),
            requested_quantity=D("0.1"),
            estimated_notional=D("10"),
            target_cumulative_sold_qty=None,
            merged_to_full_close=False,
            reason="test",
        )
        rules = InstrumentRules(
            symbol="BTCUSDT",
            contract_type="PERPETUAL",
            status="TRADING",
            quote_asset="USDT",
            margin_asset="USDT",
            market_quantity=MarketQuantityRules(
                min_qty=D("0.001"),
                max_qty=D("100"),
                step_size=D("0.001"),
                step_origin=D("0"),
            ),
        )
        price = MarkPriceSnapshot("BTCUSDT", D("100"), 1_000)

        async def authorize() -> RiskAuthorization:
            return await gate.authorize_order(
                account_id="primary",
                state=state,
                decision=decision,
                plan=plan,
                price=price,
                rules=rules,
            )

        first = asyncio.run(authorize())
        second = asyncio.run(authorize())
        self.assertFalse(first.approved)
        self.assertFalse(second.approved)
        events = self.store.load_due_notifications("telegram", now_ms=1_000, limit=10)
        self.assertEqual(len(events), 1)

    def test_pending_notification_retry_survives_store_restart(self) -> None:
        self.store.save_account_risk_snapshot(
            risk_snapshot(71, 1_000),
            notification_channel="telegram",
        )
        event = self.store.load_due_notifications("telegram", now_ms=1_000, limit=1)[0]
        self.store.reschedule_notification(
            event.event_id,
            attempted_at_ms=1_000,
            next_attempt_at_ms=6_000,
            error_code="HTTPStatusError",
        )
        self.store.close()
        self.store = SQLiteStateStore(self.db_path)

        self.assertEqual(
            self.store.load_due_notifications("telegram", now_ms=5_999, limit=1),
            (),
        )
        recovered = self.store.load_due_notifications(
            "telegram", now_ms=6_000, limit=1
        )[0]
        self.assertEqual(recovered.event_id, event.event_id)
        self.assertEqual(recovered.attempts, 1)
        self.store.mark_notification_delivered(event.event_id, sent_at_ms=6_001)
        self.assertEqual(
            self.store.load_due_notifications("telegram", now_ms=10_000, limit=1),
            (),
        )

    def test_take_profit_and_dca_events_share_execution_commit(self) -> None:
        self.store.ensure_idle_symbol_state("primary", "BTCUSDT", created_at_ms=1)
        self._apply_order(
            suffix="open",
            action=Action.OPEN_T1,
            qty=D("1"),
            quote=D("100"),
            applied_at_ms=1_000,
            buy_notional=D("100"),
            commission=D("0.10"),
        )
        self._apply_order(
            suffix="add-t2",
            action=Action.ADD_T2,
            qty=D("1"),
            quote=D("50"),
            applied_at_ms=2_000,
            buy_notional=D("50"),
            commission=D("0.05"),
        )

        state = self.store.load_symbol_state("primary", "BTCUSDT")
        self.assertEqual(state.avg_entry_price, D("75"))
        dca_event = self.store.load_due_notifications(
            "feishu", now_ms=2_000, limit=10
        )[0]
        self.assertEqual(dca_event.event_type, "dca_warning")
        self.assertIn("T2", dca_event.payload_json)
        self.assertIn('"avg_entry_price":"75"', dca_event.payload_json)

        self._apply_order(
            suffix="close-t2",
            action=Action.CLOSE_T2_FULL,
            qty=D("2"),
            quote=D("180"),
            applied_at_ms=61_000,
            target=D("2"),
            realized_pnl=D("30"),
            commission=D("0.18"),
        )
        events = self.store.load_due_notifications("feishu", now_ms=61_000, limit=10)
        take_profit = next(item for item in events if item.event_type == "take_profit")
        self.assertIn("+20.00%", take_profit.payload_json)
        self.assertIn("0天00小时01分00秒", take_profit.payload_json)
        self.assertIn('"USDT":"29.82"', take_profit.payload_json)

    def test_execution_and_notification_rollback_together_if_enqueue_fails(self) -> None:
        self.store.ensure_idle_symbol_state("primary", "BTCUSDT", created_at_ms=1)
        self._apply_order(
            suffix="open",
            action=Action.OPEN_T1,
            qty=D("1"),
            quote=D("100"),
            applied_at_ms=1_000,
            buy_notional=D("100"),
        )
        state_before = self.store.load_symbol_state("primary", "BTCUSDT")
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """CREATE TRIGGER fail_notification_enqueue
                   BEFORE INSERT ON notification_outbox
                   BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END"""
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self._apply_order(
                suffix="add-t2-failure",
                action=Action.ADD_T2,
                qty=D("1"),
                quote=D("50"),
                applied_at_ms=2_000,
                buy_notional=D("50"),
            )

        self.assertEqual(
            self.store.load_symbol_state("primary", "BTCUSDT"),
            state_before,
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            receipt_count = connection.execute(
                "SELECT COUNT(*) FROM execution_receipts WHERE receipt_id = ?",
                ("receipt-add-t2-failure",),
            ).fetchone()[0]
            fill_count = connection.execute(
                "SELECT COUNT(*) FROM trade_fills WHERE exchange_trade_id = ?",
                ("trade-add-t2-failure",),
            ).fetchone()[0]
        self.assertEqual(receipt_count, 0)
        self.assertEqual(fill_count, 0)

    def test_daily_report_notification_is_enqueued_once_with_revision_metadata(self) -> None:
        report = {"status": "PROVISIONAL", "metric": "first"}
        report_json = json.dumps(report, sort_keys=True, separators=(",", ":"))
        report_hash = hashlib.sha256(report_json.encode("utf-8")).hexdigest()
        notification_json = json.dumps(
            {"title": "每日账户晨报", "body": "净利润：USDT 1", "data": {}},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        result = self._persist_report(report_hash, report_json, notification_json)
        self.assertEqual(result, (1, True))
        self.assertEqual(
            self._persist_report(report_hash, report_json, notification_json),
            (1, False),
        )
        notifications = self.store.load_due_notifications(
            "dingtalk", now_ms=2_000, limit=10
        )
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].event_type, "daily_report")
        payload = json.loads(notifications[0].payload_json)
        self.assertEqual(payload["data"]["revision"], 1)

    def _persist_report(
        self, content_hash: str, payload_json: str, notification_payload_json: str
    ) -> tuple[int, bool]:
        return self.store.persist_daily_report_revision(
            account_id="primary",
            strategy_version="v1",
            period_start_ms=1_000,
            period_end_ms=2_000,
            status="PROVISIONAL",
            content_hash=content_hash,
            payload_json=payload_json,
            generated_at_ms=2_000,
            notification_channel="dingtalk",
            notify=True,
            notification_payload_json=notification_payload_json,
        )

    def _apply_order(
        self,
        *,
        suffix: str,
        action: Action,
        qty: Decimal,
        quote: Decimal,
        applied_at_ms: int,
        buy_notional: Decimal | None = None,
        target: Decimal | None = None,
        realized_pnl: Decimal = D("0"),
        commission: Decimal = D("0"),
    ) -> None:
        state = self.store.load_symbol_state("primary", "BTCUSDT")
        intent = OrderIntent(
            intent_id=f"intent-{suffix}",
            account_id="primary",
            symbol="BTCUSDT",
            cycle_id=state.cycle_id,
            action=action,
            business_key=f"business-{suffix}",
            expected_state_version=state.version,
            reserved_margin=D("1"),
            created_at_ms=applied_at_ms,
            target_cumulative_sold_qty=target,
            buy_notional=buy_notional,
        )
        self.store.create_order_intent(intent)
        client_order_id = f"client-{suffix}"
        self.store.create_order_attempt(
            OrderAttempt(
                attempt_id=f"attempt-{suffix}",
                intent_id=intent.intent_id,
                attempt_no=1,
                client_order_id=client_order_id,
                requested_qty=qty,
                sent_at_ms=applied_at_ms,
            )
        )
        fill = TradeFillRecord(
            symbol="BTCUSDT",
            exchange_trade_id=f"trade-{suffix}",
            client_order_id=client_order_id,
            side="BUY" if action in {Action.OPEN_T1, Action.ADD_T2, Action.ADD_T3} else "SELL",
            price=quote / qty,
            qty=qty,
            realized_pnl=realized_pnl,
            commission=commission,
            commission_asset="USDT",
            traded_at_ms=applied_at_ms,
            position_side="LONG",
            margin_asset="USDT",
        )
        receipt = ExecutionReceipt(
            action=action,
            filled_qty=qty,
            quote_qty=quote,
            target_cumulative_sold_qty=target,
        )
        self.store.commit_confirmed_execution(
            receipt_id=f"receipt-{suffix}",
            intent_id=intent.intent_id,
            receipt=receipt,
            fills=(fill,),
            expected_intent_status=IntentStatus.SUBMITTING,
            terminal_status=IntentStatus.FILLED,
            applied_at_ms=applied_at_ms,
            notification_channel="feishu",
        )


def risk_snapshot(initial_margin: int, fetched_at_ms: int) -> AccountRiskSnapshot:
    return AccountRiskSnapshot(
        account_id="primary",
        symbol="BTCUSDT",
        wallet_balance=D("100"),
        unrealized_pnl=D("0"),
        available_balance=D("20"),
        total_initial_margin=D(str(initial_margin)),
        total_maintenance_margin=D("5"),
        effective_leverage=D("10"),
        margin_type="CROSSED",
        multi_assets_mode=False,
        dual_side_position=False,
        fetched_at_ms=fetched_at_ms,
    )


if __name__ == "__main__":
    unittest.main()
