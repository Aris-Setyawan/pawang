"""OpenAI-compatible provider — works for OpenAI, DeepSeek, OpenRouter.

Thinking support:
  - DeepSeek Reasoner: outputs reasoning_content in response
  - OpenAI o-series: auto reasoning, no extra params
  - Anthropic via OpenRouter: passes thinking params
"""

import json
from typing import AsyncIterator

import httpx

from .base import (
    BaseProvider, CompletionRequest, CompletionResponse, CompletionChunk, ToolCall,
)
from core.logger import log


# Models that are reasoning models (auto-detect)
_REASONING_MODELS = {"o1", "o3", "o3-mini", "o4-mini", "deepseek-reasoner", "glm-5"}
# Models supporting Anthropic-style thinking via OpenRouter
_ANTHROPIC_MODELS_PREFIX = ("anthropic/claude-opus", "anthropic/claude-sonnet")


class OpenAIProvider(BaseProvider):
    """Handles OpenAI chat/completions API format.

    Works with: OpenAI, DeepSeek, OpenRouter, any OpenAI-compatible API.
    Thinking support: DeepSeek reasoner, OpenAI o-series, Anthropic via OpenRouter.
    """

    def build_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _is_reasoning_model(self, model: str) -> bool:
        """Check if model is a reasoning model (uses max_completion_tokens)."""
        base = model.split("/")[-1]  # strip provider prefix
        if base in _REASONING_MODELS:
            return True
        if base.startswith(("o1", "o3", "o4")):
            return True
        # GPT-5+ series all use max_completion_tokens
        if base.startswith("gpt-5"):
            return True
        return False

    def _is_deepseek_reasoner(self, model: str) -> bool:
        return "deepseek-reasoner" in model

    def _is_anthropic_via_openrouter(self, model: str) -> bool:
        return model.startswith(_ANTHROPIC_MODELS_PREFIX)

    def _build_body(self, request: CompletionRequest) -> dict:
        messages = []
        for m in request.messages:
            if m.role == "tool":
                msg = {"role": "tool", "content": m.content,
                       "tool_call_id": m.tool_call_id}
                if m.name:
                    msg["name"] = m.name
            elif m.role == "assistant" and m.tool_calls:
                msg = {"role": "assistant",
                       "content": m.content if m.content else None,
                       "tool_calls": m.tool_calls}
            else:
                msg = {"role": m.role, "content": m.content}
            messages.append(msg)

        body = {
            "model": request.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": request.stream,
        }

        if request.tools:
            body["tools"] = request.tools
            body["tool_choice"] = "auto"

        # --- Reasoning model adjustments ---
        if self._is_reasoning_model(request.model):
            body.pop("temperature", None)
            body["max_completion_tokens"] = body.pop("max_tokens")

        # --- DeepSeek thinking mode ---
        if self._is_deepseek_reasoner(request.model):
            body.pop("temperature", None)

        # --- Thinking config ---
        if request.thinking and request.thinking.enabled:
            # Anthropic via OpenRouter
            if self._is_anthropic_via_openrouter(request.model):
                body["thinking"] = {"type": "adaptive"}
                effort = request.thinking.effort or "high"
                body["output_config"] = {"effort": effort}
                # Thinking needs higher max_tokens
                if body.get("max_tokens", 0) < 16000:
                    body["max_tokens"] = 16000
                log.info(f"Thinking enabled (Anthropic via OR): effort={effort}")

            # DeepSeek thinking mode on non-reasoner models
            elif "deepseek" in request.model and not self._is_deepseek_reasoner(request.model):
                body["thinking"] = {"type": "enabled"}
                log.info("Thinking enabled (DeepSeek)")

        return body

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        request.stream = False
        body = self._build_body(request)

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self.build_headers(),
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        choices = data.get("choices", [])
        if not choices:
            return CompletionResponse(text="", model=request.model, finish_reason="error")
        choice = choices[0]
        msg = choice.get("message", {})

        # Extract reasoning content (DeepSeek Reasoner)
        thinking_text = msg.get("reasoning_content", "")

        # Extract thinking blocks (Anthropic via OpenRouter)
        if not thinking_text and isinstance(msg.get("content"), list):
            parts = []
            thinking_parts = []
            for block in msg["content"]:
                if block.get("type") == "thinking":
                    thinking_parts.append(block.get("thinking", ""))
                elif block.get("type") == "text":
                    parts.append(block.get("text", ""))
            thinking_text = "\n".join(thinking_parts)
            text = "\n".join(parts)
        else:
            text = msg.get("content", "") or ""

        # Extract tool_calls
        tool_calls = []
        for tc in msg.get("tool_calls", []):
            tc_id = tc.get("id", "")
            func = tc.get("function", {})
            if not tc_id or not func.get("name"):
                continue
            tool_calls.append(ToolCall(
                id=tc_id,
                name=func["name"],
                arguments=func.get("arguments", "{}"),
            ))

        return CompletionResponse(
            text=text,
            thinking_text=thinking_text,
            model=data.get("model", request.model),
            finish_reason=choice.get("finish_reason", "stop"),
            usage=data.get("usage", {}),
            tool_calls=tool_calls,
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        request.stream = True
        body = self._build_body(request)

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self.build_headers(),
                json=body,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload.strip() == "[DONE]":
                        return
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    choices = data.get("choices", [])
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta", {})

                    # Regular content
                    text = delta.get("content", "")

                    # DeepSeek reasoning content (streamed)
                    thinking = delta.get("reasoning_content", "")

                    # Anthropic thinking blocks via OpenRouter
                    if not thinking and delta.get("type") == "thinking":
                        thinking = delta.get("thinking", "")

                    if text or thinking:
                        yield CompletionChunk(
                            text=text or "",
                            thinking_text=thinking or "",
                            finish_reason=choice.get("finish_reason"),
                            model=data.get("model", request.model),
                        )
