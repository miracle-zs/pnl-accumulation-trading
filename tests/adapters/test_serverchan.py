from __future__ import annotations

from urllib.parse import parse_qs
import unittest

import httpx

from pnl_trader.adapters.serverchan import (
    ServerChanNotificationError,
    ServerChanNotifier,
)


class ServerChanNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_turbo_posts_title_and_body_as_form_data(self) -> None:
        requests: list[httpx.Request] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"code": 0})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            notifier = ServerChanNotifier(client=client, sendkey="SCTtestkey")
            await notifier.send(title="Review failed", body="Position mismatch")

        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            str(request.url),
            "https://sctapi.ftqq.com/SCTtestkey.send",
        )
        self.assertEqual(
            parse_qs(request.content.decode("ascii")),
            {"title": ["Review failed"], "desp": ["Position mismatch"]},
        )

    async def test_serverchan3_uses_sendkey_user_id_in_endpoint(self) -> None:
        requests: list[httpx.Request] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"code": 0})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            notifier = ServerChanNotifier(client=client, sendkey="sctp123tTestKey")
            await notifier.send(title="Review failed", body="Details")

        self.assertEqual(
            str(requests[0].url),
            "https://123.push.ft07.com/send/sctp123tTestKey.send",
        )

    async def test_nonzero_api_code_is_reported_as_delivery_failure(self) -> None:
        async def reject(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 40001})

        async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
            notifier = ServerChanNotifier(client=client, sendkey="SCTtestkey")
            with self.assertRaises(ServerChanNotificationError):
                await notifier.send(title="Review failed", body="Details")


if __name__ == "__main__":
    unittest.main()
