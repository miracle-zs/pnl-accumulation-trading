"""Static webhook payloads for Feishu, DingTalk, and Telegram."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ...application.notification_models import OutboxNotification


class WebhookNotificationSender:
    """Send a normalized outbox event through one configured messaging channel."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        channel: str,
        webhook_url: str | None = None,
        signing_secret: str | None = None,
        telegram_bot_token: str | None = None,
        telegram_chat_id: str | None = None,
    ) -> None:
        if channel not in {"feishu", "dingtalk", "telegram"}:
            raise ValueError("unsupported notification channel")
        if channel in {"feishu", "dingtalk"} and not webhook_url:
            raise ValueError("webhook_url is required for this notification channel")
        if channel == "telegram" and (not telegram_bot_token or not telegram_chat_id):
            raise ValueError("Telegram bot token and chat ID are required")
        self._client = client
        self._channel = channel
        self._webhook_url = webhook_url
        self._signing_secret = signing_secret
        self._telegram_bot_token = telegram_bot_token
        self._telegram_chat_id = telegram_chat_id

    async def send(self, notification: OutboxNotification) -> None:
        payload = json.loads(notification.payload_json)
        title = str(payload.get("title") or notification.event_type)
        body = str(payload.get("body") or "")
        priority = int(payload.get("priority", notification.priority))
        body = f"{body}\n\n事件编号：{notification.event_id}"
        if self._channel == "feishu":
            await self._send_feishu(title, body, priority)
        elif self._channel == "dingtalk":
            await self._send_dingtalk(title, body, priority)
        else:
            await self._send_telegram(title, body, priority)

    async def _send_feishu(self, title: str, body: str, priority: int) -> None:
        message: dict[str, Any] = {
            "msg_type": "interactive",
            "card": {
                "schema": "2.0",
                "header": {
                    "title": {"tag": "plain_text", "content": title[:100]},
                    "template": "red" if priority >= 100 else "blue",
                },
                "body": {"elements": [{"tag": "markdown", "content": body[:10_000]}]},
            },
        }
        if self._signing_secret:
            timestamp = str(int(time.time()))
            message["timestamp"] = timestamp
            message["sign"] = _feishu_signature(timestamp, self._signing_secret)
        response = await self._client.post(self._webhook_url, json=message)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("code", 0) != 0:
            raise RuntimeError("Feishu rejected the notification")

    async def _send_dingtalk(self, title: str, body: str, priority: int) -> None:
        url = self._webhook_url
        if self._signing_secret:
            url = _dingtalk_signed_url(url, self._signing_secret)
        prefix = "🚨 " if priority >= 100 else ""
        message = {
            "msgtype": "markdown",
            "markdown": {"title": f"{prefix}{title}"[:100], "text": f"### {prefix}{title}\n\n{body[:10_000]}"},
        }
        response = await self._client.post(url, json=message)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("errcode", 0) != 0:
            raise RuntimeError("DingTalk rejected the notification")

    async def _send_telegram(self, title: str, body: str, priority: int) -> None:
        marker = "🚨 " if priority >= 100 else ""
        text = f"{marker}{title}\n\n{body}"
        if len(text) > 4_096:
            text = text[:4_080] + "…"
        url = f"https://api.telegram.org/bot{self._telegram_bot_token}/sendMessage"
        response = await self._client.post(
            url,
            json={"chat_id": self._telegram_chat_id, "text": text},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("Telegram rejected the notification")


def _feishu_signature(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(b"", string_to_sign, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _dingtalk_signed_url(url: str, secret: str) -> str:
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    query.update({"timestamp": timestamp, "sign": base64.b64encode(digest).decode("ascii")})
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
