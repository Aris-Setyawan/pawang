"""Intent Classifier — determines what user wants when a task is running.

Uses a fast model with high thinking effort to classify user messages
into: stop, info, modify, new_task, continue.
"""

import re
from core.config import PawangConfig
from core.logger import log
from core import completion
from providers.base import Message


# Keywords for fast local classification (no API call needed)
STOP_KEYWORDS = {
    "stop", "cancel", "berhenti", "hentikan", "pause", "jeda",
    "tunggu", "wait", "abort", "batalkan", "sudah", "cukup",
    "udah", "dah", "stahp",
}

CONTINUE_KEYWORDS = {
    "lanjut", "continue", "resume", "teruskan", "lanjutkan", "gas",
    "go", "oke lanjut", "ok lanjut",
}

CLASSIFY_PROMPT = """You are an intent classifier. A user sent a message while a task is running.
Classify the intent into exactly ONE of these categories:

- STOP: User wants to stop/pause/cancel the current task
- INFO: User is asking a side question or wants information, task should keep running
- MODIFY: User wants to change/adjust the current running task
- NEW_TASK: User is giving a completely new task/instruction
- CONTINUE: User wants to resume a paused task

Current running task: "{task_prompt}"
User message: "{user_message}"

Reply with ONLY the category name (STOP, INFO, MODIFY, NEW_TASK, or CONTINUE). Nothing else."""


async def classify_intent(
    config: PawangConfig,
    user_message: str,
    task_prompt: str = "",
) -> str:
    """Classify user intent. Returns: stop, info, modify, new_task, continue."""

    text = user_message.lower().strip()

    # Fast local classification for obvious cases
    words = set(re.split(r'\s+', text))

    if words & STOP_KEYWORDS and len(words) <= 3:
        return "stop"

    if words & CONTINUE_KEYWORDS and len(words) <= 3:
        return "continue"

    # For ambiguous messages, use AI classifier
    # Pick fastest available model
    fast_providers = [
        ("openai", "gpt-5.4-mini"),
        ("google", "gemini-2.5-flash"),
        ("sumopod", "gpt-5-mini"),
    ]

    provider_name = None
    model_name = None
    for pname, mname in fast_providers:
        prov = config.get_provider(pname)
        if prov and prov.api_key:
            provider_name = pname
            model_name = mname
            break

    if not provider_name:
        # No model available — default to info (safest)
        log.warning("No model available for intent classification")
        return "info"

    try:
        prompt = CLASSIFY_PROMPT.format(
            task_prompt=task_prompt[:200],
            user_message=user_message[:500],
        )

        response = await completion.complete(
            config=config,
            provider_name=provider_name,
            model=model_name,
            messages=[Message(role="user", content=prompt)],
            temperature=0.0,
            max_tokens=10,
        )

        result = response.text.strip().upper()

        # Map to our enum values
        mapping = {
            "STOP": "stop",
            "INFO": "info",
            "MODIFY": "modify",
            "NEW_TASK": "new_task",
            "CONTINUE": "continue",
        }

        intent = mapping.get(result, "info")
        log.info(f"Intent classified: '{user_message[:50]}' -> {intent}")
        return intent

    except Exception as e:
        log.error(f"Intent classification failed: {e}")
        return "info"  # default safe
