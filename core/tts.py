"""Text-to-Speech — generate audio with fallback chain."""

import base64
import os
import tempfile
import wave

import httpx

from core.config import get_config
from core.logger import log


def _get_key(name: str) -> str:
    config = get_config()
    prov = config.get_provider(name)
    return prov.api_key if prov else ""


async def tts_google(text: str, voice: str = "Aoede") -> str:
    """Google Gemini TTS. Returns path to audio file."""
    api_key = _get_key("google")
    if not api_key:
        raise RuntimeError("Gemini key not configured")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent?key={api_key}",
            json={
                "contents": [{"parts": [{"text": text}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
                },
            },
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Gemini TTS HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    candidates = data.get("candidates", [{}])
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []

    for p in parts:
        if "inlineData" in p:
            raw = base64.b64decode(p["inlineData"]["data"])
            wav_path = tempfile.mktemp(suffix=".wav")
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes(raw)

            # Convert to ogg if ffmpeg available
            ogg_path = wav_path.replace(".wav", ".ogg")
            ret = os.system(f'ffmpeg -y -i "{wav_path}" -c:a libopus "{ogg_path}" -loglevel quiet 2>/dev/null')
            os.unlink(wav_path)
            if ret == 0 and os.path.exists(ogg_path):
                return ogg_path

            # Fallback: return wav renamed
            return wav_path

    raise RuntimeError("No audio in Gemini TTS response")


async def tts_openai(text: str, voice: str = "nova") -> str:
    """OpenAI TTS. Returns path to audio file."""
    api_key = _get_key("openai")
    if not api_key:
        raise RuntimeError("OpenAI key not configured")

    out_path = tempfile.mktemp(suffix=".ogg")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "tts-1", "input": text, "voice": voice, "response_format": "opus"},
        )

    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI TTS HTTP {resp.status_code}: {resp.text[:200]}")

    with open(out_path, "wb") as f:
        f.write(resp.content)

    if os.path.getsize(out_path) > 0:
        return out_path

    os.unlink(out_path)
    raise RuntimeError("Empty audio from OpenAI TTS")


async def text_to_speech(text: str, voice: str = "Aoede") -> str:
    """TTS with fallback: Google → OpenAI. Returns path to audio file.

    Caller is responsible for deleting the file after use.
    """
    errors = []

    try:
        path = await tts_google(text, voice)
        log.info(f"TTS (Google): {os.path.getsize(path)} bytes")
        return path
    except Exception as e:
        errors.append(f"Google: {e}")
        log.warning(f"TTS Google failed: {e}")

    # Map Google voice to OpenAI
    voice_map = {"Aoede": "nova", "Kore": "nova", "Charon": "onyx", "Puck": "fable"}
    oai_voice = voice_map.get(voice, "nova")

    try:
        path = await tts_openai(text, oai_voice)
        log.info(f"TTS (OpenAI): {os.path.getsize(path)} bytes")
        return path
    except Exception as e:
        errors.append(f"OpenAI: {e}")
        log.warning(f"TTS OpenAI failed: {e}")

    raise RuntimeError(f"All TTS providers failed: {'; '.join(errors)}")
