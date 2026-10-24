import asyncio
import json
import unittest
from urllib.parse import parse_qs, urlsplit

import httpx

from pnl_trader.adapters.notifications import WebhookNotificationSender
from pnl_trader.application.notification_models import OutboxNotification


class WebhookNotificationSenderTests(unittest.TestCase):
    def test_feishu_sends_static_interactive_card_with_signature(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"code": 0})

        async def run() -> None:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                sender = WebhookNotificationSender(
                    client=client,
                    channel="feishu",
                    webhook_url="https://example.test/feishu-hook",
                    signing_secret="signing-secret",
                )
                await sender.send(notification("feishu"))

        asyncio.run(run())
        self.assertEqual(len(requests), 1)
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["msg_type"], "interactive")
        self.assertEqual(payload["card"]["schema"], "2.0")
        self.assertTrue(payload["timestamp"])
        self.assertTrue(payload["sign"])
        self.assertIn("事件编号：event-stable", payload["card"]["body"]["elements"][0]["content"])

    def test_dingtalk_sends_markdown_and_signed_query_parameters(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

        async def run() -> None:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                sender = WebhookNotificationSender(
                    client=client,
                    channel="dingtalk",
                    webhook_url="https://example.test/robot?access_token=opaque-token",
                    signing_secret="signing-secret",
                )
                await sender.send(notification("dingtalk"))

        asyncio.run(run())
        self.assertEqual(len(requests), 1)
        parsed_url = urlsplit(str(requests[0].url))
        query = parse_qs(parsed_url.query)
        self.assertEqual(query["access_token"], ["opaque-token"])
        self.assertTrue(query["timestamp"][0].isdigit())
        self.assertTrue(query["sign"][0])
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["msgtype"], "markdown")
        self.assertIn("事件编号：event-stable", payload["markdown"]["text"])

    def test_telegram_uses_send_message_api_with_plain_text(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        async def run() -> None:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                sender = WebhookNotificationSender(
                    client=client,
                    channel="telegram",
                    telegram_bot_token="123:bot-token",
                    telegram_chat_id="-100123",
                )
                await sender.send(notification("telegram"))

        asyncio.run(run())
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "api.telegram.org")
        self.assertTrue(requests[0].url.path.endswith("/bot123:bot-token/sendMessage"))
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["chat_id"], "-100123")
        self.assertEqual(payload["text"].splitlines()[0], "通知标题")
        self.assertIn("事件编号：event-stable", payload["text"])


def notification(channel: str) -> OutboxNotification:
    return OutboxNotification(
        event_id="event-stable",
        channel=channel,
        dedupe_key="dedupe-key",
        event_type="take_profit",
        payload_json=json.dumps(
            {"title": "通知标题", "body": "正文", "priority": 0},
            ensure_ascii=False,
        ),
        priority=0,
        attempts=0,
        next_attempt_at_ms=0,
    )


if __name__ == "__main__":
    unittest.main()
