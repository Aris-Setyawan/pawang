"""Local Inference — fallback responses when no API provider is available.

Tiered fallback: cache -> knowledge base -> templates -> honest fallback.
Enables Pawang to respond even without API keys.
"""

import re
from pathlib import Path
from typing import Optional

import yaml

from core.logger import log

_TEMPLATES_PATH = Path(__file__).parent.parent / "prompts" / "templates.yaml"
_templates: Optional[dict] = None


def _load_templates() -> dict:
    global _templates
    if _templates is None:
        if _TEMPLATES_PATH.exists():
            _templates = yaml.safe_load(_TEMPLATES_PATH.read_text()) or {}
        else:
            _templates = {}
    return _templates


def _match_template(query: str) -> Optional[str]:
    """Match query against template patterns."""
    templates = _load_templates()
    q = query.lower().strip()

    for entry in templates.get("patterns", []):
        triggers = entry.get("triggers", [])
        for trigger in triggers:
            if trigger in q:
                return entry.get("response", "")
    return None


def generate(query: str, agent_id: str = "", user_id: str = "",
             agent_name: str = "") -> Optional[str]:
    """Generate response locally without API call.

    Tiers:
    1. Exact cache hit
    2. Knowledge base match (confidence >= 0.6)
    3. Template response
    4. Honest fallback
    """
    name = agent_name or "Pawang"

    # Tier 1: Response cache (exact match)
    try:
        from core.response_cache import get_response_cache
        cache = get_response_cache()
        cached = cache.lookup(query, agent_id)
        if cached:
            log.info(f"Local inference: cache hit for '{query[:40]}...'")
            return cached
    except Exception:
        pass

    # Tier 2: Knowledge base (high confidence)
    try:
        from core.knowledge import get_knowledge_base
        kb = get_knowledge_base()
        results = kb.search(query, limit=1, min_confidence=0.6)
        if results:
            entry = results[0]
            kb.reinforce(entry["id"], positive=True)
            confidence = entry["confidence"]
            answer = entry["answer"]
            if confidence >= 0.8:
                log.info(f"Local inference: knowledge hit (conf={confidence:.2f})")
                return answer
            else:
                log.info(f"Local inference: knowledge match (conf={confidence:.2f})")
                return f"{answer}\n\n_[Jawaban dari knowledge base — mungkin perlu verifikasi]_"
    except Exception:
        pass

    # Tier 3: Template responses
    template = _match_template(query)
    if template:
        log.info(f"Local inference: template match for '{query[:40]}...'")
        return template.replace("{name}", name)

    # Tier 4: Honest fallback
    q = query.lower()
    greetings = ["halo", "hai", "hi", "hello", "hey", "selamat pagi", "selamat siang",
                 "selamat malam", "good morning", "good night", "assalamualaikum"]
    if any(g in q for g in greetings):
        return f"Halo! Saya {name}. Saat ini saya dalam mode offline, tapi tetap bisa bantu untuk pertanyaan yang pernah kita diskusikan sebelumnya."

    return (
        f"Maaf, saya ({name}) sedang tidak bisa mengakses AI provider. "
        f"Coba lagi nanti, atau tanya pertanyaan yang pernah kita bahas — "
        f"saya punya catatan dari percakapan sebelumnya."
    )


def is_available() -> bool:
    """Check if any API provider has a valid key."""
    try:
        from core.config import get_config
        config = get_config()
        for name, prov in config.providers.items():
            if prov.api_key and len(prov.api_key) > 5:
                return True
    except Exception:
        pass
    return False
