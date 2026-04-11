"""Intent Cache — learn message→tool mappings from successful AI calls.

When AI maps a user message to a tool call, we learn the pattern.
Next time a similar message comes in, execute the tool directly — 0 API cost.

Self-learning: makin lama makin pintar, makin hemat token.
"""

import json
import re
import time
from dataclasses import dataclass
from typing import Optional

from core.database import get_db
from core.logger import log

# Tools safe for auto-execution (read-only / idempotent)
SAFE_TOOLS = {
    "check_balances",
    "weather",
    "recall_memories",
    "file_read",
    "file_search",
    "code_search",
    "calculator",
    "wikipedia",
    "gog_gmail",       # read-only
    "gog_calendar",    # list only (create needs args)
}

# Stop words (ID + EN) — stripped during keyword extraction
_STOP_WORDS = {
    "dan", "atau", "yang", "di", "ke", "dari", "ini", "itu", "untuk",
    "dengan", "adalah", "juga", "sudah", "bisa", "ada", "saya", "aku",
    "kamu", "mas", "dong", "ya", "nih", "deh", "lah", "kan", "mau",
    "tolong", "coba", "please", "the", "a", "an", "is", "are", "was",
    "it", "to", "for", "of", "in", "on", "at", "by", "my", "me",
    "do", "does", "can", "could", "would", "should", "how", "what",
    "berapa", "gimana", "bagaimana", "apakah", "apa",
}

MIN_CONFIDENCE = 0.60


@dataclass
class IntentMatch:
    tool_name: str
    tool_args: dict
    confidence: float
    pattern_id: int


def _extract_keywords(text: str) -> set[str]:
    """Extract meaningful keywords from text."""
    words = set(re.findall(r'\w+', text.lower()))
    return {w for w in words if w not in _STOP_WORDS and len(w) >= 2}


def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity between two sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _resolve_args(tool_name: str, stored_args: dict, query: str) -> dict:
    """Resolve tool arguments — some tools need fresh args from the query."""
    if tool_name == "check_balances":
        return {}
    if tool_name == "calculator":
        expr = re.sub(r'[a-zA-Z\s]', '', query).strip()
        if expr:
            return {"expression": expr}
        return stored_args
    if tool_name == "weather":
        _weather_words = {"cuaca", "weather", "suhu", "temperatur", "hujan", "rain", "cek"}
        words = [w for w in re.findall(r'\w+', query)
                 if w.lower() not in _STOP_WORDS and w.lower() not in _weather_words and len(w) > 2]
        if words:
            return {"location": " ".join(words)}
        return stored_args or {"location": "Jakarta"}
    return stored_args


class IntentCache:
    def __init__(self):
        self._ensure_tables()
        self._seed_if_empty()

    def _ensure_tables(self):
        db = get_db()
        db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS intent_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keywords TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tool_args TEXT DEFAULT '{}',
                agent_id TEXT DEFAULT '',
                hit_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                last_hit REAL DEFAULT 0,
                example_query TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_intent_tool
                ON intent_cache(tool_name);
        """)
        db.conn.commit()

    def _seed_if_empty(self):
        """Seed common patterns on first run."""
        db = get_db()
        count = db.conn.execute("SELECT COUNT(*) FROM intent_cache").fetchone()[0]
        if count > 0:
            return

        seeds = [
            (["cek", "saldo"], "check_balances", {}, "cek saldo"),
            (["cek", "balance"], "check_balances", {}, "cek balance"),
            (["balance", "api"], "check_balances", {}, "balance api"),
            (["saldo", "api"], "check_balances", {}, "saldo api"),
            (["check", "balance"], "check_balances", {}, "check balance"),
            (["check", "balances"], "check_balances", {}, "check balances"),
        ]
        now = time.time()
        for kw, tool, args, example in seeds:
            db.conn.execute(
                "INSERT INTO intent_cache "
                "(keywords, tool_name, tool_args, agent_id, success_count, created_at, example_query) "
                "VALUES (?, ?, ?, '', 1, ?, ?)",
                (json.dumps(sorted(kw)), tool, json.dumps(args), now, example)
            )
        db.conn.commit()
        log.info(f"Intent cache: seeded {len(seeds)} patterns")

    def match(self, query: str, agent_id: str = "") -> Optional[IntentMatch]:
        """Try to match query against learned intents. Returns match or None."""
        query_kw = _extract_keywords(query)
        if not query_kw:
            return None

        db = get_db()
        rows = db.conn.execute(
            "SELECT id, keywords, tool_name, tool_args, "
            "success_count, fail_count FROM intent_cache"
        ).fetchall()

        best = None
        best_score = 0.0

        for row in rows:
            stored_kw = set(json.loads(row["keywords"]))
            score = _jaccard(query_kw, stored_kw)

            # Boost by reliability (70–100% weight)
            total = row["success_count"] + row["fail_count"]
            if total > 0:
                reliability = row["success_count"] / total
                score *= (0.7 + 0.3 * reliability)

            if score > best_score:
                best_score = score
                best = row

        if not best or best_score < MIN_CONFIDENCE:
            return None

        if best["tool_name"] not in SAFE_TOOLS:
            return None

        try:
            stored_args = json.loads(best["tool_args"])
        except (json.JSONDecodeError, TypeError):
            stored_args = {}

        args = _resolve_args(best["tool_name"], stored_args, query)

        log.info(f"Intent MATCH ({best_score:.2f}): '{query[:50]}' → {best['tool_name']}({args})")
        return IntentMatch(
            tool_name=best["tool_name"],
            tool_args=args,
            confidence=best_score,
            pattern_id=best["id"],
        )

    def record_hit(self, pattern_id: int, success: bool):
        """Record whether a cached intent execution succeeded or failed."""
        db = get_db()
        col = "success_count" if success else "fail_count"
        db.conn.execute(
            f"UPDATE intent_cache SET hit_count = hit_count + 1, "
            f"{col} = {col} + 1, last_hit = ? WHERE id = ?",
            (time.time(), pattern_id)
        )
        db.conn.commit()

        # Auto-prune if too many failures
        row = db.conn.execute(
            "SELECT success_count, fail_count FROM intent_cache WHERE id = ?",
            (pattern_id,)
        ).fetchone()
        if row and row["fail_count"] > 3 and row["fail_count"] > row["success_count"]:
            db.conn.execute("DELETE FROM intent_cache WHERE id = ?", (pattern_id,))
            db.conn.commit()
            log.info(f"Intent cache: pruned unreliable pattern #{pattern_id}")

    def learn(self, query: str, tool_name: str, tool_args: dict,
              agent_id: str = ""):
        """Learn a new intent mapping from a successful AI tool call."""
        if tool_name not in SAFE_TOOLS:
            return

        keywords = _extract_keywords(query)
        if len(keywords) < 1:
            return

        kw_json = json.dumps(sorted(keywords))
        db = get_db()

        # Check for existing similar pattern (same tool)
        existing = db.conn.execute(
            "SELECT id, keywords FROM intent_cache WHERE tool_name = ?",
            (tool_name,)
        ).fetchall()

        for row in existing:
            stored_kw = set(json.loads(row["keywords"]))
            if _jaccard(keywords, stored_kw) > 0.5:
                # Similar — merge keywords to broaden the pattern
                merged = sorted(keywords | stored_kw)
                db.conn.execute(
                    "UPDATE intent_cache SET keywords = ?, "
                    "success_count = success_count + 1 WHERE id = ?",
                    (json.dumps(merged), row["id"])
                )
                db.conn.commit()
                log.info(f"Intent cache: merged keywords for {tool_name}")
                return

        # New pattern
        args_json = json.dumps(tool_args) if tool_args else "{}"
        db.conn.execute(
            "INSERT INTO intent_cache "
            "(keywords, tool_name, tool_args, agent_id, success_count, "
            "created_at, example_query) VALUES (?, ?, ?, ?, 1, ?, ?)",
            (kw_json, tool_name, args_json, agent_id, time.time(), query[:200])
        )
        db.conn.commit()
        log.info(f"Intent cache: learned '{tool_name}' from: {query[:60]}")

    def get_stats(self) -> dict:
        db = get_db()
        row = db.conn.execute(
            "SELECT COUNT(*) as patterns, "
            "COALESCE(SUM(hit_count), 0) as total_hits, "
            "COALESCE(SUM(success_count), 0) as successes "
            "FROM intent_cache"
        ).fetchone()
        return dict(row) if row else {"patterns": 0, "total_hits": 0, "successes": 0}

    def list_patterns(self) -> list[dict]:
        db = get_db()
        rows = db.conn.execute(
            "SELECT tool_name, example_query, hit_count, "
            "success_count, fail_count, keywords "
            "FROM intent_cache ORDER BY hit_count DESC LIMIT 20"
        ).fetchall()
        return [dict(r) for r in rows]


# Singleton
_cache: Optional[IntentCache] = None


def get_intent_cache() -> IntentCache:
    global _cache
    if _cache is None:
        _cache = IntentCache()
    return _cache
