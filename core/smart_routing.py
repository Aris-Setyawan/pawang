"""Smart Model Routing — route simple queries to cheaper models.

Saves ~70% cost by detecting simple messages (greetings, short questions)
and routing them to a fast/cheap model instead of the primary.
"""

import re
from typing import Optional
from core.logger import log

# Keywords that indicate complex tasks → always use primary model
_COMPLEX_KEYWORDS = {
    "debug", "implement", "refactor", "patch", "traceback", "exception",
    "error", "analyze", "investigate", "architecture", "design", "compare",
    "benchmark", "optimize", "review", "terminal", "pytest", "test",
    "plan", "delegate", "docker", "kubernetes", "deploy", "migrate",
    "coding", "code", "script", "function", "class", "database",
    "sql", "api", "server", "config", "install", "setup",
    "buatkan", "buat", "tulis", "perbaiki", "jelaskan", "analisa",
}

# Max thresholds for "simple" messages
MAX_SIMPLE_CHARS = 160
MAX_SIMPLE_WORDS = 28


def is_simple_message(text: str) -> bool:
    """Check if a message is simple enough to route to a cheap model.

    Conservative: only routes if message is clearly simple.
    """
    if not text:
        return False

    text = text.strip()

    # Length checks
    if len(text) > MAX_SIMPLE_CHARS:
        return False
    if len(text.split()) > MAX_SIMPLE_WORDS:
        return False

    # Multi-line = complex
    if text.count("\n") > 1:
        return False

    # Code blocks = complex
    if "```" in text or "`" in text:
        return False

    # URLs = complex
    if "http://" in text or "https://" in text or "www." in text:
        return False

    # Check for complex keywords
    words = set(re.findall(r'\w+', text.lower()))
    if words & _COMPLEX_KEYWORDS:
        return False

    return True


def get_cheap_route(config, primary_provider: str, primary_model: str) -> Optional[tuple[str, str]]:
    """Get the cheap model route from config.

    Returns (provider, model) for cheap routing, or None if not configured.
    """
    routing = getattr(config, 'smart_routing', None)
    if not routing or not routing.get('enabled', False):
        return None

    provider = routing.get('cheap_provider', '')
    model = routing.get('cheap_model', '')

    if not provider or not model:
        return None

    # Don't route to same model
    if provider == primary_provider and model == primary_model:
        return None

    # Verify provider exists and has key
    prov = config.get_provider(provider)
    if not prov or not prov.api_key:
        return None

    return provider, model


def route_message(config, text: str, primary_provider: str, primary_model: str) -> tuple[str, str, bool]:
    """Route a message to the appropriate model.

    Returns (provider, model, is_routed).
    is_routed=True means cheap model was selected.
    """
    if is_simple_message(text):
        cheap = get_cheap_route(config, primary_provider, primary_model)
        if cheap:
            log.info(f"Smart routing: simple message -> {cheap[0]}/{cheap[1]}")
            return cheap[0], cheap[1], True

    return primary_provider, primary_model, False
