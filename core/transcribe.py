"""Speech-to-Text — transcribe voice messages with fallback chain."""

import asyncio
import base64
import json
import os
import tempfile
from pathlib import Path

import httpx

from core.config import get_config
from core.logger import log

ENV_FILE = Path(__file__).parent.parent / ".env"


def _get_key(name: str) -> str:
    """Get API key from config providers."""
    config = get_config()
    prov = config.get_provider(name)
    return prov.api_key if prov else ""


async def transcribe_openai(audio_path: str) -> str:
    """Transcribe using OpenAI Whisper API."""
    api_key = _get_key("openai")
    if not api_key:
        raise RuntimeError("OpenAI key not configured")

    async with httpx.AsyncClient(timeout=30) as client:
        with open(audio_path, "rb") as f:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (Path(audio_path).name, f, "audio/ogg")},
                data={"model": "whisper-1"},
            )

    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI Whisper HTTP {resp.status_code}: {resp.text[:200]}")

    return resp.json().get("text", "").strip()


async def transcribe_gemini(audio_path: str) -> str:
    """Transcribe using Gemini (multimodal audio input)."""
    api_key = _get_key("google")
    if not api_key:
        raise RuntimeError("Gemini key not configured")

    with open(audio_path, "rb") as f:
        audio_data = base64.b64encode(f.read()).decode()

    # Detect mime type
    ext = Path(audio_path).suffix.lower()
    mime_map = {".ogg": "audio/ogg", ".mp3": "audio/mp3", ".wav": "audio/wav",
                ".oga": "audio/ogg", ".m4a": "audio/mp4"}
    mime_type = mime_map.get(ext, "audio/ogg")

    payload = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": mime_type, "data": audio_data}},
                {"text": "Transcribe this audio exactly. Output ONLY the transcription, nothing else."},
            ]
        }],
        "generationConfig": {"temperature": 0},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}",
            json=payload,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    candidates = data.get("candidates", [{}])
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    return parts[0].get("text", "").strip() if parts else ""


async def transcribe(audio_path: str) -> str:
    """Transcribe audio file with fallback: OpenAI Whisper → Gemini.

    Returns transcribed text.
    Raises RuntimeError if all providers fail.
    """
    errors = []

    # Try OpenAI Whisper first (most accurate for speech)
    try:
        text = await transcribe_openai(audio_path)
        if text:
            log.info(f"STT (OpenAI): {len(text)} chars")
            return text
    except Exception as e:
        errors.append(f"OpenAI: {e}")
        log.warning(f"STT OpenAI failed: {e}")

    # Fallback to Gemini
    try:
        text = await transcribe_gemini(audio_path)
        if text:
            log.info(f"STT (Gemini): {len(text)} chars")
            return text
    except Exception as e:
        errors.append(f"Gemini: {e}")
        log.warning(f"STT Gemini failed: {e}")

    raise RuntimeError(f"All STT providers failed: {'; '.join(errors)}")
