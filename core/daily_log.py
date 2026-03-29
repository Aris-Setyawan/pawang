"""Daily Memory Log — append conversation summaries to daily log files.

Writes to workspace/memory/YYYY-MM-DD.md for audit trail and history.
"""

import os
import time
from datetime import datetime
from pathlib import Path

from core.logger import log

LOG_DIR = Path(__file__).parent.parent / "workspace" / "memory"


def append_daily_log(agent_id: str, user_id: str, role: str, content: str):
    """Append a message summary to today's log file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    today = datetime.utcnow().strftime("%Y-%m-%d")
    log_path = LOG_DIR / f"{today}.md"

    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    # Truncate long messages for the log
    summary = content[:300] + "..." if len(content) > 300 else content
    summary = summary.replace("\n", " ")

    entry = f"- [{timestamp}] {agent_id}/{user_id} ({role}): {summary}\n"

    try:
        with open(log_path, "a", encoding="utf-8") as f:
            # Write header if new file
            if f.tell() == 0 or os.path.getsize(log_path) == 0:
                f.write(f"# Pawang Daily Log — {today}\n\n")
            f.write(entry)
    except Exception as e:
        log.error(f"Daily log write error: {e}")
