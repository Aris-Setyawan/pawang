"""Base provider — abstract interface for all LLM providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str
    tool_calls: Optional[list[dict]] = None  # raw API format for assistant tool calls
    tool_call_id: Optional[str] = None  # for role="tool" result messages
    name: Optional[str] = None  # tool name for role="tool"


@dataclass
class ToolCall:
    """Parsed tool call from LLM response."""
    id: str
    name: str
    arguments: str  # JSON string


@dataclass
class ThinkingConfig:
    """Provider-agnostic thinking/reasoning configuration.

    Maps to:
      - Anthropic: thinking.type + output_config.effort
      - DeepSeek: extra_body.thinking.type
      - Gemini: thinkingConfig.thinkingBudget
      - OpenAI o-series: automatic (no config needed)
    """
    enabled: bool = False
    effort: str = "high"  # low | medium | high | max (Anthropic)
    budget_tokens: int = 0  # Gemini thinkingBudget, 0 = dynamic


@dataclass
class CompletionRequest:
    messages: list[Message]
    model: str
    temperature: float = 0.7
    max_tokens: int = 4096
    stream: bool = True
    thinking: Optional[ThinkingConfig] = None
    tools: Optional[list[dict]] = None  # OpenAI function calling format


@dataclass
class CompletionChunk:
    text: str
    thinking_text: str = ""  # reasoning/thinking content
    finish_reason: Optional[str] = None
    model: str = ""
    usage: dict = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class CompletionResponse:
    text: str
    thinking_text: str = ""  # full reasoning/thinking content
    model: str = ""
    finish_reason: str = "stop"
    usage: dict = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)


class BaseProvider(ABC):
    """Abstract base for all provider adapters."""

    def __init__(self, name: str, base_url: str, api_key: str):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Non-streaming completion."""
        ...

    @abstractmethod
    async def stream(self, request: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        """Streaming completion — yields chunks."""
        ...

    @abstractmethod
    def build_headers(self) -> dict:
        """Provider-specific auth headers."""
        ...
