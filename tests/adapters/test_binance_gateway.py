"""Binance REST contract tests using an in-memory HTTP transport."""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qsl
import unittest
from unittest.mock import AsyncMock, call, patch

import httpx

from pnl_trader.adapters.binance.gateway import BinanceFuturesGateway
from pnl_trader.application.execution_models import (
    ExchangeOrderStatus,
    MarketOrderRequest,
    SubmissionCertainty,
)


class BinanceGatewayTests(unittest.IsolatedAsyncioTestCase):
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
                base_url="https://binance.test",
                clock_ms=lambda: 1_700_000_000_000,
            )
            result = await gateway.submit_market_order(
                MarketOrderRequest(
                    symbol="BTCUSDT",
                    side="SELL",
                    quantity=Decimal("0.01"),
                    client_order_id="test-close-1",
                    reduce_only=True,
                )
            )

        self.assertEqual(result.certainty, SubmissionCertainty.ACCEPTED)
        self.assertEqual(result.order.status, ExchangeOrderStatus.FILLED)
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
                base_url="https://binance.test",
                clock_ms=lambda: 1_700_000_000_000,
            )
            result = await gateway.submit_market_order(
                MarketOrderRequest(
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity=Decimal("0.01"),
                    client_order_id="test-open-1",
                    reduce_only=False,
                )
            )

        self.assertEqual(result.certainty, SubmissionCertainty.UNKNOWN)
        self.assertEqual(len(requests), 1)

    async def test_read_retries_transport_server_and_rate_limit_failures(self) -> None:
        slot_start_ms = 0
        close_time_ms = slot_start_ms + 899_999
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
                json=[[slot_start_ms, "59000", "61000", "58000", "60000", "10", close_time_ms]],
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            gateway = BinanceFuturesGateway(
                client=client,
                api_key="test-api-key",
                api_secret="test-api-secret",
                base_url="https://binance.test",
            )
            with patch(
                "pnl_trader.adapters.binance.gateway.asyncio.sleep",
                new_callable=AsyncMock,
            ) as sleep:
                snapshot = await gateway.get_fifteen_minute_close("BTCUSDT", slot_start_ms)

        self.assertEqual(snapshot.close_price, Decimal("60000"))
        self.assertEqual(len(requests), 4)
        self.assertEqual(
            sleep.await_args_list,
            [call(1), call(2), call(9.0)],
        )

    async def test_fifteen_minute_close_requests_the_exact_closed_candle(self) -> None:
        slot_start_ms = 900_000
        expected_close_time_ms = slot_start_ms + 899_999
        requests: list[httpx.Request] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json=[
                    [
                        slot_start_ms,
                        "59000",
                        "61000",
                        "58000",
                        "60500.5",
                        "10",
                        expected_close_time_ms,
                    ]
                ],
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            gateway = BinanceFuturesGateway(
                client=client,
                api_key="test-api-key",
                api_secret="test-api-secret",
                base_url="https://binance.test",
            )
            snapshot = await gateway.get_fifteen_minute_close("BTCUSDT", slot_start_ms)

        self.assertEqual(snapshot.close_price, Decimal("60500.5"))
        self.assertEqual(snapshot.close_time_ms, expected_close_time_ms)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/fapi/v1/klines")
        self.assertEqual(requests[0].url.params["interval"], "15m")
        self.assertEqual(requests[0].url.params["startTime"], str(slot_start_ms))
        self.assertEqual(requests[0].url.params["endTime"], str(expected_close_time_ms))


if __name__ == "__main__":
    unittest.main()
