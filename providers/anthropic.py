"""Anthropic provider — Claude API format."""

import json
from typing import AsyncIterator

import httpx

from .base import (
    BaseProvider, CompletionRequest, CompletionResponse, CompletionChunk,
)
from core.logger import log


class AnthropicProvider(BaseProvider):
    """Handles Anthropic Messages API format."""

    def build_headers(self) -> dict:
        headers = {
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        # OAuth tokens (sk-ant-oat) use Bearer auth, API keys use x-api-key
        if self.api_key.startswith("sk-ant-oat"):
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            headers["x-api-key"] = self.api_key
        return headers

    def _apply_prompt_caching(self, body: dict) -> dict:
        """Apply cache_control markers to reduce input token costs.

        Strategy: system_and_3 — cache system prompt + last 3 messages.
        Reduces repeat context by ~75% on multi-turn conversations.
        """
        marker = {"type": "ephemeral"}

        # Cache system prompt
        if "system" in body and body["system"]:
            body["system"] = [
                {"type": "text", "text": body["system"], "cache_control": marker}
            ]

        # Cache last 3 non-empty messages
        msgs = body.get("messages", [])
        cached = 0
        for msg in reversed(msgs):
            if cached >= 3:
                break
            content = msg.get("content", "")
            if not content:
                continue
            if isinstance(content, str):
                msg["content"] = [
                    {"type": "text", "text": content, "cache_control": marker}
                ]
            elif isinstance(content, list) and content:
                content[-1]["cache_control"] = marker
            cached += 1

        return body

    def _build_body(self, request: CompletionRequest) -> dict:
        # Anthropic separates system from messages
        system_text = ""
        messages = []
        for m in request.messages:
            if m.role == "system":
                system_text = m.content
            else:
                messages.append({"role": m.role, "content": m.content})

        body = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": request.stream,
        }
        if system_text:
            body["system"] = system_text

        # Apply prompt caching for cost reduction
        body = self._apply_prompt_caching(body)
        return body

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        request.stream = False
        body = self._build_body(request)

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/messages",
                headers=self.build_headers(),
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        return CompletionResponse(
            text=text,
            model=data.get("model", request.model),
            finish_reason=data.get("stop_reason", "end_turn"),
            usage=data.get("usage", {}),
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        request.stream = True
        body = self._build_body(request)

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/messages",
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

                    event_type = data.get("type", "")
                    if event_type == "content_block_delta":
                        delta = data.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield CompletionChunk(
                                text=delta["text"],
                                model=request.model,
                            )
                    elif event_type == "message_delta":
                        yield CompletionChunk(
                            text="",
                            finish_reason=data.get("delta", {}).get("stop_reason"),
                            model=request.model,
                            usage=data.get("usage", {}),
                        )
