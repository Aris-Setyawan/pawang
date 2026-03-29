"""Learning Loop — extract knowledge from conversations automatically.

Rule-based extraction (zero API cost) + optional LLM batch extraction.
Hooks into message:sent events for real-time learning.
"""

import re
import time
from typing import Optional

from core.logger import log

# Question indicators
_QUESTION_PATTERNS = re.compile(
    r'\b(apa|bagaimana|kenapa|mengapa|siapa|kapan|dimana|berapa|'
    r'what|how|why|who|when|where|which|could|can|does|is)\b',
    re.I
)

# Correction indicators
_CORRECTION_PATTERNS = re.compile(
    r'\b(bukan|salah|wrong|no[, ]|incorrect|actually|sebenarnya|'
    r'maksud(nya|ku)|i meant|koreksi)\b',
    re.I
)

# Instruction patterns
_INSTRUCTION_PATTERNS = re.compile(
    r'\b(buatkan|buat|create|make|generate|write|tulis|tolong|please|help)\b',
    re.I
)

# Factual patterns in assistant responses
_FACT_PATTERNS = [
    re.compile(r'(.{10,50})\s+(adalah|berarti|artinya|means?|refers? to)\s+(.{10,200})', re.I),
    re.compile(r'(.{10,50})\s+(is|are|was|were)\s+(a|an|the)\s+(.{10,200})', re.I),
]


def extract_learnings(user_msg: str, assistant_msg: str,
                      agent_id: str = "", session_key: str = "",
                      user_id: str = "") -> list[dict]:
    """Rule-based knowledge extraction from a conversation turn.

    Returns list of dicts: [{question, answer, category, confidence, tags}]
    Zero API cost — uses regex patterns only.
    """
    learnings = []

    # Skip very short or tool-heavy responses
    if len(assistant_msg) < 50 or len(user_msg) < 5:
        return learnings

    # Skip responses that are clearly tool outputs
    if assistant_msg.startswith("[") or "```" in assistant_msg[:20]:
        return learnings

    # 1. Q&A pair detection
    is_question = (
        user_msg.rstrip().endswith("?") or
        bool(_QUESTION_PATTERNS.search(user_msg))
    )
    if is_question and len(assistant_msg) > 80:
        learnings.append({
            "question": user_msg.strip(),
            "answer": assistant_msg[:2000].strip(),
            "category": "qa",
            "confidence": 0.5,
            "tags": "auto-extracted,qa",
        })

    # 2. Factual statement extraction from assistant response
    for pattern in _FACT_PATTERNS:
        matches = pattern.findall(assistant_msg)
        for match in matches[:3]:  # Max 3 facts per response
            subject = match[0].strip() if isinstance(match, tuple) else match
            full_match = " ".join(match) if isinstance(match, tuple) else match
            if len(full_match) > 20:
                learnings.append({
                    "question": subject,
                    "answer": full_match[:500],
                    "category": "fact",
                    "confidence": 0.4,
                    "tags": "auto-extracted,fact",
                })

    # 3. Instruction-response pattern
    if _INSTRUCTION_PATTERNS.search(user_msg) and len(assistant_msg) > 100:
        learnings.append({
            "question": user_msg.strip(),
            "answer": assistant_msg[:2000].strip(),
            "category": "instruction",
            "confidence": 0.3,
            "tags": "auto-extracted,instruction",
        })

    return learnings


def process_correction(user_msg: str, previous_answer: str, user_id: str = ""):
    """Handle user corrections — negatively reinforce matching knowledge."""
    if not _CORRECTION_PATTERNS.search(user_msg):
        return

    try:
        from core.knowledge import get_knowledge_base
        kb = get_knowledge_base()
        # Search for the previous answer in knowledge base
        results = kb.search(previous_answer[:100], limit=3, min_confidence=0.0)
        for entry in results:
            # If the answer matches, reduce confidence
            if entry["answer"][:100] in previous_answer or previous_answer[:100] in entry["answer"]:
                kb.reinforce(entry["id"], positive=False)
                log.info(f"Correction: reduced confidence for knowledge #{entry['id']}")
    except Exception as e:
        log.warning(f"Correction processing failed: {e}")


def store_learnings(learnings: list[dict], session_key: str = "",
                    user_id: str = "", agent_id: str = ""):
    """Store extracted learnings in the knowledge base."""
    if not learnings:
        return

    try:
        from core.knowledge import get_knowledge_base
        kb = get_knowledge_base()
        stored = 0
        for entry in learnings:
            kb.store(
                question=entry["question"],
                answer=entry["answer"],
                category=entry.get("category", "qa"),
                confidence=entry.get("confidence", 0.5),
                source_session=session_key,
                source_user=user_id,
                agent_id=agent_id,
                tags=entry.get("tags", ""),
            )
            stored += 1
        if stored:
            log.info(f"Learning: stored {stored} entries from session {session_key}")
    except Exception as e:
        log.warning(f"Learning store failed: {e}")


def on_message_sent(agent_id: str, user_id: str, session_key: str,
                    user_msg: str, assistant_msg: str):
    """Hook handler for message:sent events. Runs extraction + storage."""
    learnings = extract_learnings(
        user_msg, assistant_msg, agent_id, session_key, user_id
    )
    store_learnings(learnings, session_key, user_id, agent_id)


def batch_learn_from_history(hours: int = 6):
    """Scheduled job: scan recent messages and extract knowledge."""
    from core.database import get_db

    db = get_db()
    since = time.time() - (hours * 3600)

    # Get recent conversations grouped by session
    rows = db.conn.execute(
        "SELECT session_key, agent_id, user_id, role, content "
        "FROM messages WHERE created_at > ? ORDER BY session_key, created_at",
        (since,)
    ).fetchall()

    if not rows:
        return

    # Group into turns (user + assistant pairs)
    current_session = None
    user_msg = None
    total_extracted = 0

    for row in rows:
        if row["session_key"] != current_session:
            current_session = row["session_key"]
            user_msg = None

        if row["role"] == "user":
            user_msg = row["content"]
        elif row["role"] == "assistant" and user_msg:
            learnings = extract_learnings(
                user_msg, row["content"],
                row["agent_id"], row["session_key"], row["user_id"]
            )
            store_learnings(
                learnings, row["session_key"], row["user_id"], row["agent_id"]
            )
            total_extracted += len(learnings)
            user_msg = None

    if total_extracted:
        log.info(f"Batch learning: extracted {total_extracted} entries from {hours}h history")
