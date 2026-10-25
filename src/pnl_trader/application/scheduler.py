"""Schedule the 15-minute review loop."""

from __future__ import annotations

import asyncio
from datetime import UTC
import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .review_run import ReviewRunCoordinator


logger = logging.getLogger(__name__)


class PeriodicReviewScheduler:
    """Run one review after each 15-minute candle closes."""

    def __init__(
        self,
        *,
        review_runs: ReviewRunCoordinator,
    ) -> None:
        self._review_runs = review_runs

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        scheduler = AsyncIOScheduler(timezone=UTC)
        scheduler.add_job(
            self._run_review_slot,
            trigger="cron",
            minute="0,15,30,45",
            second=5,
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
        try:
            result = await self._review_runs.run_slot(
                slot_start_ms=slot_start_ms,
            )
            logger.info(
                "review finished: slot=%s candidates=%s counts=%s",
                slot_start_ms,
                result.candidate_count,
                dict(result.result_counts),
            )
        except Exception:
            logger.exception("review failed: slot=%s", slot_start_ms)
