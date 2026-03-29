"""Memory Guard — scan memory writes for prompt injection patterns."""

import re
from core.logger import log

# Max memory content length
MAX_MEMORY_LENGTH = 500

# Patterns that indicate prompt injection attempts
_INJECTION_PATTERNS = [
    # Role switching
    re.compile(r"(you are now|act as|ignore previous|forget your|new instructions|override)", re.IGNORECASE),
    # System prompt manipulation
    re.compile(r"(system prompt|system message|<\|system\|>|<\|im_start\|>)", re.IGNORECASE),
    # Exfiltration attempts
    re.compile(r"(https?://\S+)", re.IGNORECASE),  # URLs in memory
    re.compile(r"(fetch|curl|wget|request\.get|httpx)", re.IGNORECASE),
    # Code injection
    re.compile(r"(eval\(|exec\(|import os|subprocess|__import__)", re.IGNORECASE),
    # Markdown/HTML injection for Telegram
    re.compile(r"(<script|<img|javascript:|onerror=)", re.IGNORECASE),
]


def scan_memory(content: str) -> tuple[bool, str]:
    """Scan memory content for injection patterns.

    Returns (is_safe, reason). If is_safe is False, reason explains why.
    """
    if not content or not content.strip():
        return False, "Empty content"

    if len(content) > MAX_MEMORY_LENGTH:
        return False, f"Content too long ({len(content)} > {MAX_MEMORY_LENGTH} chars)"

    for pattern in _INJECTION_PATTERNS:
        match = pattern.search(content)
        if match:
            log.warning(f"Memory injection blocked: matched '{match.group()}' in: {content[:100]}")
            return False, f"Blocked: suspicious pattern detected ({match.group()[:30]})"

    return True, ""
