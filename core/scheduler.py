"""Scheduler — background cron-like tasks with Telegram notifications."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.logger import log


@dataclass
class ScheduledJob:
    """A scheduled background task."""
    name: str
    interval: int  # seconds
    func: Callable  # async callable
    enabled: bool = True
    last_run: float = 0.0
    run_count: int = 0
    last_error: str = ""


class Scheduler:
    """Simple asyncio-based scheduler for background tasks."""

    def __init__(self):
        self._jobs: dict[str, ScheduledJob] = {}
        self._task: Optional[asyncio.Task] = None
        self._notify_func: Optional[Callable] = None  # async func(text) -> send to Telegram

    def set_notify(self, func: Callable):
        """Set notification function (e.g. send Telegram message to admin)."""
        self._notify_func = func

    async def notify(self, text: str):
        """Send notification via configured channel."""
        if self._notify_func:
            try:
                await self._notify_func(text)
            except Exception as e:
                log.error(f"Scheduler notify error: {e}")

    def add_job(self, name: str, interval: int, func: Callable, enabled: bool = True):
        """Register a scheduled job."""
        if interval <= 0:
            log.warning(f"Scheduler: ignoring '{name}' with invalid interval={interval}")
            return
        self._jobs[name] = ScheduledJob(
            name=name, interval=interval, func=func, enabled=enabled,
            last_run=time.time(),  # respect interval on first run
        )
        log.info(f"Scheduler: registered '{name}' (every {interval}s)")

    def remove_job(self, name: str):
        self._jobs.pop(name, None)

    def get_jobs(self) -> list[dict]:
        """Get job status for API/panel."""
        result = []
        for j in self._jobs.values():
            result.append({
                "name": j.name,
                "interval": j.interval,
                "enabled": j.enabled,
                "last_run": j.last_run,
                "run_count": j.run_count,
                "last_error": j.last_error,
            })
        return result

    async def _run_loop(self):
        """Main scheduler loop — checks jobs every 10 seconds."""
        log.info("Scheduler started")
        while True:
            try:
                now = time.time()
                for job in self._jobs.values():
                    if not job.enabled:
                        continue
                    if now - job.last_run >= job.interval:
                        try:
                            await job.func()
                            job.run_count += 1
                            job.last_error = ""
                        except Exception as e:
                            job.last_error = str(e)[:200]
                            log.error(f"Scheduler job '{job.name}' failed: {e}")
                        job.last_run = now
            except Exception as e:
                log.error(f"Scheduler loop error: {e}")
            await asyncio.sleep(10)

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            log.info("Scheduler stopped")
