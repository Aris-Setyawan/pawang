"""SQLite WAL Database — persistent sessions, message history, usage tracking."""

import sqlite3
import json
import time
from pathlib import Path
from threading import RLock
from typing import Optional

from core.logger import log

DB_PATH = Path(__file__).parent.parent / "data" / "pawang.db"


class Database:
    """SQLite WAL database for persistent storage."""

    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10.0)
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

            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                content TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                memory_type TEXT DEFAULT 'user',
                agent_id TEXT DEFAULT '',
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);
            CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(user_id, category);

            CREATE TABLE IF NOT EXISTS scheduler_state (
                job_name TEXT PRIMARY KEY,
                last_run REAL DEFAULT 0,
                run_count INTEGER DEFAULT 0,
                last_error TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT PRIMARY KEY,
                agent_id TEXT DEFAULT '',
                thinking_mode TEXT DEFAULT '',
                voice_reply INTEGER DEFAULT 0,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT NOT NULL,
                user_id TEXT NOT NULL,
                label TEXT DEFAULT '',
                messages_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_checkpoints_session
                ON checkpoints(session_key, user_id);

            CREATE TABLE IF NOT EXISTS provider_state (
                provider_name TEXT PRIMARY KEY,
                disabled INTEGER DEFAULT 0,
                backup_key TEXT DEFAULT '',
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS github_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT DEFAULT '',
                params TEXT DEFAULT '',
                success INTEGER DEFAULT 1,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_gh_audit_ts
                ON github_audit(created_at DESC);
        """)
        self.conn.commit()
        self._setup_fts()
        self._migrate()
        log.info(f"Database initialized: {self.path}")

    def _setup_fts(self):
        """Create FTS5 virtual table for full-text search on messages."""
        try:
            self.conn.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    content, session_key, user_id, agent_id,
                    content_rowid='id',
                    tokenize='unicode61'
                );

                CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages
                BEGIN
                    INSERT INTO messages_fts(rowid, content, session_key, user_id, agent_id)
                    VALUES (NEW.id, NEW.content, NEW.session_key, NEW.user_id, NEW.agent_id);
                END;

                CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages
                BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content, session_key, user_id, agent_id)
                    VALUES ('delete', OLD.id, OLD.content, OLD.session_key, OLD.user_id, OLD.agent_id);
                END;
            """)
            self.conn.commit()
        except Exception as e:
            log.warning(f"FTS5 setup skipped (may not be available): {e}")

    def _migrate(self):
        """Run schema migrations for existing databases."""
        # Add memory_type column if missing
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(memories)").fetchall()]
        if "memory_type" not in cols:
            self.conn.execute("ALTER TABLE memories ADD COLUMN memory_type TEXT DEFAULT 'user'")
            self.conn.commit()
            log.info("Migration: added memory_type column to memories")

    def save_message(self, session_key: str, agent_id: str, user_id: str,
                     role: str, content: str, model: str = "", provider: str = ""):
        """Save a message to history."""
        tokens_est = len(content) // 4
        now = time.time()
        with self._lock:
            self.conn.execute(
                "INSERT INTO messages (session_key, agent_id, user_id, role, content, model, provider, tokens_est, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_key, agent_id, user_id, role, content, model, provider, tokens_est, now),
            )
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
        with self._lock:
            self.conn.execute("DELETE FROM messages WHERE session_key = ?", (session_key,))
            self.conn.execute(
                "UPDATE sessions SET message_count = 0 WHERE session_key = ?",
                (session_key,),
            )
            self.conn.commit()

    def save_session_model(self, session_key: str, provider: str, model: str):
        """Save active model override for a session."""
        with self._lock:
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
        """Record API usage for tracking + feed TokenGuard."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO usage (provider, model, agent_id, user_id, input_tokens, output_tokens, "
                "latency_ms, success, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (provider, model, agent_id, user_id, input_tokens, output_tokens,
                 latency_ms, int(success), error, time.time()),
            )
            self.conn.commit()

        # Feed TokenGuard (async-safe fire-and-forget)
        if success and (input_tokens + output_tokens) > 0:
            try:
                import asyncio
                from core.token_guard import get_token_guard
                guard = get_token_guard()
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(
                        guard.record(agent_id, input_tokens, output_tokens, provider, model)
                    )
            except Exception:
                pass  # guard not initialized yet during startup

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
            "messages": dict(row) if row else {"total_messages": 0, "total_sessions": 0},
            "usage": dict(usage) if usage else {"total_requests": 0, "total_input": 0, "total_output": 0},
        }

    # --- Memory ---

    def save_memory(self, user_id: str, content: str, category: str = "general",
                    agent_id: str = "", memory_type: str = "user"):
        """Save a fact/memory about a user.

        memory_type: 'user' (user facts/preferences) or 'agent' (agent observations/conventions)
        """
        with self._lock:
            # Avoid exact duplicates
            existing = self.conn.execute(
                "SELECT id FROM memories WHERE user_id = ? AND content = ?",
                (user_id, content),
            ).fetchone()
            if existing:
                return existing["id"]

            self.conn.execute(
                "INSERT INTO memories (user_id, content, category, memory_type, agent_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, content, category, memory_type, agent_id, time.time()),
            )
            self.conn.commit()
            return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_memories(self, user_id: str, category: Optional[str] = None,
                     limit: int = 50) -> list[dict]:
        """Get memories for a user, optionally filtered by category."""
        if category:
            rows = self.conn.execute(
                "SELECT id, content, category, agent_id, created_at, memory_type FROM memories "
                "WHERE user_id = ? AND category = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, category, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, content, category, agent_id, created_at, memory_type FROM memories "
                "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_memory(self, memory_id: int, user_id: str) -> bool:
        """Delete a specific memory by ID (scoped to user for safety)."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM memories WHERE id = ? AND user_id = ?",
                (memory_id, user_id),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def search_sessions(self, user_id: str, query: str, limit: int = 20) -> list[dict]:
        """Full-text search across message history using FTS5.

        Returns matching messages with snippet highlights.
        Falls back to LIKE if FTS5 is not available.
        """
        if not query or len(query) > 200:
            return []
        try:
            rows = self.conn.execute(
                "SELECT m.id, m.session_key, m.agent_id, m.role, m.created_at, "
                "snippet(messages_fts, 0, '**', '**', '...', 40) as snippet "
                "FROM messages_fts "
                "JOIN messages m ON m.id = messages_fts.rowid "
                "WHERE messages_fts MATCH ? AND m.user_id = ? "
                "ORDER BY rank LIMIT ?",
                (query, user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            # Fallback to LIKE search
            rows = self.conn.execute(
                "SELECT id, session_key, agent_id, role, content, created_at "
                "FROM messages WHERE user_id = ? AND content LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (user_id, f"%{query}%", limit),
            ).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                # Create manual snippet
                content = d.pop("content", "")
                idx = content.lower().find(query.lower())
                start = max(0, idx - 40)
                end = min(len(content), idx + len(query) + 40)
                d["snippet"] = ("..." if start > 0 else "") + content[start:end] + ("..." if end < len(content) else "")
                results.append(d)
            return results

    def search_memories(self, user_id: str, query: str, limit: int = 20) -> list[dict]:
        """Search memories by keyword (simple LIKE match)."""
        if not query or len(query) > 200:
            return []
        rows = self.conn.execute(
            "SELECT id, content, category, agent_id, created_at FROM memories "
            "WHERE user_id = ? AND content LIKE ? ORDER BY created_at DESC LIMIT ?",
            (user_id, f"%{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- User Settings ---

    def save_user_settings(self, user_id: str, agent_id: str = "",
                           thinking_mode: str = "", voice_reply: bool = False):
        """Save user preferences."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO user_settings (user_id, agent_id, thinking_mode, voice_reply, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "agent_id = ?, thinking_mode = ?, voice_reply = ?, updated_at = ?",
                (user_id, agent_id, thinking_mode, int(voice_reply), time.time(),
                 agent_id, thinking_mode, int(voice_reply), time.time()),
            )
            self.conn.commit()

    def get_user_settings(self, user_id: str) -> Optional[dict]:
        """Get user preferences."""
        row = self.conn.execute(
            "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_user_settings(self) -> list[dict]:
        """Get all user settings for bulk restore."""
        rows = self.conn.execute("SELECT * FROM user_settings").fetchall()
        return [dict(r) for r in rows]

    # --- Scheduler State ---

    def save_job_state(self, job_name: str, last_run: float, run_count: int,
                       last_error: str = "", enabled: bool = True):
        """Persist scheduler job state."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO scheduler_state (job_name, last_run, run_count, last_error, enabled) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(job_name) DO UPDATE SET "
                "last_run = ?, run_count = ?, last_error = ?, enabled = ?",
                (job_name, last_run, run_count, last_error, int(enabled),
                 last_run, run_count, last_error, int(enabled)),
            )
            self.conn.commit()

    def get_job_states(self) -> dict[str, dict]:
        """Get all persisted job states."""
        rows = self.conn.execute("SELECT * FROM scheduler_state").fetchall()
        return {r["job_name"]: dict(r) for r in rows}

    def set_provider_disabled(self, provider_name: str, backup_key: str):
        """Mark provider as disabled and persist the backup key for later restore."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO provider_state (provider_name, disabled, backup_key, updated_at) "
                "VALUES (?, 1, ?, ?) "
                "ON CONFLICT(provider_name) DO UPDATE SET "
                "disabled = 1, backup_key = ?, updated_at = ?",
                (provider_name, backup_key, time.time(), backup_key, time.time()),
            )
            self.conn.commit()

    def set_provider_enabled(self, provider_name: str) -> str:
        """Clear disabled flag; return the stored backup_key so caller can restore api_key."""
        with self._lock:
            row = self.conn.execute(
                "SELECT backup_key FROM provider_state WHERE provider_name = ?",
                (provider_name,),
            ).fetchone()
            backup = row["backup_key"] if row else ""
            self.conn.execute(
                "INSERT INTO provider_state (provider_name, disabled, backup_key, updated_at) "
                "VALUES (?, 0, '', ?) "
                "ON CONFLICT(provider_name) DO UPDATE SET "
                "disabled = 0, backup_key = '', updated_at = ?",
                (provider_name, time.time(), time.time()),
            )
            self.conn.commit()
            return backup

    def get_disabled_providers(self) -> dict[str, str]:
        """Return {provider_name: backup_key} for all providers currently marked disabled."""
        rows = self.conn.execute(
            "SELECT provider_name, backup_key FROM provider_state WHERE disabled = 1"
        ).fetchall()
        return {r["provider_name"]: r["backup_key"] for r in rows}

    def record_github_audit(self, agent_id: str, user_id: str, action: str,
                            target: str = "", params: str = "",
                            success: bool = True):
        """Append one entry to the GitHub tool audit log."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO github_audit (agent_id, user_id, action, target, "
                "params, success, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (agent_id, user_id, action, target, params,
                 int(success), time.time()),
            )
            self.conn.commit()

    def get_github_audit(self, limit: int = 50) -> list[dict]:
        """Return latest audit rows, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM github_audit ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()


# Singleton
_db: Optional[Database] = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db
