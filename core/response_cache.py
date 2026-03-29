"""Response Cache — hash-based cache to avoid redundant API calls.

Exact match via query hash + fuzzy match via knowledge base.
TTL-based expiration with category-aware durations.
"""

import hashlib
import re
import time
from typing import Optional

from core.database import get_db
from core.logger import log

# TTL by response category (seconds)
_TTL = {
    "greeting": 86400,       # 1 day
    "factual": 604800,       # 7 days
    "technical": 259200,     # 3 days
    "creative": 3600,        # 1 hour (creative should vary)
    "default": 604800,       # 7 days
}


def _normalize_for_cache(text: str) -> str:
    """Normalize query for cache hashing."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _cache_hash(text: str, agent_id: str = "") -> str:
    normalized = _normalize_for_cache(text)
    key = f"{agent_id}:{normalized}" if agent_id else normalized
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _detect_category(query: str) -> str:
    """Detect query category for TTL assignment."""
    q = query.lower()
    greetings = ["halo", "hai", "hi", "hello", "hey", "selamat", "good morning", "good night"]
    if any(q.startswith(g) for g in greetings) or len(q.split()) < 4:
        return "greeting"
    technical = ["code", "kode", "error", "bug", "api", "server", "deploy", "install",
                 "function", "class", "import", "python", "javascript"]
    if any(t in q for t in technical):
        return "technical"
    creative = ["buat", "create", "gambar", "generate", "tulis", "write", "compose"]
    if any(c in q for c in creative):
        return "creative"
    return "factual"


class ResponseCache:
    """Hash-based response cache with TTL expiration."""

    def __init__(self):
        self._ensure_tables()

    def _ensure_tables(self):
        db = get_db()
        db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS response_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_hash TEXT NOT NULL UNIQUE,
                query_text TEXT NOT NULL,
                response_text TEXT NOT NULL,
                provider TEXT DEFAULT '',
                model TEXT DEFAULT '',
                agent_id TEXT DEFAULT '',
                category TEXT DEFAULT 'default',
                hit_count INTEGER DEFAULT 0,
                tokens_saved INTEGER DEFAULT 0,
                requires_tools INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                last_hit REAL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_cache_hash ON response_cache(query_hash);
            CREATE INDEX IF NOT EXISTS idx_cache_expires ON response_cache(expires_at);
        """)
        db.conn.commit()

    def lookup(self, query: str, agent_id: str = "") -> Optional[str]:
        """Look up cached response. Returns response text or None."""
        db = get_db()
        q_hash = _cache_hash(query, agent_id)
        now = time.time()

        row = db.conn.execute(
            "SELECT id, response_text, requires_tools FROM response_cache "
            "WHERE query_hash = ? AND expires_at > ?",
            (q_hash, now)
        ).fetchone()

        if row and not row["requires_tools"]:
            # Update hit stats
            db.conn.execute(
                "UPDATE response_cache SET hit_count = hit_count + 1, "
                "tokens_saved = tokens_saved + LENGTH(response_text) / 4, "
                "last_hit = ? WHERE id = ?",
                (now, row["id"])
            )
            db.conn.commit()
            log.info(f"Cache HIT for query: {query[:50]}...")
            return row["response_text"]

        # Fallback: check knowledge base for high-confidence match
        try:
            from core.knowledge import get_knowledge_base
            kb = get_knowledge_base()
            results = kb.search(query, limit=1, min_confidence=0.8)
            if results:
                entry = results[0]
                kb.reinforce(entry["id"], positive=True)
                log.info(f"Knowledge HIT (conf={entry['confidence']:.2f}): {query[:50]}...")
                return entry["answer"]
        except Exception:
            pass

        return None

    def store(self, query: str, response: str, provider: str = "", model: str = "",
              agent_id: str = "", requires_tools: bool = False):
        """Store response in cache. Skip tool-requiring responses."""
        if requires_tools:
            return  # Don't cache side-effect responses
        if len(response) < 20:
            return  # Don't cache trivial responses

        db = get_db()
        q_hash = _cache_hash(query, agent_id)
        category = _detect_category(query)
        ttl = _TTL.get(category, _TTL["default"])
        now = time.time()

        try:
            db.conn.execute(
                "INSERT OR REPLACE INTO response_cache "
                "(query_hash, query_text, response_text, provider, model, agent_id, "
                "category, requires_tools, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (q_hash, query[:500], response[:8000], provider, model,
                 agent_id, category, int(requires_tools), now, now + ttl)
            )
            db.conn.commit()
        except Exception as e:
            log.warning(f"Cache store failed: {e}")

    def cleanup_expired(self):
        """Remove expired cache entries."""
        db = get_db()
        now = time.time()
        result = db.conn.execute("DELETE FROM response_cache WHERE expires_at < ?", (now,))
        db.conn.commit()
        if result.rowcount:
            log.info(f"Cache cleanup: removed {result.rowcount} expired entries")

    def get_stats(self) -> dict:
        db = get_db()
        row = db.conn.execute(
            "SELECT COUNT(*) as entries, SUM(hit_count) as total_hits, "
            "SUM(tokens_saved) as tokens_saved FROM response_cache"
        ).fetchone()
        return dict(row) if row else {"entries": 0, "total_hits": 0, "tokens_saved": 0}

    def invalidate(self, query: str, agent_id: str = ""):
        """Invalidate a specific cache entry."""
        db = get_db()
        q_hash = _cache_hash(query, agent_id)
        db.conn.execute("DELETE FROM response_cache WHERE query_hash = ?", (q_hash,))
        db.conn.commit()


# Singleton
_cache: Optional[ResponseCache] = None


def get_response_cache() -> ResponseCache:
    global _cache
    if _cache is None:
        _cache = ResponseCache()
    return _cache
