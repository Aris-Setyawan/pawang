"""Completion Engine — routes requests to the right provider."""

from typing import AsyncIterator, Optional

from core.config import PawangConfig, ProviderConfig
from core.logger import log
from providers.base import (
    BaseProvider, CompletionRequest, CompletionResponse, CompletionChunk,
    Message, ThinkingConfig,
)
from providers.openai_compat import OpenAIProvider
from providers.anthropic import AnthropicProvider
from providers.gemini import GeminiProvider


_FORMAT_MAP = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
}

# Cache instantiated providers
_providers: dict[str, BaseProvider] = {}


def get_provider(config: PawangConfig, provider_name: str) -> BaseProvider:
    """Get or create a provider instance."""
    if provider_name in _providers:
        return _providers[provider_name]

    prov_config = config.get_provider(provider_name)
    if not prov_config:
        raise ValueError(f"Provider '{provider_name}' not found in config")

    cls = _FORMAT_MAP.get(prov_config.api_format)
    if not cls:
        raise ValueError(
            f"Unknown api_format '{prov_config.api_format}' for provider '{provider_name}'. "
            f"Supported: {list(_FORMAT_MAP.keys())}"
        )

    instance = cls(
        name=prov_config.name,
        base_url=prov_config.base_url,
        api_key=prov_config.api_key,
    )
    _providers[provider_name] = instance
    log.info(f"Initialized provider: {provider_name} ({prov_config.api_format})")
    return instance


def _strip_provider_prefix(provider_name: str, model: str) -> str:
    """Strip provider prefix from model name (e.g. 'sumopod/glm-5' -> 'glm-5')."""
    prefix = f"{provider_name}/"
    if model.startswith(prefix):
        return model[len(prefix):]
    return model


async def complete(
    config: PawangConfig,
    provider_name: str,
    model: str,
    messages: list[Message],
    temperature: float = 0.7,
    max_tokens: int = 4096,
    thinking: Optional[ThinkingConfig] = None,
    tools: Optional[list[dict]] = None,
) -> CompletionResponse:
    """Send a non-streaming completion request."""
    provider = get_provider(config, provider_name)
    model = _strip_provider_prefix(provider_name, model)
    request = CompletionRequest(
        messages=messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=False,
        thinking=thinking,
        tools=tools,
    )
    log.info(f"Complete: {provider_name}/{model} ({len(messages)} msgs)"
             + (f" thinking={thinking.effort}" if thinking and thinking.enabled else "")
             + (f" tools={len(tools)}" if tools else ""))
    return await provider.complete(request)


async def stream(
    config: PawangConfig,
    provider_name: str,
    model: str,
    messages: list[Message],
    temperature: float = 0.7,
    max_tokens: int = 4096,
    thinking: Optional[ThinkingConfig] = None,
) -> AsyncIterator[CompletionChunk]:
    """Send a streaming completion request."""
    provider = get_provider(config, provider_name)
    model = _strip_provider_prefix(provider_name, model)
    request = CompletionRequest(
        messages=messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
        thinking=thinking,
    )
    log.info(f"Stream: {provider_name}/{model} ({len(messages)} messages)"
             + (f" thinking={thinking.effort}" if thinking and thinking.enabled else ""))
    async for chunk in provider.stream(request):
        yield chunk


def reset_providers():
    """Clear provider cache (for config reload)."""
    _providers.clear()
