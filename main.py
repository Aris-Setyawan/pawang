"""Pawang — Multi-Agent Gateway

Single process, <100MB RAM, proper systemd integration.
"""

import asyncio
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import JSONResponse
import uvicorn

from core.config import get_config, reload_config
from core.database import get_db
from core.logger import log, setup_logger
from agents.manager import AgentManager
from channels.telegram import TelegramBot
from core.health import HealthMonitor
from panel.app import panel_routes


# Globals — accessible from panel
agent_manager: AgentManager = None
telegram_bot: TelegramBot = None
health_monitor: HealthMonitor = None


# --- HTTP API Routes ---

async def health(request: Request):
    """Health check endpoint."""
    config = get_config()
    provider_status = {}
    if health_monitor:
        for name, s in health_monitor.get_all_status().items():
            provider_status[name] = {"healthy": s.healthy, "latency_ms": s.latency_ms}

    return JSONResponse({
        "status": "ok",
        "agents": len(config.agents),
        "providers": provider_status,
        "sessions": len(agent_manager.list_sessions()) if agent_manager else 0,
    })


async def api_models(request: Request):
    """List all available models."""
    models = agent_manager.list_available_models() if agent_manager else []
    return JSONResponse({"models": models})


async def api_agents(request: Request):
    """List all agents."""
    config = get_config()
    agents = []
    for a in config.agents:
        agents.append({
            "id": a.id,
            "name": a.name,
            "model": a.model,
            "provider": a.provider,
        })
    return JSONResponse({"agents": agents})


async def api_usage(request: Request):
    """Usage statistics."""
    db = get_db()
    hours = int(request.query_params.get("hours", "24"))
    return JSONResponse({
        "stats": db.get_usage_stats(hours),
        "total": db.get_total_stats(),
    })


async def api_sessions(request: Request):
    """List all sessions from DB."""
    db = get_db()
    return JSONResponse({"sessions": db.get_all_sessions()})


async def api_reload(request: Request):
    """Reload configuration."""
    try:
        reload_config()
        from core.completion import reset_providers
        reset_providers()
        return JSONResponse({"status": "reloaded"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- App Lifecycle ---

async def startup():
    global agent_manager, telegram_bot, health_monitor

    config = get_config()
    setup_logger(level=config.gateway.log_level)
    log.info("Pawang starting...")

    # Init database
    db = get_db()
    log.info(f"  Database: {db.path}")
    log.info(f"  Providers: {list(config.providers.keys())}")
    log.info(f"  Agents: {[a.id + '/' + a.name for a in config.agents]}")

    # Init agent manager
    agent_manager = AgentManager(config)

    # Start health monitor
    health_monitor = HealthMonitor(config)
    if config.health.auto_failover:
        health_monitor.start()
        log.info(f"Health monitor started (interval={config.health.check_interval}s)")

    # Start Telegram bot
    if config.telegram.token:
        telegram_bot = TelegramBot(config, agent_manager)
        await telegram_bot.start()
        log.info("Telegram bot started")
    else:
        log.warning("No Telegram token — bot disabled")

    log.info(f"Pawang ready on port {config.gateway.port}")


async def shutdown():
    global telegram_bot, health_monitor
    log.info("Pawang shutting down...")
    if health_monitor:
        health_monitor.stop()
    if telegram_bot:
        await telegram_bot.stop()
    db = get_db()
    db.close()
    log.info("Pawang stopped")


# --- Starlette App ---

@asynccontextmanager
async def lifespan(app):
    await startup()
    yield
    await shutdown()


routes = [
    Route("/health", health),
    Route("/api/models", api_models),
    Route("/api/agents", api_agents),
    Route("/api/usage", api_usage),
    Route("/api/sessions", api_sessions),
    Route("/api/reload", api_reload, methods=["POST"]),
] + panel_routes

app = Starlette(routes=routes, lifespan=lifespan)


def main():
    config = get_config()
    uvicorn.run(
        "main:app",
        host=config.gateway.host,
        port=config.gateway.port,
        log_level=config.gateway.log_level,
        workers=1,
    )


if __name__ == "__main__":
    main()
