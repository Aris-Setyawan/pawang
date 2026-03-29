"""Context Compressor — LLM-powered conversation summarization.

When a session approaches the context limit, older messages are summarized
into a compact summary that preserves key information while reducing tokens.
"""

from core.logger import log
from providers.base import Message


# Template for the summarization prompt
SUMMARY_PROMPT = """Summarize this conversation concisely. Preserve:
1. Key facts and decisions made
2. User preferences and requests
3. Current task progress and next steps
4. Important context that would be needed to continue

Be brief but complete. Use bullet points. Write in the same language as the conversation."""


async def compress_context(
    config,
    provider_name: str,
    model: str,
    messages: list[Message],
    max_tokens: int = 100000,
    keep_recent: int = 6,
) -> list[Message]:
    """Compress conversation when approaching context limit.

    Strategy:
    - Keep system message (first)
    - Keep last `keep_recent` messages (recent context)
    - Summarize everything in between with LLM
    - Replace summarized messages with a single summary message

    Returns new message list (shorter).
    """
    from core import completion

    # Estimate current tokens
    total_tokens = sum(len(m.content) for m in messages) // 4
    if total_tokens < max_tokens * 0.8:
        return messages  # Not near limit, no compression needed

    log.info(f"Context compression triggered: ~{total_tokens} tokens (limit={max_tokens})")

    # Separate messages
    system_msgs = [m for m in messages if m.role == "system"]
    non_system = [m for m in messages if m.role != "system"]

    if len(non_system) <= keep_recent + 2:
        return messages  # Too few messages to compress

    # Split: old messages to summarize, recent to keep
    to_summarize = non_system[:-keep_recent]
    to_keep = non_system[-keep_recent:]

    # Build summarization request
    convo_text = "\n".join(
        f"[{m.role}]: {m.content[:500]}" for m in to_summarize
    )

    summary_messages = [
        Message(role="system", content=SUMMARY_PROMPT),
        Message(role="user", content=f"Conversation to summarize ({len(to_summarize)} messages):\n\n{convo_text}"),
    ]

    try:
        response = await completion.complete(
            config=config,
            provider_name=provider_name,
            model=model,
            messages=summary_messages,
            temperature=0.3,
            max_tokens=1000,
        )
        summary = response.text or ""
    except Exception as e:
        log.error(f"Context compression failed: {e}")
        # Fallback: just truncate old messages
        return system_msgs + non_system[-keep_recent * 2:]

    if not summary:
        return system_msgs + non_system[-keep_recent * 2:]

    # Build compressed message list
    summary_msg = Message(
        role="system",
        content=f"[Conversation Summary — {len(to_summarize)} earlier messages compressed]\n{summary}",
    )

    compressed = system_msgs + [summary_msg] + to_keep
    new_tokens = sum(len(m.content) for m in compressed) // 4
    log.info(f"Context compressed: {total_tokens} -> ~{new_tokens} tokens "
             f"({len(to_summarize)} messages summarized)")

    return compressed
