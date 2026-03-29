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
        self._saved_states: dict[str, dict] = {}
        self._load_states()

    def _load_states(self):
        """Load persisted job states from DB."""
        try:
            from core.database import get_db
            self._saved_states = get_db().get_job_states()
            if self._saved_states:
                log.info(f"Scheduler: loaded {len(self._saved_states)} saved job states")
        except Exception as e:
            log.warning(f"Scheduler: could not load states: {e}")

    def _persist_job(self, job: ScheduledJob):
        """Save job state to DB after each run."""
        try:
            from core.database import get_db
            get_db().save_job_state(job.name, job.last_run, job.run_count,
                                    job.last_error, job.enabled)
        except Exception as e:
            log.warning(f"Scheduler: could not persist '{job.name}': {e}")

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
        """Register a scheduled job, restoring state from DB if available."""
        if interval <= 0:
            log.warning(f"Scheduler: ignoring '{name}' with invalid interval={interval}")
            return
        job = ScheduledJob(
            name=name, interval=interval, func=func, enabled=enabled,
            last_run=time.time(),  # default: respect interval on first run
        )
        # Restore persisted state
        saved = self._saved_states.get(name)
        if saved:
            job.last_run = saved.get("last_run", job.last_run)
            job.run_count = saved.get("run_count", 0)
            job.last_error = saved.get("last_error", "")
            if not saved.get("enabled", True):
                job.enabled = False
            log.info(f"Scheduler: restored '{name}' (runs={job.run_count})")
        self._jobs[name] = job
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
                        self._persist_job(job)
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
