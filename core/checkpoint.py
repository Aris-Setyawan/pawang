"""Checkpoint/Rollback — save and restore conversation state.

Stores conversation snapshots in DB so users can undo/rollback
to a previous state.
"""

import json
import time
from typing import Optional

from core.database import get_db
from core.logger import log


def save_checkpoint(session_key: str, user_id: str, messages: list[dict],
                    label: str = "") -> int:
    """Save a conversation checkpoint.

    Args:
        session_key: Session identifier
        user_id: User ID
        messages: List of message dicts [{role, content}, ...]
        label: Optional human-readable label

    Returns checkpoint ID.
    """
    db = get_db()
    now = time.time()
    data = json.dumps(messages, ensure_ascii=False)

    if not label:
        label = f"checkpoint_{int(now)}"

    with db._lock:
        db.conn.execute(
            "INSERT INTO checkpoints (session_key, user_id, label, messages_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_key, user_id, label, data, now),
        )
        db.conn.commit()
        row = db.conn.execute("SELECT last_insert_rowid()").fetchone()

    cp_id = row[0]
    log.info(f"Checkpoint saved: #{cp_id} '{label}' ({len(messages)} messages)")
    return cp_id


def list_checkpoints(session_key: str, user_id: str, limit: int = 10) -> list[dict]:
    """List available checkpoints for a session."""
    db = get_db()
    rows = db.conn.execute(
        "SELECT id, label, created_at, "
        "LENGTH(messages_json) as size "
        "FROM checkpoints WHERE session_key = ? AND user_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (session_key, user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def load_checkpoint(checkpoint_id: int, user_id: str) -> Optional[list[dict]]:
    """Load a checkpoint's messages.

    Returns list of message dicts or None if not found.
    """
    db = get_db()
    row = db.conn.execute(
        "SELECT messages_json FROM checkpoints WHERE id = ? AND user_id = ?",
        (checkpoint_id, user_id),
    ).fetchone()

    if not row:
        return None

    try:
        return json.loads(row["messages_json"])
    except (json.JSONDecodeError, TypeError):
        return None


def delete_checkpoint(checkpoint_id: int, user_id: str) -> bool:
    """Delete a checkpoint."""
    db = get_db()
    with db._lock:
        cur = db.conn.execute(
            "DELETE FROM checkpoints WHERE id = ? AND user_id = ?",
            (checkpoint_id, user_id),
        )
        db.conn.commit()
    return cur.rowcount > 0


def cleanup_old_checkpoints(max_per_session: int = 20):
    """Remove old checkpoints beyond the limit per session."""
    db = get_db()
    with db._lock:
        # Get sessions with too many checkpoints
        sessions = db.conn.execute(
            "SELECT session_key, user_id, COUNT(*) as cnt FROM checkpoints "
            "GROUP BY session_key, user_id HAVING cnt > ?",
            (max_per_session,),
        ).fetchall()

        for s in sessions:
            # Keep newest max_per_session, delete the rest
            db.conn.execute(
                "DELETE FROM checkpoints WHERE session_key = ? AND user_id = ? "
                "AND id NOT IN ("
                "  SELECT id FROM checkpoints WHERE session_key = ? AND user_id = ? "
                "  ORDER BY created_at DESC LIMIT ?"
                ")",
                (s["session_key"], s["user_id"],
                 s["session_key"], s["user_id"], max_per_session),
            )

        db.conn.commit()
