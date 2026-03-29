"""Task Manager — tracks running tasks, supports pause/resume/cancel."""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TaskState(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Intent(str, Enum):
    STOP = "stop"           # Hentikan task
    INFO = "info"           # Side question, jangan stop task
    MODIFY = "modify"       # Ubah task yang sedang jalan
    NEW_TASK = "new_task"   # Task baru, queue atau ganti
    CONTINUE = "continue"  # Lanjutkan task yang di-pause


@dataclass
class Task:
    id: str
    user_id: str
    agent_id: str
    prompt: str
    state: TaskState = TaskState.RUNNING
    partial_response: str = ""
    provider: str = ""
    model: str = ""
    created_at: float = field(default_factory=time.time)
    paused_at: Optional[float] = None
    _cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def is_active(self) -> bool:
        return self.state in (TaskState.RUNNING, TaskState.PAUSED)

    def request_cancel(self):
        """Signal task to stop gracefully."""
        self._cancel_event.set()

    @property
    def should_cancel(self) -> bool:
        return self._cancel_event.is_set()

    def pause(self):
        self.state = TaskState.PAUSED
        self.paused_at = time.time()

    def resume(self):
        self.state = TaskState.RUNNING
        self.paused_at = None
        self._cancel_event.clear()


class TaskManager:
    """Manages active tasks per user."""

    def __init__(self):
        self._tasks: dict[str, Task] = {}  # user_id -> active task
        self._history: list[Task] = []

    def get_active_task(self, user_id: str) -> Optional[Task]:
        task = self._tasks.get(user_id)
        if task and task.is_active:
            return task
        return None

    def create_task(self, user_id: str, agent_id: str, prompt: str,
                    provider: str = "", model: str = "") -> Task:
        # Cancel existing task if any
        existing = self.get_active_task(user_id)
        if existing:
            existing.request_cancel()
            existing.state = TaskState.CANCELLED
            self._history.append(existing)

        task = Task(
            id=f"task_{int(time.time())}_{user_id}",
            user_id=user_id,
            agent_id=agent_id,
            prompt=prompt,
            provider=provider,
            model=model,
        )
        self._tasks[user_id] = task
        return task

    def complete_task(self, user_id: str, response: str = ""):
        task = self._tasks.pop(user_id, None)
        if task:
            task.state = TaskState.COMPLETED
            task.partial_response = response
            self._history.append(task)
            self._trim_history()

    def fail_task(self, user_id: str, error: str = ""):
        task = self._tasks.pop(user_id, None)
        if task:
            task.state = TaskState.FAILED
            task.partial_response = error
            self._history.append(task)
            self._trim_history()

    def _trim_history(self, max_size: int = 100):
        """Keep history bounded to prevent memory leak."""
        if len(self._history) > max_size:
            self._history = self._history[-max_size:]

    def pause_task(self, user_id: str) -> Optional[Task]:
        task = self.get_active_task(user_id)
        if task and task.state == TaskState.RUNNING:
            task.pause()
            task.request_cancel()  # signal streaming to stop
            return task
        return None

    def get_paused_task(self, user_id: str) -> Optional[Task]:
        task = self._tasks.get(user_id)
        if task and task.state == TaskState.PAUSED:
            return task
        return None

    def list_active(self) -> list[Task]:
        return [t for t in self._tasks.values() if t.is_active]
