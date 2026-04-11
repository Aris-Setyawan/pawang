"""Smart Model Routing — route simple queries to cheaper models.

Saves ~70% cost by detecting simple messages (greetings, short questions)
and routing them to a fast/cheap model instead of the primary.
"""

import re
from typing import Optional
from core.logger import log

# Level 1: PASTI butuh tools — selalu masuk tool-capable model
_TOOL_KEYWORDS = {
    # Delegation / orchestration
    "delegate", "delegasi",
    # Creative generation — butuh tool generate_image/video/audio
    "gambar", "image", "video", "audio", "musik", "music",
    "foto", "photo", "picture", "draw", "render", "lukis",
    "generate", "generasi",
    # Coding — butuh delegate ke agent lain
    "coding", "code", "script", "debug", "refactor", "implement",
    "patch", "traceback", "exception", "pytest",
    "architecture", "deploy", "migrate", "docker", "kubernetes",
    # File / system ops — butuh run_bash, file_read, etc
    "terminal", "bash", "pip", "npm",
    # Explicit task requests
    "buatkan", "buatin", "bikinin",
    # Skill hub
    "skill", "clawhub", "hermes",
    # Install / execute — butuh tool skill_hub atau run_bash
    "install", "uninstall",
    "jalankan", "jalanin", "eksekusi", "execute", "run",
    # Balance / monitoring — butuh tool check_balances
    "balance", "saldo",
    # Google Workspace — butuh gog tools
    "email", "gmail", "inbox", "unread",
    "calendar", "jadwal", "schedule", "event",
    "sheets", "spreadsheet",
}

# Level 2: MUNGKIN butuh tools — hanya complex jika dikombinasi
# dengan kata kerja perintah atau kalimat panjang (>6 kata)
_SOFT_KEYWORDS = {
    "buat", "bikin", "tolong", "cari", "carikan", "search",
    "tulis", "tuliskan", "perbaiki", "benerin", "fixkan",
    "jelaskan", "jelasin", "analisa", "analyze", "investigate",
    "hapus", "ubah", "ganti", "tambah", "tambahin",
    "kerjakan", "kerjain", "selesaikan", "selesain",
    "download", "unduh", "setup",
    "compare", "benchmark", "optimize", "review",
    "database", "sql", "api", "server", "config",
    "function", "class", "cli", "test", "plan",
    "design", "monitor", "error",
    "cek", "update",
}

# Fuzzy targets: keywords worth fuzzy-matching (high-value tool triggers)
# Only keywords where typos are common and misrouting is costly
_FUZZY_TARGETS = [
    "install", "uninstall", "balance", "generate", "delegate",
    "jalankan", "jalanin", "eksekusi", "execute",
    "gambar", "video", "audio", "skill",
]


def _fuzzy_match_keywords(words: set[str]) -> bool:
    """Check if any word is a close match to a tool keyword (Levenshtein ≤ 2).

    Only checks words with 4+ chars against high-value keywords to avoid
    false positives on short words.
    """
    for word in words:
        if len(word) < 4:
            continue
        for target in _FUZZY_TARGETS:
            if len(target) < 4:
                continue
            # Quick prefix check: if word starts with first 3 chars of target
            if word[:3] == target[:3] and abs(len(word) - len(target)) <= 2:
                # Simple Levenshtein distance check
                if _lev_distance(word, target) <= 2:
                    return True
    return False


def _lev_distance(a: str, b: str) -> int:
    """Compute Levenshtein distance between two strings."""
    if len(a) < len(b):
        return _lev_distance(b, a)
    if len(b) == 0:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(
                prev[j + 1] + 1,      # deletion
                curr[j] + 1,           # insertion
                prev[j] + (ca != cb),  # substitution
            ))
        prev = curr
    return prev[len(b)]


# Max thresholds for "simple" messages
MAX_SIMPLE_CHARS = 200
MAX_SIMPLE_WORDS = 35


def is_simple_message(text: str) -> bool:
    """Check if a message is simple enough to route to a cheap model.

    Two-level keyword system:
    - _TOOL_KEYWORDS: always complex (definitely needs tools)
    - _SOFT_KEYWORDS: only complex if message has >6 words (indicates a task, not a question)
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

    words = set(re.findall(r'\w+', text.lower()))
    word_count = len(text.split())

    # Level 1: hard keywords → always complex
    if words & _TOOL_KEYWORDS:
        log.debug(f"Routing: COMPLEX (tool keyword) — {text[:60]}")
        return False

    # Level 1b: fuzzy match — catch common typos for tool keywords
    # Check if any word in the message is a close prefix/substring of a tool keyword
    if _fuzzy_match_keywords(words):
        log.debug(f"Routing: COMPLEX (fuzzy keyword) — {text[:60]}")
        return False

    # Level 2: soft keywords → only complex if looks like a command (>6 words)
    if words & _SOFT_KEYWORDS and word_count > 6:
        log.debug(f"Routing: COMPLEX (soft keyword + long) — {text[:60]}")
        return False

    log.debug(f"Routing: SIMPLE — {text[:60]}")
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
