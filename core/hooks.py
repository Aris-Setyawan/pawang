"""Event Hooks — lightweight plugin system for lifecycle events.

Events:
  - startup: gateway started
  - message:received: user message received
  - message:sent: response sent to user
  - tool:called: tool execution started
  - session:reset: session cleared
  - error: error occurred

Hooks are async callables registered programmatically.
"""

import asyncio
from collections import defaultdict
from typing import Callable
from core.logger import log


class HookRegistry:
    """Registry for event hooks."""

    def __init__(self):
        self._hooks: dict[str, list[Callable]] = defaultdict(list)

    def on(self, event: str, func: Callable):
        """Register a hook for an event."""
        self._hooks[event].append(func)
        log.info(f"Hook registered: {event} -> {func.__name__}")

    def off(self, event: str, func: Callable):
        """Unregister a hook."""
        if func in self._hooks[event]:
            self._hooks[event].remove(func)

    async def emit(self, event: str, **kwargs):
        """Emit an event, calling all registered hooks."""
        for hook in self._hooks.get(event, []):
            try:
                result = hook(**kwargs)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                log.error(f"Hook error ({event}/{hook.__name__}): {e}")

    def list_hooks(self) -> dict[str, list[str]]:
        """List all registered hooks."""
        return {event: [f.__name__ for f in funcs]
                for event, funcs in self._hooks.items() if funcs}


# Global registry
hooks = HookRegistry()
