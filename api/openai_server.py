"""OpenAI-Compatible API Server — /v1/chat/completions endpoint.

Allows third-party clients (Open WebUI, LobeChat, Cursor, etc.)
to use Pawang as an OpenAI-compatible proxy.
"""

import json
import time
import asyncio
from typing import AsyncIterator

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from core.config import get_config
from core.database import get_db
from core.logger import log
from core import completion
from providers.base import Message, ThinkingConfig


async def _auth_check(request: Request) -> tuple[bool, str]:
    """Validate Bearer token against configured API keys."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return False, "Missing Bearer token"

    token = auth[7:]
    config = get_config()

    # Accept any valid provider API key as auth, or a dedicated key
    panel_pw = config.panel.password
    if panel_pw and token == panel_pw:
        return True, ""

    # Accept Telegram bot token as auth
    if config.telegram.token and token == config.telegram.token:
        return True, ""

    return False, "Invalid API key"


async def list_models(request: Request):
    """GET /v1/models — list available models."""
    config = get_config()
    models = []
    for prov_name, prov in config.providers.items():
        if not prov.api_key:
            continue
        for model_id in prov.models:
            models.append({
                "id": f"{prov_name}/{model_id}",
                "object": "model",
                "created": 1700000000,
                "owned_by": prov_name,
            })

    # Also add agent aliases
    for agent in config.agents:
        models.append({
            "id": f"agent:{agent.id}",
            "object": "model",
            "created": 1700000000,
            "owned_by": "pawang",
        })

    return JSONResponse({"object": "list", "data": models})


async def chat_completions(request: Request):
    """POST /v1/chat/completions — main completion endpoint."""
    ok, err = await _auth_check(request)
    if not ok:
        return JSONResponse({"error": {"message": err, "type": "auth_error"}}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": {"message": "Invalid JSON body", "type": "invalid_request"}},
            status_code=400,
        )

    model_str = body.get("model", "")
    messages_raw = body.get("messages", [])
    stream = body.get("stream", False)
    temperature = body.get("temperature", 0.7)
    max_tokens = body.get("max_tokens", 4096)

    if not model_str or not messages_raw:
        return JSONResponse(
            {"error": {"message": "model and messages are required", "type": "invalid_request"}},
            status_code=400,
        )

    config = get_config()

    # Resolve model: "provider/model" or "agent:agent_id"
    provider_name, model = _resolve_model(config, model_str)
    if not provider_name:
        return JSONResponse(
            {"error": {"message": f"Model not found: {model_str}", "type": "not_found"}},
            status_code=404,
        )

    # Convert messages
    messages = [Message(role=m.get("role", "user"), content=_extract_content(m)) for m in messages_raw]

    request_id = f"chatcmpl-{int(time.time()*1000)}"

    if stream:
        return StreamingResponse(
            _stream_response(config, provider_name, model, messages,
                             temperature, max_tokens, request_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Non-streaming
    try:
        response = await completion.complete(
            config=config,
            provider_name=provider_name,
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        result = {
            "id": request_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_str,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": response.text},
                "finish_reason": response.finish_reason or "stop",
            }],
            "usage": response.usage or {
                "prompt_tokens": sum(len(m.content) for m in messages) // 4,
                "completion_tokens": len(response.text) // 4,
                "total_tokens": (sum(len(m.content) for m in messages) + len(response.text)) // 4,
            },
        }

        # Record usage
        db = get_db()
        db.record_usage(provider_name, model, "api", "api",
                        result["usage"].get("prompt_tokens", 0),
                        result["usage"].get("completion_tokens", 0), 0)

        return JSONResponse(result)

    except Exception as e:
        log.error(f"API completion error: {e}")
        return JSONResponse(
            {"error": {"message": str(e)[:500], "type": "server_error"}},
            status_code=500,
        )


async def _stream_response(config, provider_name: str, model: str,
                           messages: list[Message], temperature: float,
                           max_tokens: int, request_id: str) -> AsyncIterator[str]:
    """SSE streaming response generator."""
    try:
        async for chunk in completion.stream(
            config=config,
            provider_name=provider_name,
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            if chunk.text:
                data = {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": f"{provider_name}/{model}",
                    "choices": [{
                        "index": 0,
                        "delta": {"content": chunk.text},
                        "finish_reason": None,
                    }],
                }
                yield f"data: {json.dumps(data)}\n\n"

        # Final chunk
        final = {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": f"{provider_name}/{model}",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(final)}\n\n"
        yield "data: [DONE]\n\n"

    except Exception as e:
        error_data = {"error": {"message": str(e)[:500], "type": "server_error"}}
        yield f"data: {json.dumps(error_data)}\n\n"


def _resolve_model(config, model_str: str) -> tuple[str, str]:
    """Resolve model string to (provider_name, model_id).

    Formats:
      - "provider/model" → direct
      - "agent:agent_id" → use agent's configured model
      - "model_id" → search all providers
    """
    if model_str.startswith("agent:"):
        agent_id = model_str[6:]
        agent = config.get_agent(agent_id)
        if agent:
            return agent.provider, agent.model
        return "", ""

    if "/" in model_str:
        provider_name, model = model_str.split("/", 1)
        prov = config.get_provider(provider_name)
        if prov and prov.api_key:
            return provider_name, model
        return "", ""

    # Search all providers
    for name, prov in config.providers.items():
        if prov.api_key and model_str in prov.models:
            return name, model_str

    return "", ""


def _extract_content(msg: dict) -> str:
    """Extract text content from message (handle both string and array formats)."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return " ".join(parts)
    return str(content)


# Routes
api_routes = [
    Route("/v1/models", list_models, methods=["GET"]),
    Route("/v1/chat/completions", chat_completions, methods=["POST"]),
]
