"""Schedule the 15-minute review loop."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .ports import AlertNotifierPort
from .review_run import ReviewRunCoordinator


logger = logging.getLogger(__name__)


class PeriodicReviewScheduler:
    """Run one review after each 15-minute candle closes."""

    def __init__(
        self,
        *,
        review_runs: ReviewRunCoordinator,
        notifier: AlertNotifierPort | None = None,
    ) -> None:
        self._review_runs = review_runs
        self._notifier = notifier
        self._failure_alert_sent = False

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        scheduler = AsyncIOScheduler(timezone=UTC)
        scheduler.add_job(
            self._run_review_slot,
            trigger="cron",
            minute="0,15,30,45",
            second=0,
            misfire_grace_time=60,
        )
        scheduler.start()
        logger.info("15-minute review scheduler started")
        try:
            await stop_event.wait()
        finally:
            scheduler.shutdown()
            logger.info("15-minute review scheduler stopped")

    async def _run_review_slot(self) -> None:
        now_ms = time.time_ns() // 1_000_000
        slot_start_ms = now_ms - now_ms % 900_000 - 900_000
        started_ns = time.monotonic_ns()
        try:
            result = await self._review_runs.run_slot(
                slot_start_ms=slot_start_ms,
            )
            logger.info(
                "review finished: slot=%s duration_ms=%.0f candidates=%s counts=%s",
                slot_start_ms,
                (time.monotonic_ns() - started_ns) / 1_000_000,
                result.candidate_count,
                dict(result.result_counts),
            )
            self._failure_alert_sent = False
        except Exception as exc:
            logger.exception(
                "review failed: slot=%s duration_ms=%.0f",
                slot_start_ms,
                (time.monotonic_ns() - started_ns) / 1_000_000,
            )
            if self._notifier is None or self._failure_alert_sent:
                return

            slot_time = datetime.fromtimestamp(slot_start_ms / 1_000, tz=UTC)
            exception_text = str(exc).replace("\n", " ")
            if exception_text.startswith((
                "position mismatch for ",
                "exchange position for ",
                "exchange activity for ",
                "short position detected for ",
            )):
                reason = exception_text[:500]
            else:
                reason = type(exc).__name__
            body = (
                f"巡检时段（UTC）：{slot_time.isoformat()}\n\n"
                "本轮已停止，后续时段仍会继续尝试。\n\n"
                f"异常：{reason}\n\n"
                "请查看交易服务日志。"
            )
            try:
                await self._notifier.send(title="15分钟交易巡检失败", body=body)
            except Exception as notify_error:
                logger.error(
                    "Server酱通知发送失败（%s）",
                    type(notify_error).__name__,
                )
            else:
                self._failure_alert_sent = True
