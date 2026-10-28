"""Binance REST contract tests using an in-memory HTTP transport."""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qsl
import unittest
from unittest.mock import AsyncMock, call, patch

import httpx

from pnl_trader.adapters.binance.gateway import BinanceFuturesGateway, BinanceProtocolError
from pnl_trader.application.execution_models import (
    ExchangeOrderStatus,
    MarketOrderRequest,
    SubmissionCertainty,
)


class BinanceGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_snapshot_uses_position_notionals_from_one_account_request(self) -> None:
        requests = []

        async def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={
                "totalMarginBalance": "1000", "totalUnrealizedProfit": "-25",
                "availableBalance": "600", "positions": [
                    {"notional": "1500"}, {"notional": "-500"},
                ],
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            gateway = BinanceFuturesGateway(client=client, api_key="test-key", api_secret="test-secret")
            snapshot = await gateway.get_account_snapshot()
        self.assertEqual(snapshot.equity, Decimal("1000"))
        self.assertEqual(snapshot.unrealized_pnl, Decimal("-25"))
        self.assertEqual(snapshot.available_balance, Decimal("600"))
        self.assertEqual(snapshot.position_notional, Decimal("2000"))
        self.assertEqual([request.url.path for request in requests], ["/fapi/v3/account"])

    async def test_account_snapshot_requires_positions(self) -> None:
        async def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "totalMarginBalance": "1000", "totalUnrealizedProfit": "0", "availableBalance": "600"
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            gateway = BinanceFuturesGateway(client=client, api_key="test-key", api_secret="test-secret")
            with self.assertRaises(BinanceProtocolError):
                await gateway.get_account_snapshot()

    async def test_market_order_is_signed_and_uses_reduce_only_market_request(self) -> None:
        requests: list[httpx.Request] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "orderId": 123,
                    "status": "FILLED",
                    "executedQty": "0.01",
                    "cumQuote": "600.00",
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            gateway = BinanceFuturesGateway(
                client=client,
                api_key="test-api-key",
                api_secret="test-api-secret",
                clock_ms=lambda: 1_700_000_000_000,
            )
            with self.assertLogs("pnl_trader.adapters.binance.gateway", level="INFO") as captured:
                result = await gateway.submit_market_order(
                    MarketOrderRequest(
                        symbol="BTCUSDT",
                        side="SELL",
                        quantity=Decimal("0.01"),
                        client_order_id="test-close-1",
                    )
                )

        self.assertEqual(result.certainty, SubmissionCertainty.ACCEPTED)
        self.assertEqual(result.order.status, ExchangeOrderStatus.FILLED)
        self.assertIn("symbol=BTCUSDT side=SELL quantity=0.01", captured.output[0])
        self.assertNotIn("test-api-key", captured.output[0])
        self.assertNotIn("test-api-secret", captured.output[0])
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/fapi/v1/order")
        self.assertEqual(request.headers["X-MBX-APIKEY"], "test-api-key")

        raw_query = request.url.query.decode("ascii")
        unsigned_query, signature = raw_query.rsplit("&signature=", 1)
        expected_signature = hmac.new(
            b"test-api-secret",
            unsigned_query.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(signature, expected_signature)
        parameters = dict(parse_qsl(unsigned_query))
        self.assertEqual(parameters["symbol"], "BTCUSDT")
        self.assertEqual(parameters["side"], "SELL")
        self.assertEqual(parameters["type"], "MARKET")
        self.assertEqual(parameters["quantity"], "0.01")
        self.assertEqual(parameters["reduceOnly"], "true")
        self.assertEqual(parameters["newClientOrderId"], "test-close-1")

    async def test_market_order_timeout_is_unknown_and_is_never_retried(self) -> None:
        requests: list[httpx.Request] = []

        async def timeout(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            raise httpx.ReadTimeout("response timed out", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            gateway = BinanceFuturesGateway(
                client=client,
                api_key="test-api-key",
                api_secret="test-api-secret",
                clock_ms=lambda: 1_700_000_000_000,
            )
            result = await gateway.submit_market_order(
                MarketOrderRequest(
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity=Decimal("0.01"),
                    client_order_id="test-open-1",
                )
            )

        self.assertEqual(result.certainty, SubmissionCertainty.UNKNOWN)
        self.assertEqual(len(requests), 1)

    async def test_read_retries_transport_server_and_rate_limit_failures(self) -> None:
        requests: list[httpx.Request] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                raise httpx.ReadTimeout("temporary timeout", request=request)
            if len(requests) == 2:
                return httpx.Response(503, json={"code": -1000, "msg": "temporary"})
            if len(requests) == 3:
                return httpx.Response(
                    429,
                    json={"code": -1003, "msg": "rate limited"},
                    headers={"Retry-After": "9"},
                )
            return httpx.Response(
                200,
                json=[{"symbol": "BTCUSDT", "price": "60000", "time": 1_700_000_000_000}],
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            gateway = BinanceFuturesGateway(
                client=client,
                api_key="test-api-key",
                api_secret="test-api-secret",
            )
            with patch(
                "pnl_trader.adapters.binance.gateway.asyncio.sleep",
                new_callable=AsyncMock,
            ) as sleep:
                prices = await gateway.get_latest_prices()

        self.assertEqual(prices, {"BTCUSDT": Decimal("60000")})
        self.assertEqual(len(requests), 4)
        self.assertEqual(
            sleep.await_args_list,
            [call(1), call(2), call(9.0)],
        )

    async def test_latest_price_returns_all_symbols_in_one_request(self) -> None:
        requests: list[httpx.Request] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json=[
                    {"symbol": "BTCUSDT", "price": "60500.5", "time": 1_700_000_000_000},
                    {"symbol": "ETHUSDT", "price": "3000.25", "time": 1_700_000_000_000},
                ],
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            gateway = BinanceFuturesGateway(
                client=client,
                api_key="test-api-key",
                api_secret="test-api-secret",
            )
            prices = await gateway.get_latest_prices()

        self.assertEqual(
            prices,
            {"BTCUSDT": Decimal("60500.5"), "ETHUSDT": Decimal("3000.25")},
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/fapi/v2/ticker/price")
        self.assertEqual(dict(requests[0].url.params), {})


if __name__ == "__main__":
    unittest.main()
