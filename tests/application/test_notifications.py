import asyncio
from dataclasses import replace
import logging
import unittest

from pnl_trader.application.notification_models import OutboxNotification
from pnl_trader.application.notifications import NotificationDispatcher


class MemoryOutbox:
    def __init__(self, notification: OutboxNotification) -> None:
        self.notification = notification
        self.status = "PENDING"
        self.last_error: str | None = None

    def load_due_notifications(self, channel: str, *, now_ms: int, limit: int):
        if (
            self.status == "PENDING"
            and self.notification.channel == channel
            and self.notification.next_attempt_at_ms <= now_ms
        ):
            return (self.notification,)
        return ()

    def mark_notification_delivered(self, event_id: str, *, sent_at_ms: int) -> None:
        self.status = "SENT"

    def reschedule_notification(
        self,
        event_id: str,
        *,
        attempted_at_ms: int,
        next_attempt_at_ms: int,
        error_code: str,
    ) -> None:
        self.notification = replace(
            self.notification,
            attempts=self.notification.attempts + 1,
            next_attempt_at_ms=next_attempt_at_ms,
        )
        self.last_error = error_code


class FlakySender:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, notification: OutboxNotification) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("failed request https://secret.example/bot/secret-token")


class NotificationDispatcherTests(unittest.TestCase):
    def test_failure_is_retried_with_backoff_without_logging_exception_secrets(self) -> None:
        notification = OutboxNotification(
            event_id="notification:stable-event-id",
            channel="telegram",
            dedupe_key="event-1",
            event_type="risk_circuit_breaker",
            payload_json='{"title":"alert","body":"body"}',
            priority=100,
            attempts=0,
            next_attempt_at_ms=1_000,
        )
        store = MemoryOutbox(notification)
        sender = FlakySender()
        current_time = [1_000]
        dispatcher = NotificationDispatcher(
            store=store,
            sender=sender,
            channel="telegram",
            clock_ms=lambda: current_time[0],
        )

        with self.assertLogs("pnl_trader.application.notifications", logging.WARNING) as captured:
            sent, failed = asyncio.run(dispatcher.dispatch_due())
        self.assertEqual((sent, failed), (0, 1))
        self.assertEqual(store.notification.attempts, 1)
        self.assertEqual(store.notification.next_attempt_at_ms, 6_000)
        self.assertEqual(store.last_error, "RuntimeError")
        self.assertNotIn("secret.example", "\n".join(captured.output))
        self.assertNotIn("secret-token", "\n".join(captured.output))

        current_time[0] = 6_000
        self.assertEqual(asyncio.run(dispatcher.dispatch_due()), (1, 0))
        self.assertEqual(store.status, "SENT")
        self.assertEqual(sender.calls, 2)


if __name__ == "__main__":
    unittest.main()
