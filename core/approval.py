"""Command Approval — interactive DM confirmation for dangerous commands.

When a dangerous command is detected, instead of blocking outright,
sends a DM to admin for approval. The command is held pending until
approved or denied (or timeout).
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Callable

from core.logger import log


@dataclass
class PendingApproval:
    command: str
    reason: str
    user_id: str
    agent_id: str
    created_at: float
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())
    approval_id: str = ""


class ApprovalManager:
    """Manages pending command approvals."""

    TIMEOUT = 120  # seconds to wait for approval

    def __init__(self):
        self._pending: dict[str, PendingApproval] = {}
        self._counter = 0
        self._notify: Optional[Callable] = None  # async func to send DM

    def set_notify(self, func: Callable):
        """Set the notification function for sending approval requests."""
        self._notify = func

    async def request_approval(self, command: str, reason: str,
                               user_id: str, agent_id: str) -> bool:
        """Request approval for a dangerous command.

        Returns True if approved, False if denied or timed out.
        Sends a DM to admin with approve/deny buttons.
        """
        self._counter += 1
        approval_id = f"apr_{self._counter}"

        pending = PendingApproval(
            command=command,
            reason=reason,
            user_id=user_id,
            agent_id=agent_id,
            created_at=time.time(),
            approval_id=approval_id,
        )
        self._pending[approval_id] = pending

        # Notify admin
        if self._notify:
            try:
                await self._notify(
                    approval_id=approval_id,
                    command=command,
                    reason=reason,
                    user_id=user_id,
                    agent_id=agent_id,
                )
            except Exception as e:
                log.error(f"Approval notify failed: {e}")
                self._pending.pop(approval_id, None)
                return False

        # Wait for approval (with timeout)
        try:
            result = await asyncio.wait_for(pending.future, timeout=self.TIMEOUT)
            return result
        except asyncio.TimeoutError:
            log.info(f"Approval timeout for {approval_id}")
            return False
        finally:
            self._pending.pop(approval_id, None)

    def approve(self, approval_id: str) -> bool:
        """Approve a pending command."""
        pending = self._pending.get(approval_id)
        if not pending or pending.future.done():
            return False
        pending.future.set_result(True)
        log.info(f"Command approved: {approval_id}")
        return True

    def deny(self, approval_id: str) -> bool:
        """Deny a pending command."""
        pending = self._pending.get(approval_id)
        if not pending or pending.future.done():
            return False
        pending.future.set_result(False)
        log.info(f"Command denied: {approval_id}")
        return True

    def list_pending(self) -> list[dict]:
        """List all pending approvals."""
        now = time.time()
        return [
            {
                "id": p.approval_id,
                "command": p.command[:100],
                "reason": p.reason,
                "user_id": p.user_id,
                "agent_id": p.agent_id,
                "age_seconds": int(now - p.created_at),
            }
            for p in self._pending.values()
        ]


# Global instance
approval_manager = ApprovalManager()
