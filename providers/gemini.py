"""Google Gemini provider — native Gemini API format.

Thinking support:
  - Gemini 2.5: thinkingConfig.thinkingBudget (-1 = dynamic, 0 = off)
  - Strips unsupported params — no google-proxy needed.
"""

import json
from typing import AsyncIterator

import httpx

from .base import (
    BaseProvider, CompletionRequest, CompletionResponse, CompletionChunk,
)
from core.logger import log


# Models that support thinking
_THINKING_MODELS = {
    "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite",
    "gemini-2.5-flash-preview",
}


class GeminiProvider(BaseProvider):
    """Handles Google Gemini generateContent API.

    Thinking support for Gemini 2.5 series via thinkingConfig.
    """

    def build_headers(self) -> dict:
        return {"Content-Type": "application/json"}

    def _supports_thinking(self, model: str) -> bool:
        return any(model.startswith(m) for m in _THINKING_MODELS)

    def _build_body(self, request: CompletionRequest) -> dict:
        contents = []
        system_text = ""
        for m in request.messages:
            if m.role == "system":
                system_text = m.content
                continue
            role = "user" if m.role == "user" else "model"
            contents.append({
                "role": role,
                "parts": [{"text": m.content}],
            })

        body = {
            "contents": contents,
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_tokens,
            },
        }
        if system_text:
            body["systemInstruction"] = {
                "parts": [{"text": system_text}],
            }

        # --- Thinking support ---
        if request.thinking and request.thinking.enabled and self._supports_thinking(request.model):
            budget = request.thinking.budget_tokens
            if budget == 0:
                budget = -1  # dynamic

            body["generationConfig"]["thinkingConfig"] = {
                "thinkingBudget": budget,
            }
            # Thinking needs higher output tokens
            if request.max_tokens < 16000:
                body["generationConfig"]["maxOutputTokens"] = 16000
            log.info(f"Thinking enabled (Gemini): budget={budget}")

        return body

    def _url(self, model: str, stream: bool) -> str:
        method = "streamGenerateContent" if stream else "generateContent"
        return f"{self.base_url}/models/{model}:{method}?key={self.api_key}"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = self._build_body(request)

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                self._url(request.model, stream=False),
                headers=self.build_headers(),
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        text = ""
        thinking_text = ""
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                if part.get("thought", False):
                    thinking_text += part.get("text", "")
                else:
                    text += part.get("text", "")

        usage = data.get("usageMetadata", {})

        return CompletionResponse(
            text=text,
            thinking_text=thinking_text,
            model=request.model,
            finish_reason=data.get("candidates", [{}])[0].get(
                "finishReason", "STOP"
            ),
            usage=usage,
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        body = self._build_body(request)
        url = self._url(request.model, stream=True) + "&alt=sse"

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                url,
                headers=self.build_headers(),
                json=body,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        data = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    for candidate in data.get("candidates", []):
                        for part in candidate.get("content", {}).get("parts", []):
                            ptext = part.get("text", "")
                            is_thought = part.get("thought", False)
                            if ptext:
                                yield CompletionChunk(
                                    text="" if is_thought else ptext,
                                    thinking_text=ptext if is_thought else "",
                                    finish_reason=candidate.get("finishReason"),
                                    model=request.model,
                                )
