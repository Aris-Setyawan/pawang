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
            "model_count": len(prov.models),
            "has_key": has_key,
            "api_format": prov.api_format,
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
        updated.append(agent_id)

    return JSONResponse({"ok": True, "updated": updated})


@require_auth
async def panel_usage(request: Request):
    """Usage statistics — provider/model breakdown, agent stats, daily cost."""
    db = get_db()
    hours = int(request.query_params.get("hours", "168"))  # default 7 days
    since = time.time() - (hours * 3600)

    # Provider/model stats
    stats = db.get_usage_stats(hours)

    # Total stats
    total = db.get_total_stats()

    # Agent stats
    agent_rows = db.conn.execute(
        "SELECT agent_id, COUNT(*) as count, COUNT(DISTINCT user_id) as users, "
        "SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens "
        "FROM usage WHERE created_at > ? GROUP BY agent_id ORDER BY count DESC",
        (since,),
    ).fetchall()
    agent_stats = [dict(r) for r in agent_rows]

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
        "stats": [dict(s) for s in stats] if stats else [],
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
]
