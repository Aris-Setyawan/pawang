"""Pawang Admin Panel — full web dashboard with API management.

Adapted from OpenClaw panel. Dark theme SPA with:
- Agent status monitoring
- API key management (propagate to .env + runtime)
- Model/agent configuration
- Usage statistics & cost tracking
- Creative generation settings
- System health overview
"""

import os
import time
import base64
from functools import wraps
from pathlib import Path

from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from core.config import PawangConfig, get_config, reload_config
from core.database import get_db
from core.logger import log


_start_time = time.time()
_PANEL_DIR = Path(__file__).parent


def _check_auth(request: Request) -> bool:
    """Check Basic Auth or X-Panel-Token header."""
    config = get_config()
    if not config.panel.password:
        return True

    # Check X-Panel-Token header (like OpenClaw)
    token = request.headers.get("x-panel-token", "")
    if token and token == config.panel.password:
        return True

    # Check Basic Auth
    auth = request.headers.get("authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, pwd = decoded.split(":", 1)
            return user == config.panel.username and pwd == config.panel.password
        except Exception:
            pass

    return False


def require_auth(func):
    @wraps(func)
    async def wrapper(request: Request):
        if not _check_auth(request):
            return JSONResponse(
                {"error": "unauthorized"}, status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Pawang Panel"'},
            )
        return await func(request)
    return wrapper


# --- Panel Pages ---

@require_auth
async def panel_index(request: Request):
    html_path = _PANEL_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Pawang Panel</h1><p>index.html not found</p>")


# --- API Endpoints ---

@require_auth
async def panel_status(request: Request):
    """System status — agents, providers, health."""
    config = get_config()

    # Provider health
    providers = {}
    try:
        from main import health_monitor
        if health_monitor:
            for name, s in health_monitor.get_all_status().items():
                providers[name] = {
                    "healthy": s.healthy,
                    "latency_ms": round(s.latency_ms, 1),
                    "total_requests": s.total_requests,
                    "total_errors": s.total_errors,
                    "rate_limited": getattr(s, 'rate_limited', False),
                }
    except (ImportError, AttributeError):
        for name in config.providers:
            prov = config.get_provider(name)
            providers[name] = {
                "healthy": bool(prov and prov.api_key),
                "latency_ms": 0,
                "total_requests": 0,
                "total_errors": 0,
            }

    # Agent info
    agents = []
    for a in config.agents:
        prov = config.get_provider(a.provider)
        has_key = bool(prov and prov.api_key)
        agents.append({
            "id": a.id, "name": a.name,
            "provider": a.provider, "model": a.model,
            "status": "healthy" if has_key else "no_key",
            "temperature": a.temperature,
            "max_iterations": a.max_iterations,
        })

    # Session count
    sessions = 0
    try:
        from main import agent_manager
        if agent_manager:
            sessions = len(agent_manager.list_sessions())
    except (ImportError, AttributeError):
        pass

    # MCP status
    mcp_info = {"servers": 0, "tools": 0}
    try:
        from main import mcp_manager
        if mcp_manager:
            mcp_info = {"servers": mcp_manager.server_count, "tools": mcp_manager.tool_count}
    except (ImportError, AttributeError):
        pass

    uptime = int(time.time() - _start_time)
    h, r = divmod(uptime, 3600)
    m, s = divmod(r, 60)

    return JSONResponse({
        "agents": agents,
        "providers": providers,
        "sessions": sessions,
        "uptime": f"{h}h {m}m {s}s",
        "mcp": mcp_info,
    })


@require_auth
async def panel_config(request: Request):
    """Full config overview — providers, models, keys (masked), agents."""
    config = get_config()

    providers_list = []
    keys = {}
    for name, prov in config.providers.items():
        has_key = bool(prov.api_key)
        masked = ""
        if has_key and len(prov.api_key) > 18:
            masked = prov.api_key[:10] + "..." + prov.api_key[-4:]
        elif has_key:
            masked = "set"
        providers_list.append({
            "id": name,
            "base_url": prov.base_url,
            "model_count": len(prov.models),
            "has_key": has_key,
            "api_format": prov.api_format,
            "methods": getattr(prov, 'methods', []) or [],
        })
        keys[name] = masked

    agents_cfg = []
    for a in config.agents:
        agents_cfg.append({
            "id": a.id, "name": a.name,
            "provider": a.provider, "model": a.model,
            "temperature": a.temperature,
            "max_iterations": a.max_iterations,
            "max_context_tokens": a.max_context_tokens,
            "fallbacks": a.fallbacks,
            "chat_model": a.chat_model,
            "chat_provider": a.chat_provider,
        })

    # All available models
    models_list = []
    for name, prov in config.providers.items():
        for model_id in prov.models:
            models_list.append(f"{name}/{model_id}")

    # Smart routing config
    routing = config.smart_routing or {}

    return JSONResponse({
        "providers": providers_list,
        "keys": keys,
        "agents": agents_cfg,
        "models": models_list,
        "smart_routing": routing,
        "session_timeout": config.session_timeout,
    })


@require_auth
async def panel_keys(request: Request):
    """POST — update API keys. Propagates to runtime + .env file."""
    data = await request.json()
    config = get_config()
    updated = []

    env_path = Path(__file__).parent.parent / ".env"
    env_map = {
        "anthropic": "ANTHROPIC_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY",
        "google": "GEMINI_API_KEY",
        "modelstudio": "MODELSTUDIO_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "kieai": "KIEAI_API_KEY",
        "sumopod": "SUMOPOD_API_KEY",
    }

    for provider_name, new_key in data.items():
        if not new_key or not isinstance(new_key, str):
            continue

        new_key = new_key.strip()
        prov = config.get_provider(provider_name)
        if prov:
            # Update runtime
            prov.api_key = new_key

            # Update .env
            env_var = env_map.get(provider_name)
            if env_var and env_path.exists():
                _update_env_file(env_path, env_var, new_key)

            updated.append(provider_name)
            log.info(f"Panel: API key updated for {provider_name}")

    # Reset provider instances
    if updated:
        try:
            from core.completion import reset_providers
            reset_providers()
        except Exception:
            pass

    return JSONResponse({"ok": True, "updated": updated})


@require_auth
async def panel_agents(request: Request):
    """POST — update agent model/provider config."""
    data = await request.json()
    config = get_config()
    updated = []

    for item in data.get("agents", []):
        agent_id = item.get("id")
        if not agent_id:
            continue
        agent = config.get_agent(agent_id)
        if not agent:
            continue

        if "provider" in item:
            agent.provider = item["provider"]
        if "model" in item:
            agent.model = item["model"]
        if "temperature" in item:
            agent.temperature = float(item["temperature"])
        if "max_iterations" in item:
            agent.max_iterations = int(item["max_iterations"])
        if "fallbacks" in item:
            agent.fallbacks = [f.strip() for f in item["fallbacks"] if f.strip()]
        if "chat_model" in item:
            agent.chat_model = item["chat_model"].strip()
        if "chat_provider" in item:
            agent.chat_provider = item["chat_provider"].strip()
        updated.append(agent_id)

    # Persist changes to config.yaml
    if updated:
        try:
            from core.config import save_config
            save_config(config)
            log.info(f"Panel: agent config persisted for {updated}")
        except Exception as e:
            log.error(f"Panel: failed to persist config: {e}")

    return JSONResponse({"ok": True, "updated": updated})


@require_auth
async def panel_usage(request: Request):
    """Usage statistics — provider/model breakdown, agent stats, daily cost."""
    db = get_db()
    config = get_config()
    hours = int(request.query_params.get("hours", "168"))  # default 7 days
    since = time.time() - (hours * 3600)

    # Provider/model stats from DB
    stats_rows = db.get_usage_stats(hours)
    stats_dict = {}
    for s in (stats_rows if stats_rows else []):
        d = dict(s)
        key = f"{d['provider']}|{d['model']}"
        stats_dict[key] = d

    # Merge with all configured models (so all show up even with 0 traffic)
    stats = []
    for prov_name, prov in config.providers.items():
        for model_id in prov.models:
            key = f"{prov_name}|{model_id}"
            if key in stats_dict:
                stats.append(stats_dict.pop(key))
            else:
                stats.append({
                    "provider": prov_name, "model": model_id,
                    "requests": 0, "total_input": 0, "total_output": 0,
                    "avg_latency": 0, "errors": 0,
                })
    # Add any remaining DB entries not in config (legacy models)
    for d in stats_dict.values():
        stats.append(d)

    # Total stats
    total = db.get_total_stats()

    # Agent stats — include ALL configured agents
    agent_rows = db.conn.execute(
        "SELECT agent_id, COUNT(*) as count, COUNT(DISTINCT user_id) as users, "
        "SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens "
        "FROM usage WHERE created_at > ? GROUP BY agent_id ORDER BY count DESC",
        (since,),
    ).fetchall()
    agent_map = {r["agent_id"]: dict(r) for r in agent_rows}
    agent_stats = []
    for agent in config.agents:
        if agent.id in agent_map:
            row = agent_map.pop(agent.id)
            row["name"] = agent.name
            row["model"] = agent.model
            row["provider"] = agent.provider
            agent_stats.append(row)
        else:
            agent_stats.append({
                "agent_id": agent.id, "name": agent.name,
                "model": agent.model, "provider": agent.provider,
                "count": 0, "users": 0,
                "input_tokens": 0, "output_tokens": 0,
            })
    # Add any remaining DB entries not in config
    for d in agent_map.values():
        agent_stats.append(d)

    # Daily breakdown
    daily_rows = db.conn.execute(
        "SELECT DATE(created_at, 'unixepoch') as date, "
        "COUNT(*) as requests, SUM(input_tokens) as input_tokens, "
        "SUM(output_tokens) as output_tokens "
        "FROM usage WHERE created_at > ? GROUP BY date ORDER BY date",
        (since,),
    ).fetchall()
    daily = [dict(r) for r in daily_rows]

    # Error rate
    error_row = db.conn.execute(
        "SELECT COUNT(*) as total, SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) as errors "
        "FROM usage WHERE created_at > ?",
        (since,),
    ).fetchone()

    return JSONResponse({
        "stats": stats,
        "total": total,
        "agent_stats": agent_stats,
        "daily": daily,
        "error_rate": {
            "total": error_row["total"] if error_row else 0,
            "errors": error_row["errors"] if error_row else 0,
        },
        "hours": hours,
    })


@require_auth
async def panel_health_check(request: Request):
    """Trigger health check on all providers."""
    try:
        from main import health_monitor
        if health_monitor:
            await health_monitor.check_all()
            return JSONResponse({"status": "checked"})
        return JSONResponse({"error": "Health monitor not initialized"}, status_code=503)
    except (ImportError, AttributeError):
        return JSONResponse({"error": "Health monitor not available"}, status_code=503)


@require_auth
async def panel_reload(request: Request):
    """Reload config from YAML."""
    try:
        reload_config()
        from core.completion import reset_providers
        reset_providers()
        return JSONResponse({"status": "reloaded"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Helpers ---

def _update_env_file(env_path: Path, var_name: str, value: str):
    """Update a variable in .env file."""
    lines = env_path.read_text().splitlines()
    updated = False
    for i, line in enumerate(lines):
        stripped = line.lstrip("# ")
        if stripped.startswith(f"{var_name}="):
            lines[i] = f"{var_name}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{var_name}={value}")
    env_path.write_text("\n".join(lines) + "\n")


@require_auth
async def panel_token_guard(request: Request):
    """Token Guard status API."""
    try:
        from core.token_guard import get_token_guard
        guard = get_token_guard()
        return JSONResponse(guard.get_status())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@require_auth
async def panel_token_guard_budgets(request: Request):
    """Update Token Guard budgets."""
    try:
        body = await request.json()
        budgets = body.get("budgets", {})
        if not budgets:
            return JSONResponse({"error": "no budgets provided"}, status_code=400)

        from core.token_guard import get_token_guard
        guard = get_token_guard()
        for agent_id, budget in budgets.items():
            guard.set_budget(agent_id, int(budget))

        return JSONResponse({"ok": True, "updated": list(budgets.keys())})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@require_auth
async def panel_change_password(request: Request):
    """Change panel password (updates .env file)."""
    try:
        body = await request.json()
        new_pw = body.get("password", "")
        if not new_pw or len(new_pw) < 4:
            return JSONResponse({"error": "Password minimal 4 karakter"}, status_code=400)

        # Update .env
        env_path = Path(__file__).parent.parent / ".env"
        _update_env_file(env_path, "PANEL_PASSWORD", new_pw)

        # Update runtime config
        config = get_config()
        config.panel.password = new_pw

        log.info("Panel password changed via panel")
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Provider Templates ---

PROVIDER_TEMPLATES = {
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api_format": "openai",
        "env_var": "OPENAI_API_KEY",
        "models": ["gpt-4o", "gpt-4o-mini", "o3", "o4-mini", "codex-mini-latest"],
        "docs_url": "https://platform.openai.com/api-keys",
    },
    "anthropic": {
        "name": "Anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "api_format": "anthropic",
        "env_var": "ANTHROPIC_API_KEY",
        "models": ["claude-opus-4-20250514", "claude-sonnet-4-20250514", "claude-haiku-4-5-20250514"],
        "docs_url": "https://console.anthropic.com/settings/keys",
    },
    "google": {
        "name": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "api_format": "gemini",
        "env_var": "GEMINI_API_KEY",
        "models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
        "docs_url": "https://aistudio.google.com/apikey",
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "api_format": "openai",
        "env_var": "DEEPSEEK_API_KEY",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "docs_url": "https://platform.deepseek.com/api_keys",
    },
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_format": "openai",
        "env_var": "OPENROUTER_API_KEY",
        "models": [],
        "docs_url": "https://openrouter.ai/keys",
    },
    "groq": {
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "api_format": "openai",
        "env_var": "GROQ_API_KEY",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
        "docs_url": "https://console.groq.com/keys",
    },
    "together": {
        "name": "Together AI",
        "base_url": "https://api.together.xyz/v1",
        "api_format": "openai",
        "env_var": "TOGETHER_API_KEY",
        "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo", "Qwen/Qwen2.5-72B-Instruct-Turbo"],
        "docs_url": "https://api.together.ai/settings/api-keys",
    },
    "mistral": {
        "name": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1",
        "api_format": "openai",
        "env_var": "MISTRAL_API_KEY",
        "models": ["mistral-large-latest", "mistral-small-latest", "codestral-latest"],
        "docs_url": "https://console.mistral.ai/api-keys",
    },
    "modelstudio": {
        "name": "Alibaba ModelStudio (Qwen)",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "api_format": "openai",
        "env_var": "MODELSTUDIO_API_KEY",
        "models": ["qwen-max", "qwen-plus", "qwen-turbo", "qwen2.5-72b-instruct"],
        "docs_url": "https://dashscope.console.aliyun.com/apiKey",
    },
    "sumopod": {
        "name": "SumoPod AI",
        "base_url": "https://ai.sumopod.com/v1",
        "api_format": "openai",
        "env_var": "SUMOPOD_API_KEY",
        "models": ["deepseek-v3-2", "deepseek-r1", "gpt-5.2", "gpt-5", "gpt-5-mini",
                    "gemini/gemini-2.5-pro", "gemini/gemini-2.5-flash", "glm-5", "glm-5-code",
                    "kimi-k2", "claude-haiku-4-5"],
        "docs_url": "https://ai.sumopod.com",
    },
}


@require_auth
async def panel_provider_templates(request: Request):
    """GET — list available provider templates (for quick-add)."""
    config = get_config()
    existing = set(config.providers.keys())
    templates = []
    for pid, t in PROVIDER_TEMPLATES.items():
        templates.append({
            "id": pid,
            "name": t["name"],
            "base_url": t["base_url"],
            "api_format": t["api_format"],
            "model_count": len(t["models"]),
            "docs_url": t["docs_url"],
            "installed": pid in existing,
        })
    return JSONResponse({"templates": templates})


@require_auth
async def panel_provider_add(request: Request):
    """POST — add a new provider (from template or custom)."""
    data = await request.json()
    config = get_config()

    pid = data.get("id", "").strip().lower()
    if not pid:
        return JSONResponse({"error": "Provider ID required"}, status_code=400)
    if pid in config.providers:
        return JSONResponse({"error": f"Provider '{pid}' already exists"}, status_code=400)

    # From template or custom
    template = PROVIDER_TEMPLATES.get(data.get("template", ""))
    base_url = data.get("base_url", template["base_url"] if template else "").strip()
    api_format = data.get("api_format", template["api_format"] if template else "openai").strip()
    api_key = data.get("api_key", "").strip()
    models = data.get("models", template["models"] if template else [])
    env_var = data.get("env_var", f"{pid.upper()}_API_KEY").strip()

    if not base_url:
        return JSONResponse({"error": "base_url required"}, status_code=400)

    # Save API key to .env
    env_path = Path(__file__).parent.parent / ".env"
    if api_key:
        _update_env_file(env_path, env_var, api_key)
        os.environ[env_var] = api_key

    methods = data.get("methods", [])
    if not isinstance(methods, list):
        methods = []

    # Add to runtime config
    from core.config import ProviderConfig, save_config
    prov = ProviderConfig(
        name=pid,
        base_url=base_url,
        api_key=api_key or os.environ.get(env_var, ""),
        api_format=api_format,
        models=models if isinstance(models, list) else [],
        methods=methods,
    )
    config.providers[pid] = prov

    # Also update env_map for panel_keys
    env_map = {
        "anthropic": "ANTHROPIC_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY", "google": "GEMINI_API_KEY",
        "modelstudio": "MODELSTUDIO_API_KEY", "openrouter": "OPENROUTER_API_KEY",
        "kieai": "KIEAI_API_KEY", "sumopod": "SUMOPOD_API_KEY",
    }
    env_map[pid] = env_var

    # Reset provider instances
    try:
        from core.completion import reset_providers
        reset_providers()
    except Exception:
        pass

    # Persist to config.yaml
    try:
        save_config(config)
    except Exception as e:
        log.error(f"Panel: failed to persist provider add: {e}")

    log.info(f"Panel: provider '{pid}' added ({api_format}, {len(models)} models)")
    return JSONResponse({"ok": True, "provider": pid, "models": len(models)})


@require_auth
async def panel_provider_edit(request: Request):
    """POST — edit an existing provider (base_url, models, api_key)."""
    data = await request.json()
    config = get_config()

    pid = data.get("id", "").strip()
    prov = config.get_provider(pid)
    if not prov:
        return JSONResponse({"error": f"Provider '{pid}' not found"}, status_code=404)

    if "base_url" in data:
        prov.base_url = data["base_url"].strip()
    if "api_format" in data:
        prov.api_format = data["api_format"].strip()
    if "models" in data:
        prov.models = data["models"] if isinstance(data["models"], list) else []
    if "api_key" in data and data["api_key"]:
        new_key = data["api_key"].strip()
        prov.api_key = new_key
        env_var = data.get("env_var", f"{pid.upper()}_API_KEY")
        env_path = Path(__file__).parent.parent / ".env"
        _update_env_file(env_path, env_var, new_key)
        os.environ[env_var] = new_key

    try:
        from core.completion import reset_providers
        reset_providers()
    except Exception:
        pass

    try:
        from core.config import save_config
        save_config(config)
    except Exception as e:
        log.error(f"Panel: failed to persist provider edit: {e}")

    log.info(f"Panel: provider '{pid}' edited")
    return JSONResponse({"ok": True, "provider": pid})


@require_auth
async def panel_provider_delete(request: Request):
    """POST — delete a provider."""
    data = await request.json()
    config = get_config()

    pid = data.get("id", "").strip()
    if pid not in config.providers:
        return JSONResponse({"error": f"Provider '{pid}' not found"}, status_code=404)

    # Check if any agent uses this provider
    using_agents = [a.id for a in config.agents if a.provider == pid]
    if using_agents:
        return JSONResponse({
            "error": f"Cannot delete: agents {using_agents} use this provider"
        }, status_code=400)

    del config.providers[pid]

    try:
        from core.completion import reset_providers
        reset_providers()
    except Exception:
        pass

    try:
        from core.config import save_config
        save_config(config)
    except Exception as e:
        log.error(f"Panel: failed to persist provider delete: {e}")

    log.info(f"Panel: provider '{pid}' deleted")
    return JSONResponse({"ok": True, "deleted": pid})


# --- Routes ---

panel_routes = [
    Route("/panel", panel_index),
    Route("/panel/api/status", panel_status),
    Route("/panel/api/config", panel_config),
    Route("/panel/api/keys", panel_keys, methods=["POST"]),
    Route("/panel/api/agents", panel_agents, methods=["POST"]),
    Route("/panel/api/usage", panel_usage),
    Route("/panel/api/health-check", panel_health_check, methods=["POST"]),
    Route("/panel/api/reload", panel_reload, methods=["POST"]),
    Route("/panel/api/token-guard", panel_token_guard),
    Route("/panel/api/token-guard/budgets", panel_token_guard_budgets, methods=["POST"]),
    Route("/panel/api/password", panel_change_password, methods=["POST"]),
    Route("/panel/api/providers/templates", panel_provider_templates),
    Route("/panel/api/providers/add", panel_provider_add, methods=["POST"]),
    Route("/panel/api/providers/edit", panel_provider_edit, methods=["POST"]),
    Route("/panel/api/providers/delete", panel_provider_delete, methods=["POST"]),
]
