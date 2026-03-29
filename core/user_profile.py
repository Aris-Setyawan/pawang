"""User Profile Builder — auto-build user profiles from conversation patterns.

Analyzes message history to infer language preference, topics,
activity patterns, and communication style.
"""

import re
import time
from collections import Counter
from typing import Optional

from core.database import get_db
from core.logger import log


def build_profile(user_id: str) -> dict:
    """Build a user profile from conversation history.

    Returns dict with inferred attributes:
      - language: primary language
      - topics: top topics discussed
      - activity_hours: most active hours (UTC)
      - message_style: short/medium/long
      - total_messages: count
      - first_seen: timestamp
      - agents_used: list of agents interacted with
    """
    db = get_db()

    # Get all user messages
    rows = db.conn.execute(
        "SELECT content, agent_id, created_at FROM messages "
        "WHERE user_id = ? AND role = 'user' ORDER BY created_at DESC LIMIT 500",
        (user_id,),
    ).fetchall()

    if not rows:
        return {"user_id": user_id, "total_messages": 0}

    messages = [dict(r) for r in rows]
    total = len(messages)

    # Language detection (simple heuristic)
    language = _detect_language(messages)

    # Topic extraction
    topics = _extract_topics(messages)

    # Activity hours
    hours = Counter()
    for m in messages:
        h = int((m["created_at"] % 86400) / 3600)
        hours[h] += 1
    top_hours = [h for h, _ in hours.most_common(3)]

    # Message style
    avg_len = sum(len(m["content"]) for m in messages) / total
    if avg_len < 50:
        style = "concise"
    elif avg_len < 200:
        style = "moderate"
    else:
        style = "detailed"

    # Agents used
    agents = list(set(m["agent_id"] for m in messages if m["agent_id"]))

    # First seen
    first_seen = min(m["created_at"] for m in messages)

    return {
        "user_id": user_id,
        "language": language,
        "topics": topics[:10],
        "activity_hours_utc": top_hours,
        "message_style": style,
        "avg_message_length": int(avg_len),
        "total_messages": total,
        "first_seen": first_seen,
        "agents_used": agents,
    }


def get_profile_summary(user_id: str) -> str:
    """Get a human-readable profile summary."""
    profile = build_profile(user_id)

    if profile.get("total_messages", 0) == 0:
        return "No conversation history yet."

    lines = [f"User Profile ({user_id})", "=" * 30]
    lines.append(f"Language: {profile.get('language', 'unknown')}")
    lines.append(f"Style: {profile.get('message_style', 'unknown')} (avg {profile.get('avg_message_length', 0)} chars)")
    lines.append(f"Messages: {profile.get('total_messages', 0)}")

    topics = profile.get("topics", [])
    if topics:
        lines.append(f"Top Topics: {', '.join(topics[:5])}")

    hours = profile.get("activity_hours_utc", [])
    if hours:
        hour_strs = [f"{h:02d}:00" for h in hours]
        lines.append(f"Active Hours (UTC): {', '.join(hour_strs)}")

    agents = profile.get("agents_used", [])
    if agents:
        lines.append(f"Agents Used: {', '.join(agents)}")

    return "\n".join(lines)


def _detect_language(messages: list[dict]) -> str:
    """Simple language detection based on common words."""
    # Sample recent messages
    sample = " ".join(m["content"][:200] for m in messages[:50]).lower()

    # Indonesian indicators
    id_words = {"saya", "aku", "kamu", "ini", "itu", "yang", "dan", "di",
                "ke", "dari", "untuk", "dengan", "adalah", "bisa", "ada",
                "tidak", "sudah", "mau", "bagaimana", "apa", "tolong",
                "buat", "buatkan", "gimana", "gak", "dong", "nih"}

    # English indicators
    en_words = {"the", "is", "are", "was", "were", "have", "has", "been",
                "will", "would", "could", "should", "can", "this", "that",
                "what", "how", "please", "help", "make", "create"}

    words = set(re.findall(r'\w+', sample))
    id_score = len(words & id_words)
    en_score = len(words & en_words)

    if id_score > en_score * 1.5:
        return "Indonesian"
    elif en_score > id_score * 1.5:
        return "English"
    elif id_score > 0 and en_score > 0:
        return "Mixed (Indonesian/English)"
    return "Unknown"


def _extract_topics(messages: list[dict]) -> list[str]:
    """Extract common topics from messages using keyword frequency."""
    # Topic categories with keywords
    topic_keywords = {
        "coding": {"code", "bug", "function", "class", "error", "debug", "python", "javascript",
                    "api", "server", "deploy", "git", "database", "sql"},
        "AI/ML": {"model", "ai", "gpt", "llm", "training", "prompt", "token", "embedding",
                   "neural", "machine learning", "deep learning"},
        "creative": {"gambar", "image", "video", "audio", "musik", "music", "generate",
                      "design", "art", "foto", "photo"},
        "analysis": {"analisis", "analyze", "data", "report", "statistik", "statistics",
                      "chart", "graph", "compare"},
        "web": {"website", "web", "html", "css", "frontend", "backend", "react",
                 "browser", "url", "http"},
        "system": {"server", "linux", "docker", "systemd", "nginx", "process",
                    "memory", "cpu", "disk", "ssh"},
        "writing": {"tulis", "write", "email", "artikel", "article", "summary",
                     "translate", "terjemah"},
        "search": {"cari", "search", "find", "look", "where", "info", "tentang"},
    }

    all_text = " ".join(m["content"][:300] for m in messages).lower()
    all_words = set(re.findall(r'\w+', all_text))

    topic_scores = {}
    for topic, keywords in topic_keywords.items():
        score = len(all_words & keywords)
        if score > 0:
            topic_scores[topic] = score

    return [t for t, _ in sorted(topic_scores.items(), key=lambda x: -x[1])]
