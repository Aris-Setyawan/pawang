"""DM Pairing — code-based user approval system.

Instead of static allowlists, users can pair via one-time codes.
Admin generates a code, user sends it to bot, gets approved.
"""

import secrets
import time
from dataclasses import dataclass, field
from core.logger import log

# Unambiguous alphabet (no 0/O, 1/I/l confusion)
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

CODE_EXPIRY = 3600  # 1 hour
MAX_PENDING = 5


@dataclass
class PairingCode:
    code: str
    created_at: float
    created_by: str = ""  # admin user_id


class PairingManager:
    """Manages one-time pairing codes for user approval."""

    def __init__(self):
        self._pending: dict[str, PairingCode] = {}  # code -> PairingCode
        self._approved: set[int] = set()  # approved user_ids
        self._failed_attempts: dict[int, int] = {}  # user_id -> count
        self.max_failures = 5
        self.lockout_duration = 600  # 10 minutes
        self._lockout_until: dict[int, float] = {}

    def generate_code(self, admin_id: str = "") -> str:
        """Generate a new pairing code."""
        # Clean expired codes
        now = time.time()
        self._pending = {
            k: v for k, v in self._pending.items()
            if now - v.created_at < CODE_EXPIRY
        }

        if len(self._pending) >= MAX_PENDING:
            # Remove oldest
            oldest = min(self._pending.values(), key=lambda x: x.created_at)
            del self._pending[oldest.code]

        code = "".join(secrets.choice(_ALPHABET) for _ in 8)
        self._pending[code] = PairingCode(code=code, created_at=now, created_by=admin_id)
        log.info(f"Pairing code generated: {code} (by {admin_id})")
        return code

    def try_pair(self, user_id: int, code: str) -> bool:
        """Try to pair a user with a code. Returns True if successful."""
        now = time.time()

        # Check lockout
        if user_id in self._lockout_until and now < self._lockout_until[user_id]:
            return False

        code = code.strip().upper()
        pc = self._pending.get(code)

        if not pc or now - pc.created_at > CODE_EXPIRY:
            # Failed attempt
            self._failed_attempts[user_id] = self._failed_attempts.get(user_id, 0) + 1
            if self._failed_attempts[user_id] >= self.max_failures:
                self._lockout_until[user_id] = now + self.lockout_duration
                log.warning(f"Pairing lockout: user {user_id}")
            return False

        # Success
        del self._pending[code]
        self._approved.add(user_id)
        self._failed_attempts.pop(user_id, None)
        log.info(f"User {user_id} paired successfully with code {code}")
        return True

    def is_approved(self, user_id: int) -> bool:
        return user_id in self._approved

    def revoke(self, user_id: int):
        self._approved.discard(user_id)

    def list_pending(self) -> list[dict]:
        now = time.time()
        return [
            {"code": pc.code, "expires_in": int(CODE_EXPIRY - (now - pc.created_at)),
             "created_by": pc.created_by}
            for pc in self._pending.values()
            if now - pc.created_at < CODE_EXPIRY
        ]
