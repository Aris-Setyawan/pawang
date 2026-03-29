"""Rate Limiter — per-user request throttling."""

import time
from collections import defaultdict
from core.logger import log


class RateLimiter:
    """Simple sliding window rate limiter per user."""

    def __init__(self, max_requests: int = 20, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)

    def check(self, user_id: str) -> tuple[bool, int]:
        """Check if user is within rate limit.

        Returns (allowed, remaining_requests).
        """
        now = time.time()
        cutoff = now - self.window

        # Clean old entries
        self._requests[user_id] = [
            t for t in self._requests[user_id] if t > cutoff
        ]

        count = len(self._requests[user_id])
        remaining = self.max_requests - count

        if count >= self.max_requests:
            return False, 0

        self._requests[user_id].append(now)
        return True, remaining - 1

    def get_wait_time(self, user_id: str) -> float:
        """Get seconds until next request is allowed."""
        if not self._requests[user_id]:
            return 0
        oldest = self._requests[user_id][0]
        wait = (oldest + self.window) - time.time()
        return max(0, wait)
