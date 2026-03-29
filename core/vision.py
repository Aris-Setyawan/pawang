"""Vision Analysis — analyze images using vision-capable models.

Supports Google Gemini and OpenAI GPT-4o vision APIs.
Falls back through providers until one succeeds.
"""

import base64
import httpx
from typing import Optional

from core.logger import log


# Vision-capable models by provider (priority order)
VISION_MODELS = {
    "google": "gemini-2.0-flash",
    "openai": "gpt-4o-mini",
    "openrouter": "google/gemini-2.0-flash-exp:free",
}


async def analyze_image(
    config,
    image_data: bytes,
    prompt: str = "Describe this image in detail.",
    mime_type: str = "image/jpeg",
) -> Optional[str]:
    """Analyze an image using a vision-capable model.

    Tries providers in order: google > openai > openrouter.
    Returns analysis text or None on failure.
    """
    for provider_name, model in VISION_MODELS.items():
        prov = config.get_provider(provider_name)
        if not prov or not prov.api_key:
            continue

        try:
            if prov.api_format == "gemini":
                return await _analyze_gemini(prov, model, image_data, prompt, mime_type)
            elif prov.api_format == "openai":
                return await _analyze_openai(prov, model, image_data, prompt, mime_type)
            else:
                # OpenRouter and others use OpenAI format
                return await _analyze_openai(prov, model, image_data, prompt, mime_type)
        except Exception as e:
            log.warning(f"Vision analysis failed with {provider_name}: {e}")
            continue

    return None


async def _analyze_gemini(prov, model: str, image_data: bytes,
                          prompt: str, mime_type: str) -> str:
    """Analyze image via Google Gemini API."""
    b64 = base64.b64encode(image_data).decode("utf-8")

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime_type, "data": b64}},
            ]
        }],
        "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.4},
    }

    url = f"{prov.base_url}/v1beta/models/{model}:generateContent?key={prov.api_key}"

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()

    candidates = data.get("candidates", [])
    if not candidates:
        return "(No response from vision model)"

    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts) or "(Empty response)"


async def _analyze_openai(prov, model: str, image_data: bytes,
                          prompt: str, mime_type: str) -> str:
    """Analyze image via OpenAI-compatible API (GPT-4o, OpenRouter)."""
    b64 = base64.b64encode(image_data).decode("utf-8")
    data_url = f"data:{mime_type};base64,{b64}"

    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url, "detail": "auto"}},
            ],
        }],
        "max_tokens": 2048,
        "temperature": 0.4,
    }

    headers = {"Authorization": f"Bearer {prov.api_key}", "Content-Type": "application/json"}
    url = f"{prov.base_url}/chat/completions"

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    choices = data.get("choices", [])
    if not choices:
        return "(No response from vision model)"

    return choices[0].get("message", {}).get("content", "(Empty response)")
