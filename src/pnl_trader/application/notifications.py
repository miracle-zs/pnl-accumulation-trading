"""At-least-once delivery for the durable notification outbox."""

from __future__ import annotations

import json
import logging
import time
from typing import Callable, Protocol

from .notification_models import OutboxNotification


logger = logging.getLogger(__name__)


class NotificationOutboxPort(Protocol):
    def load_due_notifications(
        self, channel: str, *, now_ms: int, limit: int
    ) -> tuple[OutboxNotification, ...]: ...

    def mark_notification_delivered(self, event_id: str, *, sent_at_ms: int) -> None: ...

    def reschedule_notification(
        self,
        event_id: str,
        *,
        attempted_at_ms: int,
        next_attempt_at_ms: int,
        error_code: str,
    ) -> None: ...


class NotificationSenderPort(Protocol):
    async def send(self, notification: OutboxNotification) -> None: ...


class NotificationDispatcher:
    """Send due messages without holding a database transaction over HTTP."""

    def __init__(
        self,
        *,
        store: NotificationOutboxPort,
        sender: NotificationSenderPort,
        channel: str,
        clock_ms: Callable[[], int] | None = None,
        batch_size: int = 25,
    ) -> None:
        if not channel:
            raise ValueError("notification channel must not be empty")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self._store = store
        self._sender = sender
        self._channel = channel
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._batch_size = batch_size

    async def dispatch_due(self) -> tuple[int, int]:
        now_ms = self._clock_ms()
        notifications = self._store.load_due_notifications(
            self._channel,
            now_ms=now_ms,
            limit=self._batch_size,
        )
        sent = 0
        failed = 0
        for notification in notifications:
            try:
                json.loads(notification.payload_json)
                await self._sender.send(notification)
            except Exception as exc:
                failed += 1
                attempts = notification.attempts + 1
                delay_ms = min(5_000 * (2 ** min(attempts - 1, 13)), 21_600_000)
                attempted_at_ms = self._clock_ms()
                self._store.reschedule_notification(
                    notification.event_id,
                    attempted_at_ms=attempted_at_ms,
                    next_attempt_at_ms=attempted_at_ms + delay_ms,
                    error_code=type(exc).__name__,
                )
                # Provider exceptions can contain a webhook URL or bot token.
                logger.warning(
                    "notification delivery failed: event_id=%s event_type=%s error_type=%s attempts=%s retry_in_ms=%s",
                    notification.event_id,
                    notification.event_type,
                    type(exc).__name__,
                    attempts,
                    delay_ms,
                )
            else:
                sent_at_ms = self._clock_ms()
                self._store.mark_notification_delivered(
                    notification.event_id,
                    sent_at_ms=sent_at_ms,
                )
                sent += 1
        return sent, failed
