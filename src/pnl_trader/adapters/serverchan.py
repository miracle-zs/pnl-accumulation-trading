"""Server酱 push adapter for review failure alerts."""

from __future__ import annotations

import httpx


class ServerChanNotificationError(RuntimeError):
    """Server酱 rejected a notification or could not be reached."""


class ServerChanNotifier:
    def __init__(self, *, client: httpx.AsyncClient, sendkey: str) -> None:
        if not sendkey or not sendkey.isascii() or not sendkey.isalnum():
            raise ValueError("Server酱 SendKey must contain only letters and digits")
        self._client = client
        self._url = self._send_url(sendkey)

    async def send(self, *, title: str, body: str) -> None:
        try:
            response = await self._client.post(
                self._url,
                data={"title": title, "desp": body},
            )
        except httpx.HTTPError as exc:
            raise ServerChanNotificationError(
                f"request failed ({type(exc).__name__})"
            ) from None

        if response.status_code >= 400:
            raise ServerChanNotificationError(f"HTTP {response.status_code}")

        try:
            result = response.json()
        except ValueError:
            raise ServerChanNotificationError("response was not valid JSON") from None
        if not isinstance(result, dict) or result.get("code") != 0:
            raise ServerChanNotificationError("API returned an unsuccessful result")

    @staticmethod
    def _send_url(sendkey: str) -> str:
        if sendkey.startswith("sctp"):
            user_id, separator, token = sendkey[4:].partition("t")
            if not separator or not user_id.isdigit() or not token:
                raise ValueError("Server酱³ SendKey must contain its numeric user ID")
            return f"https://{user_id}.push.ft07.com/send/{sendkey}.send"
        if sendkey.startswith("SCT"):
            return f"https://sctapi.ftqq.com/{sendkey}.send"
        raise ValueError("Server酱 SendKey must start with SCT or sctp")
