"""Claude Code Bridge — run Claude Code CLI from Telegram.

Allows users to interact with Claude Code sessions through Telegram,
without needing SSH/mosh. Supports session management, resume, and
streaming output.
"""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.database import get_db
from core.logger import log

CLAUDE_BIN = "/root/.local/bin/claude"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
AGENT9_PROJECTS = Path("/root/pawang/projects")  # project folders for agent9


@dataclass
class CCSession:
    id: int
    name: str
    directory: str
    session_id: str
    created_at: float
    last_used: float = 0
    is_active: bool = False
    description: str = ""


@dataclass
class CCActiveSession:
    """Tracks a user's active Claude Code session."""
    session: CCSession
    process: Optional[asyncio.subprocess.Process] = None
    started_at: float = field(default_factory=time.time)


class ClaudeCodeManager:
    def __init__(self):
        self._ensure_tables()
        self._active: dict[str, CCActiveSession] = {}  # user_id -> active session

    def _ensure_tables(self):
        db = get_db()
        db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS claude_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                directory TEXT NOT NULL,
                session_id TEXT NOT NULL,
                user_id TEXT DEFAULT '',
                description TEXT DEFAULT '',
                created_at REAL NOT NULL,
                last_used REAL DEFAULT 0
            );
        """)
        db.conn.commit()
        # Migrate: add description column if missing
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(claude_sessions)")}
        if "description" not in cols:
            db.conn.execute("ALTER TABLE claude_sessions ADD COLUMN description TEXT DEFAULT ''")
            db.conn.commit()

    @staticmethod
    def _resolve_folder(dir_name: str) -> str:
        """Convert Claude project dir name back to real path.

        Claude encodes paths as: /root/pawang -> -root-pawang
        But folder names can contain dashes too (e.g. sisbro-bill).
        Strategy: try progressively replacing dashes with slashes from left,
        pick the longest existing path.
        """
        # Strip leading dash, split on remaining dashes
        parts = dir_name.lstrip("-").split("-")
        # Try from most slashes to fewest — first existing path wins
        best = "/" + "/".join(parts)  # fallback: all dashes = slashes
        for i in range(len(parts) - 1, 0, -1):
            # Join first i parts with /, rest with -
            candidate = "/" + "/".join(
                "-".join(parts[j] for j in range(g, g + 1))
                if g < i else parts[g]
                for g in range(len(parts))
            )
            # Actually, simpler: try every split point
            pass

        # Better approach: recursively resolve left to right
        def _resolve(idx: int, current: str) -> Optional[str]:
            if idx >= len(parts):
                return current if os.path.isdir(current) else None
            # Try consuming more parts joined with dash
            for end in range(len(parts), idx, -1):
                segment = "-".join(parts[idx:end])
                candidate = current + "/" + segment
                result = _resolve(end, candidate)
                if result is not None:
                    return result
            return None

        resolved = _resolve(0, "")
        return resolved if resolved else best

    def scan_sessions(self) -> list[CCSession]:
        """Scan ~/.claude/projects/ — return latest session per project."""
        sessions = []
        if not CLAUDE_PROJECTS.exists():
            return sessions

        for proj_dir in sorted(CLAUDE_PROJECTS.iterdir()):
            if not proj_dir.is_dir():
                continue
            folder = self._resolve_folder(proj_dir.name)

            # Only pick the most recent valid session per project
            jsonls = sorted(
                proj_dir.glob("*.jsonl"),
                key=lambda f: f.stat().st_mtime, reverse=True,
            )
            for jsonl in jsonls:
                if jsonl.stat().st_size < 1000:
                    continue
                sessions.append(CCSession(
                    id=0,
                    name="",
                    directory=folder,
                    session_id=jsonl.stem,
                    created_at=jsonl.stat().st_mtime,
                ))
                break  # only latest per project

        return sessions

    def get_saved_sessions(self, user_id: str = "") -> list[CCSession]:
        """Get saved/named sessions from database."""
        db = get_db()
        rows = db.conn.execute(
            "SELECT id, name, directory, session_id, created_at, last_used, "
            "COALESCE(description, '') as description "
            "FROM claude_sessions ORDER BY last_used DESC"
        ).fetchall()
        return [CCSession(
            id=r["id"], name=r["name"], directory=r["directory"],
            session_id=r["session_id"], created_at=r["created_at"],
            last_used=r["last_used"], description=r["description"],
        ) for r in rows]

    def get_session_by_id(self, db_id: int) -> Optional[CCSession]:
        """Get a single session by DB id."""
        db = get_db()
        r = db.conn.execute(
            "SELECT id, name, directory, session_id, created_at, last_used, "
            "COALESCE(description, '') as description "
            "FROM claude_sessions WHERE id = ?", (db_id,)
        ).fetchone()
        if not r:
            return None
        return CCSession(
            id=r["id"], name=r["name"], directory=r["directory"],
            session_id=r["session_id"], created_at=r["created_at"],
            last_used=r["last_used"], description=r["description"],
        )

    def save_session(self, name: str, directory: str, session_id: str,
                     user_id: str = "") -> int:
        """Save a named session."""
        db = get_db()
        now = time.time()
        # Check if session_id already saved
        existing = db.conn.execute(
            "SELECT id FROM claude_sessions WHERE session_id = ?",
            (session_id,)
        ).fetchone()
        if existing:
            db.conn.execute(
                "UPDATE claude_sessions SET name = ?, directory = ?, last_used = ? WHERE id = ?",
                (name, directory, now, existing["id"])
            )
            db.conn.commit()
            return existing["id"]

        cursor = db.conn.execute(
            "INSERT INTO claude_sessions (name, directory, session_id, user_id, created_at, last_used) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, directory, session_id, user_id, now, now)
        )
        db.conn.commit()
        return cursor.lastrowid

    def delete_session(self, session_db_id: int):
        db = get_db()
        db.conn.execute("DELETE FROM claude_sessions WHERE id = ?", (session_db_id,))
        db.conn.commit()

    def rename_session(self, session_db_id: int, new_name: str):
        db = get_db()
        db.conn.execute(
            "UPDATE claude_sessions SET name = ? WHERE id = ?",
            (new_name, session_db_id)
        )
        db.conn.commit()

    def update_description(self, session_db_id: int, description: str):
        db = get_db()
        db.conn.execute(
            "UPDATE claude_sessions SET description = ? WHERE id = ?",
            (description, session_db_id)
        )
        db.conn.commit()

    def touch_session(self, session_db_id: int):
        db = get_db()
        db.conn.execute(
            "UPDATE claude_sessions SET last_used = ? WHERE id = ?",
            (time.time(), session_db_id)
        )
        db.conn.commit()

    # --- Project matching (agent9 delegation) ---

    @staticmethod
    def _extract_keywords(text: str) -> set[str]:
        """Extract keywords from text, ignoring stop words."""
        stop = {
            "buat", "buatin", "bikin", "tolong", "coba", "project", "proyek",
            "baru", "new", "lanjut", "lanjutin", "continue", "resume",
            "yang", "di", "ke", "dari", "untuk", "dengan", "dan", "atau",
            "ini", "itu", "nih", "dong", "ya", "mas", "mau", "saya",
            "the", "a", "an", "is", "for", "to", "of", "in", "on",
            "create", "make", "build", "please", "help",
        }
        words = set(re.findall(r'[a-zA-Z0-9]+', text.lower()))
        return {w for w in words if w not in stop and len(w) >= 2}

    def match_session(self, task: str, user_id: str = "") -> Optional[CCSession]:
        """Find best matching session for a task description.

        Compares task keywords against session name + description.
        Returns session if confidence > 0.3, else None.
        """
        task_kw = self._extract_keywords(task)
        if not task_kw:
            return None

        saved = self.get_saved_sessions(user_id)
        best = None
        best_score = 0.0

        for s in saved:
            session_text = f"{s.name} {s.description}"
            session_kw = self._extract_keywords(session_text)
            if not session_kw:
                continue
            # Jaccard similarity
            overlap = len(task_kw & session_kw)
            union = len(task_kw | session_kw)
            score = overlap / union if union > 0 else 0
            if score > best_score:
                best_score = score
                best = s

        if best and best_score >= 0.3:
            log.info(f"CC session match ({best_score:.2f}): '{task[:50]}' → {best.name}")
            return best
        return None

    def find_or_create_project(self, task: str, user_id: str = "") -> CCSession:
        """Find matching session or create new project folder + session.

        1. Try matching existing session by keywords
        2. If no match, extract project name from task, create folder, create session
        """
        # 1. Try match existing
        matched = self.match_session(task, user_id)
        if matched:
            return matched

        # 2. Extract project name from task
        name = self._extract_project_name(task)
        slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or "project"

        # 3. Create folder
        AGENT9_PROJECTS.mkdir(parents=True, exist_ok=True)
        project_dir = AGENT9_PROJECTS / slug
        # Avoid collision
        if project_dir.exists():
            i = 2
            while (AGENT9_PROJECTS / f"{slug}-{i}").exists():
                i += 1
            project_dir = AGENT9_PROJECTS / f"{slug}-{i}"
        project_dir.mkdir(parents=True, exist_ok=True)

        # 4. Save session to DB
        db_id = self.save_session(name, str(project_dir), "", user_id)
        self.update_description(db_id, task[:200])

        session = CCSession(
            id=db_id, name=name, directory=str(project_dir),
            session_id="", created_at=time.time(),
            description=task[:200],
        )
        log.info(f"CC new project: '{name}' at {project_dir}")
        return session

    @staticmethod
    def _extract_project_name(task: str) -> str:
        """Try to extract a meaningful project name from task description."""
        # Common patterns: "buat project X", "project X", "bikin X"
        patterns = [
            r'(?:project|proyek)\s+(?:tentang\s+)?(.+?)(?:\s*[,.]|$)',
            r'(?:buat|buatin|bikin)\s+(?:project\s+)?(.+?)(?:\s*[,.]|$)',
            r'(?:lanjut|lanjutin|resume|continue)\s+(?:project\s+)?(.+?)(?:\s*[,.]|$)',
        ]
        for pattern in patterns:
            m = re.search(pattern, task, re.IGNORECASE)
            if m:
                name = m.group(1).strip()
                # Clean up — take first few meaningful words
                words = name.split()[:4]
                return " ".join(words)
        # Fallback: first 3 meaningful words from task
        kw = list(ClaudeCodeManager._extract_keywords(task))[:3]
        return " ".join(kw) if kw else "untitled"

    # --- Active session management ---

    def get_active(self, user_id: str) -> Optional[CCActiveSession]:
        return self._active.get(user_id)

    def set_active(self, user_id: str, session: CCSession):
        self._active[user_id] = CCActiveSession(session=session)

    def clear_active(self, user_id: str):
        active = self._active.pop(user_id, None)
        if active and active.process:
            try:
                active.process.kill()
            except Exception:
                pass

    def is_in_cc_mode(self, user_id: str) -> bool:
        return user_id in self._active

    # --- Execute Claude Code ---

    async def execute(self, session: CCSession, prompt: str,
                      on_chunk=None, timeout: int = 300) -> str:
        """Run claude -p with streaming output."""
        cmd = [
            CLAUDE_BIN, "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", "acceptEdits",
            "--max-turns", "50",
        ]

        if session.session_id:
            cmd += ["--resume", session.session_id]

        log.info(f"Claude Code: {' '.join(cmd[:8])}... cwd={session.directory}")

        try:
            # Strip ANTHROPIC_API_KEY so claude CLI uses subscription auth
            env = {k: v for k, v in os.environ.items()
                   if k != "ANTHROPIC_API_KEY"}

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=session.directory,
            )

            full_output = []
            buffer = ""

            async def read_stream():
                nonlocal buffer
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        break
                    text = chunk.decode(errors="replace")
                    buffer += text

                    # Parse stream-json lines
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                            content = self._extract_content(event)
                            if content:
                                full_output.append(content)
                                if on_chunk:
                                    await on_chunk(content)
                        except json.JSONDecodeError:
                            if line:
                                full_output.append(line)

            try:
                await asyncio.wait_for(read_stream(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                full_output.append("\n[Timeout — task terlalu lama]")

            await proc.wait()

            # Capture stderr for errors (rate limit, auth issues, etc.)
            stderr_data = b""
            try:
                stderr_data = await asyncio.wait_for(
                    proc.stderr.read(), timeout=5
                )
            except Exception:
                pass
            if stderr_data:
                stderr_text = stderr_data.decode(errors="replace").strip()
                if stderr_text:
                    log.warning(f"Claude Code stderr: {stderr_text[:200]}")
                    full_output.append(f"\n[stderr] {stderr_text}")

            # Update session_id from output if new session
            result_text = "\n".join(full_output)

            # Touch last_used
            if session.id:
                self.touch_session(session.id)

            return result_text if result_text else "(no output — check /cc session or try again)"

        except FileNotFoundError:
            return "Error: Claude Code CLI not found. Install: npm install -g @anthropic-ai/claude-code"
        except Exception as e:
            return f"Error: {e}"

    def _extract_content(self, event: dict) -> str:
        """Extract readable content from stream-json event."""
        etype = event.get("type", "")

        if etype == "assistant":
            # Text message from Claude
            msg = event.get("message", {})
            content = msg.get("content", [])
            parts = []
            for c in content:
                if isinstance(c, dict):
                    if c.get("type") == "text":
                        parts.append(c.get("text", ""))
                    elif c.get("type") == "tool_use":
                        name = c.get("name", "")
                        inp = c.get("input", {})
                        if name == "Edit":
                            parts.append(f"[Edit: {inp.get('file_path', '?')}]")
                        elif name == "Write":
                            parts.append(f"[Write: {inp.get('file_path', '?')}]")
                        elif name == "Read":
                            parts.append(f"[Read: {inp.get('file_path', '?')}]")
                        elif name == "Bash":
                            cmd = inp.get("command", "")
                            parts.append(f"[$ {cmd[:80]}]")
                        elif name == "Grep":
                            parts.append(f"[Grep: {inp.get('pattern', '?')}]")
                        else:
                            parts.append(f"[{name}]")
                elif isinstance(c, str):
                    parts.append(c)
            return "\n".join(parts)

        elif etype == "result":
            result = event.get("result", "")
            sid = event.get("session_id", "")
            cost = event.get("total_cost_usd", 0)
            turns = event.get("num_turns", 0)
            parts = []
            if result:
                parts.append(result)
            if sid or cost:
                meta = []
                if turns:
                    meta.append(f"{turns} turns")
                if cost:
                    meta.append(f"${cost:.4f}")
                if sid:
                    meta.append(f"session: {sid[:8]}...")
                parts.append(f"[{', '.join(meta)}]")
            return "\n".join(parts)

        return ""


# Singleton
_manager: Optional[ClaudeCodeManager] = None


def get_cc_manager() -> ClaudeCodeManager:
    global _manager
    if _manager is None:
        _manager = ClaudeCodeManager()
    return _manager
