import asyncio
from unittest.mock import patch
import unittest

from pnl_trader.application.review_run import ReviewRunResult
from pnl_trader.application.scheduler import PeriodicReviewScheduler


class PeriodicReviewSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_is_scheduled_at_the_start_of_each_quarter_hour(self) -> None:
        scheduler = PeriodicReviewScheduler(review_runs=_ReviewRuns([]))
        stop_event = asyncio.Event()
        stop_event.set()
        with patch("pnl_trader.application.scheduler.AsyncIOScheduler") as scheduler_class:
            await scheduler.run_until_stopped(stop_event)

        options = scheduler_class.return_value.add_job.call_args.kwargs
        self.assertEqual(options["minute"], "0,15,30,45")
        self.assertEqual(options["second"], 0)

    async def test_quarter_hour_trigger_uses_the_preceding_slot_id(self) -> None:
        review_runs = _ReviewRuns([ReviewRunResult(candidate_count=0, result_counts=())])
        scheduler = PeriodicReviewScheduler(review_runs=review_runs)
        with patch("pnl_trader.application.scheduler.time.time_ns", return_value=900_000_000_000):
            await scheduler._run_review_slot()
        self.assertEqual(review_runs.slots, [0])

    async def test_failure_alert_is_sent_once_until_a_review_succeeds(self) -> None:
        review_runs = _ReviewRuns(
            [
                RuntimeError("position mismatch for BTCUSDT: local=1, exchange=0"),
                RuntimeError("position mismatch for BTCUSDT: local=1, exchange=0"),
                ReviewRunResult(candidate_count=0, result_counts=()),
                RuntimeError("position mismatch for BTCUSDT: local=1, exchange=0"),
            ]
        )
        notifier = _RecordingNotifier()
        scheduler = PeriodicReviewScheduler(
            review_runs=review_runs,
            notifier=notifier,
        )

        with self.assertLogs("pnl_trader.application.scheduler", level="ERROR"):
            with patch(
                "pnl_trader.application.scheduler.time.time_ns",
                return_value=900_005_000_000,
            ):
                await scheduler._run_review_slot()
                await scheduler._run_review_slot()
                await scheduler._run_review_slot()
                await scheduler._run_review_slot()

        self.assertEqual(len(notifier.messages), 2)
        title, body = notifier.messages[0]
        self.assertEqual(title, "15分钟交易巡检失败")
        self.assertIn("position mismatch for BTCUSDT", body)
        self.assertIn("后续时段仍会继续尝试", body)


class _ReviewRuns:
    def __init__(self, outcomes: list[ReviewRunResult | Exception]) -> None:
        self._outcomes = iter(outcomes)
        self.slots: list[int] = []

    async def run_slot(self, *, slot_start_ms: int) -> ReviewRunResult:
        self.slots.append(slot_start_ms)
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send(self, *, title: str, body: str) -> None:
        self.messages.append((title, body))


if __name__ == "__main__":
    unittest.main()
