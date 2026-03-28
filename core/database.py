"""SQLite WAL Database — persistent sessions, message history, usage tracking."""

import sqlite3
import json
import time
from pathlib import Path
from typing import Optional

from core.logger import log

DB_PATH = Path(__file__).parent.parent / "data" / "pawang.db"


class Database:
    """SQLite WAL database for persistent storage."""

    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._setup()

    def _setup(self):
        """Create tables and enable WAL mode."""
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                model TEXT DEFAULT '',
                provider TEXT DEFAULT '',
                tokens_est INTEGER DEFAULT 0,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                session_key TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                active_model TEXT DEFAULT '',
                active_provider TEXT DEFAULT '',
                message_count INTEGER DEFAULT 0,
                last_active REAL NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                latency_ms REAL DEFAULT 0,
                success INTEGER DEFAULT 1,
                error TEXT DEFAULT '',
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_key);
            CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_usage_created ON usage(created_at);
            CREATE INDEX IF NOT EXISTS idx_usage_provider ON usage(provider);
        """)
        self.conn.commit()
        log.info(f"Database initialized: {self.path}")

    def save_message(self, session_key: str, agent_id: str, user_id: str,
                     role: str, content: str, model: str = "", provider: str = ""):
        """Save a message to history."""
        tokens_est = len(content) // 4
        now = time.time()
        self.conn.execute(
            "INSERT INTO messages (session_key, agent_id, user_id, role, content, model, provider, tokens_est, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_key, agent_id, user_id, role, content, model, provider, tokens_est, now),
        )
        # Update session
        self.conn.execute(
            "INSERT INTO sessions (session_key, agent_id, user_id, message_count, last_active, created_at) "
            "VALUES (?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(session_key) DO UPDATE SET "
            "message_count = message_count + 1, last_active = ?",
            (session_key, agent_id, user_id, now, now, now),
        )
        self.conn.commit()

    def get_history(self, session_key: str, limit: int = 50) -> list[dict]:
        """Get message history for a session."""
        rows = self.conn.execute(
            "SELECT role, content, model, provider, created_at FROM messages "
            "WHERE session_key = ? ORDER BY id DESC LIMIT ?",
            (session_key, limit),
        ).fetchall()
        # Return in chronological order
        return [dict(r) for r in reversed(rows)]

    def clear_history(self, session_key: str):
        """Clear message history for a session."""
        self.conn.execute("DELETE FROM messages WHERE session_key = ?", (session_key,))
        self.conn.execute(
            "UPDATE sessions SET message_count = 0 WHERE session_key = ?",
            (session_key,),
        )
        self.conn.commit()

    def save_session_model(self, session_key: str, provider: str, model: str):
        """Save active model override for a session."""
        self.conn.execute(
            "UPDATE sessions SET active_provider = ?, active_model = ? WHERE session_key = ?",
            (provider, model, session_key),
        )
        self.conn.commit()

    def get_session(self, session_key: str) -> Optional[dict]:
        """Get session data."""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_sessions(self) -> list[dict]:
        """Get all active sessions."""
        rows = self.conn.execute(
            "SELECT * FROM sessions ORDER BY last_active DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def record_usage(self, provider: str, model: str, agent_id: str, user_id: str,
                     input_tokens: int = 0, output_tokens: int = 0,
                     latency_ms: float = 0, success: bool = True, error: str = ""):
        """Record API usage for tracking."""
        self.conn.execute(
            "INSERT INTO usage (provider, model, agent_id, user_id, input_tokens, output_tokens, "
            "latency_ms, success, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (provider, model, agent_id, user_id, input_tokens, output_tokens,
             latency_ms, int(success), error, time.time()),
        )
        self.conn.commit()

    def get_usage_stats(self, hours: int = 24) -> dict:
        """Get usage statistics for the last N hours."""
        since = time.time() - (hours * 3600)
        rows = self.conn.execute(
            "SELECT provider, model, COUNT(*) as requests, "
            "SUM(input_tokens) as total_input, SUM(output_tokens) as total_output, "
            "AVG(latency_ms) as avg_latency, SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) as errors "
            "FROM usage WHERE created_at > ? GROUP BY provider, model",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_total_stats(self) -> dict:
        """Get overall stats."""
        row = self.conn.execute(
            "SELECT COUNT(*) as total_messages, COUNT(DISTINCT session_key) as total_sessions "
            "FROM messages"
        ).fetchone()
        usage = self.conn.execute(
            "SELECT COUNT(*) as total_requests, SUM(input_tokens) as total_input, "
            "SUM(output_tokens) as total_output FROM usage"
        ).fetchone()
        return {
            "messages": dict(row),
            "usage": dict(usage),
        }

    def close(self):
        self.conn.close()


# Singleton
_db: Optional[Database] = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db
