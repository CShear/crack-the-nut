"""APScheduler wiring for trading bots."""

from __future__ import annotations

import asyncio
import signal
from typing import Callable, Awaitable

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

logger = structlog.get_logger()


class SchedulerRunner:
    """Wraps APScheduler for async trading bot jobs.

    Usage::

        runner = SchedulerRunner(timezone="America/New_York")
        runner.add_interval("update_prices", fetch_prices, minutes=5)
        runner.add_cron("daily_report", send_report, hour=23, minute=59)
        await runner.run_forever()
    """

    def __init__(self, timezone: str = "UTC"):
        self._scheduler = AsyncIOScheduler(timezone=timezone)
        self._shutdown_event = asyncio.Event()

    def add_interval(
        self,
        job_id: str,
        func: Callable[..., Awaitable],
        *,
        seconds: int | None = None,
        minutes: int | None = None,
        hours: int | None = None,
        misfire_grace_time: int = 60,
        args: tuple = (),
        kwargs: dict | None = None,
    ) -> None:
        """Register a job that runs on a fixed interval."""
        trigger_kwargs = {}
        if seconds is not None:
            trigger_kwargs["seconds"] = seconds
        if minutes is not None:
            trigger_kwargs["minutes"] = minutes
        if hours is not None:
            trigger_kwargs["hours"] = hours

        self._scheduler.add_job(
            func,
            trigger=IntervalTrigger(**trigger_kwargs),
            id=job_id,
            name=job_id,
            misfire_grace_time=misfire_grace_time,
            args=args,
            kwargs=kwargs or {},
            replace_existing=True,
        )
        logger.info("job_registered", job_id=job_id, trigger="interval", **trigger_kwargs)

    def add_cron(
        self,
        job_id: str,
        func: Callable[..., Awaitable],
        *,
        hour: int | None = None,
        minute: int | None = None,
        day_of_week: str | None = None,
        misfire_grace_time: int = 300,
        args: tuple = (),
        kwargs: dict | None = None,
    ) -> None:
        """Register a job that runs on a cron schedule."""
        trigger_kwargs = {}
        if hour is not None:
            trigger_kwargs["hour"] = hour
        if minute is not None:
            trigger_kwargs["minute"] = minute
        if day_of_week is not None:
            trigger_kwargs["day_of_week"] = day_of_week

        self._scheduler.add_job(
            func,
            trigger=CronTrigger(**trigger_kwargs),
            id=job_id,
            name=job_id,
            misfire_grace_time=misfire_grace_time,
            args=args,
            kwargs=kwargs or {},
            replace_existing=True,
        )
        logger.info("job_registered", job_id=job_id, trigger="cron", **trigger_kwargs)

    async def run_forever(self) -> None:
        """Start the scheduler and block until shutdown signal."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_shutdown)

        self._scheduler.start()
        logger.info("scheduler_started", jobs=len(self._scheduler.get_jobs()))

        try:
            await self._shutdown_event.wait()
        finally:
            self._scheduler.shutdown(wait=False)
            logger.info("scheduler_stopped")

    def _handle_shutdown(self) -> None:
        logger.info("shutdown_signal_received")
        self._shutdown_event.set()
