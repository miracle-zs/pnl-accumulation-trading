"""Tests for the Web Dashboard FastAPI app and Query Service."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx

from pnl_trader.web import DashboardQueryService, create_app


class DashboardWebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._temp_dir.name)
        self._db_path = self._data_dir / "state.sqlite3"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _init_sample_db(self) -> None:
        conn = sqlite3.connect(self._db_path)
        cur = conn.cursor()
        cur.executescript("""
            CREATE TABLE symbol_states (
                symbol TEXT NOT NULL,
                tier INTEGER NOT NULL,
                lifecycle TEXT NOT NULL,
                qty TEXT NOT NULL,
                avg_entry_price TEXT NOT NULL,
                tier_entry_qty TEXT NOT NULL,
                tier_anchor_price TEXT NOT NULL,
                completed_batches INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (symbol)
            );

            CREATE TABLE trade_fills (
                symbol TEXT NOT NULL,
                exchange_trade_id TEXT NOT NULL,
                side TEXT NOT NULL,
                price TEXT NOT NULL,
                qty TEXT NOT NULL,
                realized_pnl TEXT NOT NULL,
                commission TEXT NOT NULL,
                commission_asset TEXT NOT NULL,
                traded_at_ms INTEGER NOT NULL,
                intent_id TEXT NOT NULL,
                PRIMARY KEY (symbol, exchange_trade_id)
            );

            CREATE TABLE order_intents (
                intent_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                status TEXT NOT NULL
            );

            CREATE TABLE account_equity_snapshots (
                captured_at_ms INTEGER PRIMARY KEY,
                total_margin_balance TEXT NOT NULL,
                unrealized_pnl TEXT,
                available_balance TEXT,
                position_notional TEXT
            );

            INSERT INTO account_equity_snapshots VALUES
            (1700000000000, '1000', '-25', '600', '2000');

            INSERT INTO symbol_states VALUES
            ('BTCUSDT', 1, 'OPEN', '0.001', '60000.0', '0.001', '60000.0', 0, 1700000000000),
            ('ETHUSDT', 2, 'OPEN', '0.05', '3000.0', '0.05', '3000.0', 0, 1700000000000),
            ('SOLUSDT', 2, 'OPEN', '0.2', '150.0', '0.4', '150.0', 1, 1700000000000),
            ('DOGEUSDT', 0, 'IDLE', '0', '0', '0', '0', 0, 1700000000000);

            INSERT INTO trade_fills VALUES
            ('BTCUSDT', 't-1', 'SELL', '66000.0', '0.001', '6.0', '0.033', 'USDT', 1700000000000, 'intent-1');

        """)
        conn.commit()
        conn.close()

    async def test_dashboard_routes_return_valid_data(self) -> None:
        self._init_sample_db()
        app = create_app(data_dir=self._data_dir)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            # 1. Test root dashboard HTML
            resp = await client.get("/")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("PNL Periodic Review Trader", resp.text)

            # 2. Test overview API
            resp = await client.get("/api/overview")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["status"], "LOCAL_LEDGER_ONLY")
            self.assertAlmostEqual(data["realized_cash"], 5.97)
            self.assertEqual(data["realized_gross"], 6.0)
            self.assertEqual(data["fill_count"], 1)
            self.assertAlmostEqual(data["total_commission"], 0.03)
            self.assertEqual(data["active_positions_count"], 3)
            self.assertEqual(data["pending_close_fill_count"], 0)

            with patch("pnl_trader.web.service.time.time_ns", return_value=1700000000000 * 1_000_000):
                resp = await client.get("/api/equity?period=24h")
                latest = resp.json()["latest"]
                self.assertEqual(latest["leverage"], 2.0)
                self.assertEqual(latest["unrealized_pnl"], -25.0)
                self.assertEqual(latest["available_balance"], 600.0)
                resp = await client.get("/api/analytics")
                analytics = resp.json()
                self.assertEqual(analytics["today"]["fill_count"], 1)
                self.assertAlmostEqual(analytics["today"]["realized_net"], 5.97)
                self.assertEqual(analytics["symbols"][0]["symbol"], "BTCUSDT")

                with closing(sqlite3.connect(self._db_path)) as connection, connection:
                    connection.execute(
                        "INSERT INTO account_equity_snapshots VALUES (?, ?, ?, ?, ?)",
                        (1700000000001, "0", "-1", "0", "200"),
                    )
                resp = await client.get("/api/equity?period=24h")
                self.assertIsNone(resp.json()["latest"]["leverage"])

            # 3. Test tier distribution API
            resp = await client.get("/api/tier-distribution")
            self.assertEqual(resp.status_code, 200)
            td = resp.json()
            self.assertEqual(td["t1"], 1)
            self.assertEqual(td["t2"], 1)
            self.assertEqual(td["t21"], 1)
            self.assertEqual(td["total"], 3)

            # 4. Test positions API
            resp = await client.get("/api/positions")
            self.assertEqual(resp.status_code, 200)
            positions = resp.json()
            self.assertEqual(len(positions), 3)
            syms = [p["symbol"] for p in positions]
            self.assertIn("BTCUSDT", syms)
            self.assertIn("ETHUSDT", syms)
            self.assertIn("SOLUSDT", syms)

            # 5. Test fills API
            resp = await client.get("/api/fills")
            self.assertEqual(resp.status_code, 200)
            fills = resp.json()
            self.assertEqual(len(fills), 1)
            self.assertEqual(fills[0]["symbol"], "BTCUSDT")
            self.assertEqual(fills[0]["realized_pnl"], 6.0)

    async def test_today_net_is_unavailable_when_fees_are_not_usdt(self) -> None:
        self._init_sample_db()
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.execute(
                "INSERT INTO trade_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("ETHUSDT", "t-2", "BUY", "100", "1", "0", "0.01", "BNB", 1700000000000, "intent-2"),
            )
        app = create_app(data_dir=self._data_dir)
        with patch("pnl_trader.web.service.time.time_ns", return_value=1700000000000 * 1_000_000):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/analytics")
        self.assertIsNone(resp.json()["today"]["realized_net"])
        self.assertEqual(resp.json()["today"]["other_fee_count"], 1)

    async def test_missing_ledger_does_not_report_zero_metrics(self) -> None:
        app = create_app(data_dir=self._data_dir)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            overview = (await client.get("/api/overview")).json()
        self.assertEqual(overview["status"], "WAITING_DATA")
        self.assertIsNone(overview["realized_cash"])
        self.assertIsNone(overview["fill_count"])
        self.assertIsNone(overview["active_positions_count"])

    async def test_recent_fills_are_limited_separately_by_side(self) -> None:
        self._init_sample_db()
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.executemany(
                "INSERT INTO trade_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("ETHUSDT", f"buy-{i}", "BUY", "100", "1", "0", "0", "USDT", 1700000000001 + i, f"buy-{i}")
                    for i in range(13)
                ],
            )
        app = create_app(data_dir=self._data_dir)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            buys = (await client.get("/api/fills?limit=12&side=BUY")).json()
            sells = (await client.get("/api/fills?limit=12&side=SELL")).json()
            invalid = await client.get("/api/fills?side=INVALID")
        self.assertEqual(len(buys), 12)
        self.assertEqual(buys[0]["traded_at_ms"], 1700000000013)
        self.assertTrue(all(fill["side"] == "BUY" for fill in buys))
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["symbol"], "BTCUSDT")
        self.assertEqual(invalid.status_code, 422)

    async def test_large_dashboard_responses_support_gzip(self) -> None:
        self._init_sample_db()
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.executemany(
                "INSERT INTO symbol_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(f"TEST{i}USDT", 1, "OPEN", "1", "10", "1", "10", 0, 1700000000000) for i in range(100)],
            )
        app = create_app(data_dir=self._data_dir)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            compressed = await client.get("/api/positions", headers={"Accept-Encoding": "gzip"})
            plain = await client.get("/api/positions", headers={"Accept-Encoding": "identity"})
        self.assertEqual(compressed.headers["content-encoding"], "gzip")
        self.assertNotIn("content-encoding", plain.headers)
        self.assertEqual(compressed.json(), plain.json())
        self.assertLess(int(compressed.headers["content-length"]), int(plain.headers["content-length"]))

    async def test_net_profit_waits_for_close_fills(self) -> None:
        self._init_sample_db()
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.execute("INSERT INTO order_intents VALUES (?, ?, ?)", ("close-2", "CLOSE_T1", "FILLED"))
        app = create_app(data_dir=self._data_dir)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            pending = (await client.get("/api/overview")).json()
            self.assertEqual(pending["pending_close_fill_count"], 1)
            self.assertIsNone(pending["realized_cash"])
            with closing(sqlite3.connect(self._db_path)) as connection, connection:
                connection.execute(
                    "INSERT INTO trade_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("ETHUSDT", "t-2", "SELL", "100", "1", "2", "0", "USDT", 1700000000001, "close-2"),
                )
            complete = (await client.get("/api/overview")).json()
        self.assertEqual(complete["pending_close_fill_count"], 0)
        self.assertAlmostEqual(complete["realized_cash"], 7.97)

    async def test_equity_snapshot_marks_the_current_review_slot(self) -> None:
        self._init_sample_db()
        snapshot_ms = 1700000000000
        next_slot_ms = snapshot_ms - snapshot_ms % 900_000 + 900_000
        app = create_app(data_dir=self._data_dir)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with patch("pnl_trader.web.service.time.time_ns", return_value=(next_slot_ms - 1) * 1_000_000):
                current = (await client.get("/api/equity?period=24h")).json()
            with patch("pnl_trader.web.service.time.time_ns", return_value=next_slot_ms * 1_000_000):
                stale = (await client.get("/api/equity?period=24h")).json()
        self.assertTrue(current["latest"]["is_current_slot"])
        self.assertFalse(stale["latest"]["is_current_slot"])

    async def test_pending_full_close_is_shown_as_an_active_position(self) -> None:
        self._init_sample_db()
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.execute(
                "INSERT INTO symbol_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("ADAUSDT", 3, "CLOSING", "0.6", "10", "1", "10", 0, 1700000000001),
            )
        app = create_app(data_dir=self._data_dir)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            overview = (await client.get("/api/overview")).json()
            distribution = (await client.get("/api/tier-distribution")).json()
            positions = (await client.get("/api/positions")).json()

        self.assertEqual(overview["active_positions_count"], 4)
        self.assertEqual(distribution["total"], 4)
        self.assertEqual(distribution["t3"], 1)
        closing_position = next(position for position in positions if position["symbol"] == "ADAUSDT")
        self.assertIn("待全平", closing_position["tier_label"])
        self.assertIsNone(closing_position["tp_price"])
        self.assertIsNone(closing_position["dca_price"])
        self.assertIsNone(closing_position["writeoff_price"])

    async def test_today_uses_beijing_midnight(self) -> None:
        self._init_sample_db()
        beijing_midnight_ms = 1699977600000
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.executemany(
                "INSERT INTO trade_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("ETHUSDT", "before", "SELL", "100", "1", "3", "0", "USDT", beijing_midnight_ms - 1, "before"),
                    ("ETHUSDT", "after", "SELL", "100", "1", "2", "0", "USDT", beijing_midnight_ms, "after"),
                ],
            )
        app = create_app(data_dir=self._data_dir)
        with patch("pnl_trader.web.service.time.time_ns", return_value=1700000000000 * 1_000_000):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/api/analytics")
        self.assertEqual(response.json()["today"]["fill_count"], 2)
        self.assertEqual(response.json()["today"]["realized_net"], 7.97)


if __name__ == "__main__":
    unittest.main()
