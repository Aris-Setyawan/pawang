"""Knowledge Base — structured Q&A, facts, and patterns learned from conversations.

Stores reusable knowledge with confidence scoring, FTS5 search, and decay.
Foundation for self-learning agents and local inference fallback.
"""

import hashlib
import re
import time
from typing import Optional

from core.database import get_db
from core.logger import log

# Stopwords for question normalization (Indonesian + English)
_STOPWORDS = {
    "yang", "dan", "di", "ke", "dari", "ini", "itu", "dengan", "untuk",
    "pada", "adalah", "atau", "juga", "tidak", "sudah", "bisa", "ada",
    "akan", "saya", "apa", "kamu", "mas", "bang", "kak", "dong", "sih",
    "the", "a", "an", "is", "are", "was", "were", "in", "on", "at",
    "to", "for", "of", "with", "by", "it", "this", "that", "and", "or",
    "but", "not", "do", "does", "did", "can", "could", "would", "should",
    "what", "how", "why", "when", "where", "who", "which",
}


def _normalize_question(text: str) -> str:
    """Normalize text for hashing: lowercase, strip punct, remove stopwords, sort words."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    words = [w for w in text.split() if w not in _STOPWORDS and len(w) > 1]
    words.sort()
    return " ".join(words)


def _hash_text(text: str) -> str:
    normalized = _normalize_question(text)
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


class KnowledgeBase:
    """Persistent knowledge base backed by SQLite + FTS5."""

    def __init__(self):
        self._ensure_tables()

    def _ensure_tables(self):
        db = get_db()
        db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                question_hash TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                confidence REAL DEFAULT 0.5,
                use_count INTEGER DEFAULT 0,
                source_session TEXT DEFAULT '',
                source_user TEXT DEFAULT '',
                agent_id TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_hash ON knowledge(question_hash);
            CREATE INDEX IF NOT EXISTS idx_knowledge_confidence ON knowledge(confidence DESC);
        """)
        # FTS5 for knowledge search
        try:
            db.conn.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                    question, answer, tags,
                    content_rowid='id',
                    tokenize='unicode61'
                );
                CREATE TRIGGER IF NOT EXISTS knowledge_fts_insert AFTER INSERT ON knowledge
                BEGIN
                    INSERT INTO knowledge_fts(rowid, question, answer, tags)
                    VALUES (NEW.id, NEW.question, NEW.answer, NEW.tags);
                END;
                CREATE TRIGGER IF NOT EXISTS knowledge_fts_delete AFTER DELETE ON knowledge
                BEGIN
                    INSERT INTO knowledge_fts(knowledge_fts, rowid, question, answer, tags)
                    VALUES ('delete', OLD.id, OLD.question, OLD.answer, OLD.tags);
                END;
            """)
        except Exception:
            pass  # FTS5 not available
        db.conn.commit()

    def store(self, question: str, answer: str, category: str = "qa",
              confidence: float = 0.5, source_session: str = "",
              source_user: str = "", agent_id: str = "", tags: str = "") -> int:
        """Store or merge knowledge entry. Returns entry ID."""
        db = get_db()
        q_hash = _hash_text(question)
        now = time.time()

        # Check for existing entry with same hash
        existing = db.conn.execute(
            "SELECT id, confidence, answer, use_count FROM knowledge WHERE question_hash = ?",
            (q_hash,)
        ).fetchone()

        if existing:
            # Merge: weighted average of confidence, keep better answer
            new_conf = min(0.99, (existing["confidence"] * 0.6 + confidence * 0.4))
            # Keep the longer/newer answer if confidence is higher
            new_answer = answer if confidence >= existing["confidence"] else existing["answer"]
            db.conn.execute(
                "UPDATE knowledge SET answer=?, confidence=?, updated_at=?, tags=? WHERE id=?",
                (new_answer, new_conf, now, tags or "", existing["id"])
            )
            db.conn.commit()
            return existing["id"]

        # New entry
        cur = db.conn.execute(
            "INSERT INTO knowledge (question, answer, question_hash, category, confidence, "
            "source_session, source_user, agent_id, tags, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (question, answer, q_hash, category, confidence,
             source_session, source_user, agent_id, tags, now, now)
        )
        db.conn.commit()
        return cur.lastrowid

    def search(self, query: str, limit: int = 5, min_confidence: float = 0.3) -> list[dict]:
        """Search knowledge base using FTS5 with confidence filter."""
        db = get_db()
        results = []

        # Try FTS5 first
        try:
            rows = db.conn.execute(
                "SELECT k.*, bm25(knowledge_fts) AS rank "
                "FROM knowledge_fts fts "
                "JOIN knowledge k ON k.id = fts.rowid "
                "WHERE knowledge_fts MATCH ? AND k.confidence >= ? "
                "ORDER BY rank * k.confidence LIMIT ?",
                (query, min_confidence, limit)
            ).fetchall()
            results = [dict(r) for r in rows]
        except Exception:
            pass

        # Fallback to LIKE search
        if not results:
            words = _normalize_question(query).split()[:3]
            if words:
                like_clause = " AND ".join(f"(question LIKE '%{w}%' OR answer LIKE '%{w}%')" for w in words)
                rows = db.conn.execute(
                    f"SELECT * FROM knowledge WHERE {like_clause} AND confidence >= ? "
                    f"ORDER BY confidence DESC, use_count DESC LIMIT ?",
                    (min_confidence, limit)
                ).fetchall()
                results = [dict(r) for r in rows]

        return results

    def get_by_hash(self, question: str) -> Optional[dict]:
        """Exact match lookup by question hash."""
        db = get_db()
        q_hash = _hash_text(question)
        row = db.conn.execute(
            "SELECT * FROM knowledge WHERE question_hash = ?", (q_hash,)
        ).fetchone()
        return dict(row) if row else None

    def reinforce(self, knowledge_id: int, positive: bool = True):
        """Adjust confidence and increment use count."""
        db = get_db()
        delta = 0.05 if positive else -0.05
        db.conn.execute(
            "UPDATE knowledge SET confidence = MIN(0.99, MAX(0.01, confidence + ?)), "
            "use_count = use_count + 1, updated_at = ? WHERE id = ?",
            (delta, time.time(), knowledge_id)
        )
        db.conn.commit()

    def decay_old(self, days: int = 30):
        """Reduce confidence for entries not used in N days."""
        cutoff = time.time() - (days * 86400)
        db = get_db()
        db.conn.execute(
            "UPDATE knowledge SET confidence = MAX(0.01, confidence - 0.1) "
            "WHERE updated_at < ? AND use_count < 3",
            (cutoff,)
        )
        db.conn.commit()

    def get_stats(self) -> dict:
        db = get_db()
        row = db.conn.execute(
            "SELECT COUNT(*) as total, AVG(confidence) as avg_conf, "
            "SUM(use_count) as total_uses FROM knowledge"
        ).fetchone()
        return dict(row) if row else {"total": 0, "avg_conf": 0, "total_uses": 0}

    def cleanup(self, max_entries: int = 10000):
        """Remove lowest-confidence entries if over limit."""
        db = get_db()
        count = db.conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
        if count > max_entries:
            db.conn.execute(
                "DELETE FROM knowledge WHERE id IN ("
                "SELECT id FROM knowledge ORDER BY confidence ASC, use_count ASC LIMIT ?)",
                (count - max_entries,)
            )
            db.conn.commit()
            log.info(f"Knowledge cleanup: removed {count - max_entries} entries")


# Singleton
_kb: Optional[KnowledgeBase] = None


def get_knowledge_base() -> KnowledgeBase:
    global _kb
    if _kb is None:
        _kb = KnowledgeBase()
    return _kb
