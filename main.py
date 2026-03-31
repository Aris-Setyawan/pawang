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
from core.hooks import hooks
from core.mcp import MCPManager
from core.approval import approval_manager
from channels.webhook import webhook_adapter
from agents.manager import AgentManager
from channels.telegram import TelegramBot
from core.health import HealthMonitor
from core.scheduler import Scheduler
from core.jobs import register_jobs
from core.token_guard import init_token_guard, get_token_guard
from panel.app import panel_routes
from api.openai_server import api_routes


# Globals — accessible from panel
agent_manager: AgentManager = None
telegram_bot: TelegramBot = None
health_monitor: HealthMonitor = None
scheduler: Scheduler = None
mcp_manager: MCPManager = None
token_guard = None


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
        "scheduled_jobs": len(scheduler.get_jobs()) if scheduler else 0,
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


async def api_jobs(request: Request):
    """List scheduled jobs and their status."""
    jobs = scheduler.get_jobs() if scheduler else []
    return JSONResponse({"jobs": jobs})


async def api_reload(request: Request):
    """Reload configuration."""
    try:
        reload_config()
        from core.completion import reset_providers
        reset_providers()
        return JSONResponse({"status": "reloaded"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_token_guard(request: Request):
    """Token Guard status — spike detection + budget monitoring."""
    guard = get_token_guard()
    return JSONResponse(guard.get_status())


# --- App Lifecycle ---

async def startup():
    global agent_manager, telegram_bot, health_monitor, scheduler, mcp_manager

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

    # Start scheduler
    scheduler = Scheduler()
    register_jobs(scheduler, config)
    if telegram_bot and telegram_bot.app and config.telegram.admin_chat_id:
        admin_chat_id = config.telegram.admin_chat_id
        async def _notify(text):
            try:
                await telegram_bot.app.bot.send_message(chat_id=admin_chat_id, text=text)
            except Exception as e:
                log.error(f"Telegram notify failed: {e}")
        scheduler.set_notify(_notify)
    scheduler.start()
    log.info(f"Scheduler started ({len(scheduler.get_jobs())} jobs)")

    # Init MCP servers (if configured)
    mcp_config = getattr(config, '_raw', {}).get('mcp', {}).get('servers', [])
    if not mcp_config:
        # Try loading from raw YAML
        import yaml
        from core.config import CONFIG_PATH
        try:
            raw = yaml.safe_load(CONFIG_PATH.read_text())
            mcp_config = raw.get('mcp', {}).get('servers', [])
        except Exception:
            mcp_config = []
    if mcp_config:
        mcp_manager = MCPManager()
        await mcp_manager.load_servers(mcp_config)
        log.info(f"MCP: {mcp_manager.server_count} servers, {mcp_manager.tool_count} tools")

    # Init webhook adapter (if configured)
    try:
        import yaml
        from core.config import CONFIG_PATH
        raw = yaml.safe_load(CONFIG_PATH.read_text())
        webhooks = raw.get('webhooks', {})
        if webhooks:
            webhook_adapter.configure(webhooks)
            log.info(f"Webhooks: {webhook_adapter.enabled_platforms}")
    except Exception:
        pass

    # Wire command approval DM notifications
    if telegram_bot and telegram_bot.app and config.telegram.admin_chat_id:
        admin_chat_id = config.telegram.admin_chat_id
        tg_app = telegram_bot.app

        async def _approval_notify(approval_id, command, reason, user_id, agent_id, **kw):
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Approve", callback_data=f"approve:{approval_id}"),
                InlineKeyboardButton("Deny", callback_data=f"deny:{approval_id}"),
            ]])
            await tg_app.bot.send_message(
                chat_id=admin_chat_id,
                text=(
                    f"Command Approval Request\n"
                    f"User: {user_id}\nAgent: {agent_id}\n"
                    f"Reason: {reason}\n"
                    f"Command: {command[:200]}"
                ),
                reply_markup=keyboard,
            )

        approval_manager.set_notify(_approval_notify)

    # Init Token Guard (spike detection + budget enforcement)
    global token_guard
    token_guard = init_token_guard(config)
    if telegram_bot and telegram_bot.app and config.telegram.admin_chat_id:
        token_guard.set_notify(_notify)
    log.info("Token Guard initialized")

    log.info(f"Pawang ready on port {config.gateway.port}")
    await hooks.emit("startup", providers=list(config.providers.keys()),
                      agents=[a.id for a in config.agents])


async def shutdown():
    global telegram_bot, health_monitor, scheduler, mcp_manager
    log.info("Pawang shutting down...")
    await hooks.emit("shutdown")
    if mcp_manager:
        await mcp_manager.shutdown()
    if scheduler:
        scheduler.stop()
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
    Route("/api/jobs", api_jobs),
    Route("/api/reload", api_reload, methods=["POST"]),
    Route("/api/token-guard", api_token_guard),
] + api_routes + panel_routes

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
