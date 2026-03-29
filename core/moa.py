"""Mixture of Agents (MoA) — parallel multi-model reasoning.

Spawns N reference models in parallel, then aggregates their responses
with a master model for higher quality output.

Usage:
  result = await mixture_of_agents(config, prompt, reference_models, master_model)
"""

import asyncio
from typing import Optional

from core.logger import log
from core import completion
from providers.base import Message


async def mixture_of_agents(
    config,
    prompt: str,
    reference_models: list[tuple[str, str]],  # [(provider, model), ...]
    master_provider: str = "",
    master_model: str = "",
    system_prompt: str = "",
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> str:
    """Run MoA: parallel reference models → aggregate with master.

    Args:
        config: PawangConfig
        prompt: User's question/task
        reference_models: List of (provider, model) tuples for reference
        master_provider: Provider for aggregation (default: first reference)
        master_model: Model for aggregation
        system_prompt: Optional system prompt
        temperature: Generation temperature
        max_tokens: Max output tokens

    Returns:
        Aggregated response text
    """
    if not reference_models:
        return "(No reference models configured for MoA)"

    # Default master to first reference model
    if not master_provider:
        master_provider = reference_models[0][0]
        master_model = reference_models[0][1]

    # Phase 1: Parallel reference completions
    log.info(f"MoA: spawning {len(reference_models)} reference models")
    tasks = []
    for prov_name, model in reference_models:
        tasks.append(_get_reference_response(
            config, prov_name, model, prompt, system_prompt, temperature, max_tokens,
        ))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Collect successful responses
    reference_responses = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            prov, model = reference_models[i]
            log.warning(f"MoA reference failed ({prov}/{model}): {result}")
        elif result:
            prov, model = reference_models[i]
            reference_responses.append({
                "model": f"{prov}/{model}",
                "response": result,
            })

    if not reference_responses:
        return "(All reference models failed)"

    if len(reference_responses) == 1:
        # Only one succeeded, return it directly
        return reference_responses[0]["response"]

    # Phase 2: Aggregate with master model
    log.info(f"MoA: aggregating {len(reference_responses)} responses with {master_provider}/{master_model}")

    aggregate_prompt = _build_aggregate_prompt(prompt, reference_responses)

    messages = []
    if system_prompt:
        messages.append(Message(role="system", content=system_prompt))
    messages.append(Message(role="user", content=aggregate_prompt))

    try:
        response = await completion.complete(
            config=config,
            provider_name=master_provider,
            model=master_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.text
    except Exception as e:
        log.error(f"MoA aggregation failed: {e}")
        # Fallback: return best (longest) reference response
        best = max(reference_responses, key=lambda r: len(r["response"]))
        return best["response"]


async def _get_reference_response(
    config, provider_name: str, model: str, prompt: str,
    system_prompt: str, temperature: float, max_tokens: int,
) -> str:
    """Get a single reference model response."""
    messages = []
    if system_prompt:
        messages.append(Message(role="system", content=system_prompt))
    messages.append(Message(role="user", content=prompt))

    response = await completion.complete(
        config=config,
        provider_name=provider_name,
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.text


def _build_aggregate_prompt(original_prompt: str, responses: list[dict]) -> str:
    """Build the aggregation prompt for the master model."""
    parts = [
        "You have been provided with multiple AI responses to the same question. "
        "Synthesize these responses into a single, comprehensive, high-quality answer. "
        "Take the best ideas from each response, resolve any contradictions, "
        "and produce a clear, well-structured final answer.\n\n"
        f"Original question: {original_prompt}\n\n"
        "--- Responses ---\n"
    ]

    for i, resp in enumerate(responses, 1):
        parts.append(f"\n[Model {i}: {resp['model']}]\n{resp['response']}\n")

    parts.append(
        "\n--- End of Responses ---\n\n"
        "Now synthesize the above into your best possible answer:"
    )

    return "".join(parts)


def get_available_reference_models(config, exclude_provider: str = "",
                                   exclude_model: str = "",
                                   max_models: int = 3) -> list[tuple[str, str]]:
    """Get available models for MoA reference, excluding the master.

    Returns up to max_models (provider, model) tuples.
    """
    models = []
    # Preferred models for MoA references
    preferred = [
        ("google", "gemini-2.0-flash"),
        ("deepseek", "deepseek-chat"),
        ("openai", "gpt-4o-mini"),
        ("modelstudio", "qwen-plus"),
        ("openrouter", "meta-llama/llama-3.1-8b-instruct:free"),
    ]

    for prov_name, model in preferred:
        if prov_name == exclude_provider and model == exclude_model:
            continue
        prov = config.get_provider(prov_name)
        if prov and prov.api_key:
            models.append((prov_name, model))
            if len(models) >= max_models:
                break

    return models
