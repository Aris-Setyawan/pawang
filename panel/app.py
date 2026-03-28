"""Admin Panel — web-based control panel for Pawang."""

import base64
import time
from functools import wraps

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.config import PawangConfig, reload_config
from core.logger import log


def _check_auth(request: Request, config: PawangConfig) -> bool:
    """Check Basic Auth."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        user, pwd = decoded.split(":", 1)
        return user == config.panel.username and pwd == config.panel.password
    except Exception:
        return False


def require_auth(func):
    """Decorator to require Basic Auth."""
    @wraps(func)
    async def wrapper(request: Request):
        from core.config import get_config
        config = get_config()
        if config.panel.password and not _check_auth(request, config):
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Pawang Panel"'},
            )
        return await func(request)
    return wrapper


PANEL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pawang Panel</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0f172a; color: #e2e8f0; }
  .header { background: #1e293b; padding: 1rem 2rem; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #334155; }
  .header h1 { font-size: 1.5rem; color: #38bdf8; }
  .header .status { color: #4ade80; font-size: 0.9rem; }
  .container { max-width: 1200px; margin: 0 auto; padding: 2rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(350px, 1fr)); gap: 1.5rem; }
  .card { background: #1e293b; border-radius: 12px; padding: 1.5rem; border: 1px solid #334155; }
  .card h2 { color: #38bdf8; font-size: 1.1rem; margin-bottom: 1rem; }
  .stat { display: flex; justify-content: space-between; padding: 0.5rem 0; border-bottom: 1px solid #334155; }
  .stat:last-child { border-bottom: none; }
  .stat .label { color: #94a3b8; }
  .stat .value { font-weight: 600; }
  .healthy { color: #4ade80; }
  .unhealthy { color: #f87171; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 600; }
  .badge-green { background: #166534; color: #4ade80; }
  .badge-red { background: #7f1d1d; color: #f87171; }
  .badge-blue { background: #1e3a5f; color: #38bdf8; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 0.6rem; border-bottom: 1px solid #334155; }
  th { color: #94a3b8; font-size: 0.85rem; text-transform: uppercase; }
  button { background: #38bdf8; color: #0f172a; border: none; padding: 0.5rem 1rem; border-radius: 6px; cursor: pointer; font-weight: 600; }
  button:hover { background: #7dd3fc; }
  .btn-sm { padding: 0.25rem 0.75rem; font-size: 0.85rem; }
  .actions { margin-top: 1.5rem; display: flex; gap: 0.75rem; }
</style>
</head>
<body>
<div class="header">
  <h1>Pawang Panel</h1>
  <div class="status" id="uptime">Loading...</div>
</div>
<div class="container">
  <div class="grid" id="content">
    <div class="card">
      <h2>Providers</h2>
      <div id="providers">Loading...</div>
    </div>
    <div class="card">
      <h2>Agents</h2>
      <div id="agents">Loading...</div>
    </div>
    <div class="card">
      <h2>Sessions</h2>
      <div id="sessions">Loading...</div>
    </div>
    <div class="card">
      <h2>Usage (24h)</h2>
      <div id="usage">Loading...</div>
    </div>
    <div class="card">
      <h2>Actions</h2>
      <div class="actions">
        <button onclick="reloadConfig()">Reload Config</button>
        <button onclick="checkHealth()">Health Check</button>
        <button onclick="location.reload()">Refresh</button>
      </div>
    </div>
  </div>
</div>
<script>
async function fetchData() {
  try {
    const [status, models, agents, usage] = await Promise.all([
      fetch('/panel/api/status').then(r => r.json()),
      fetch('/api/models').then(r => r.json()),
      fetch('/api/agents').then(r => r.json()),
      fetch('/api/usage').then(r => r.json()),
    ]);

    // Providers
    let provHtml = '<table><tr><th>Provider</th><th>Status</th><th>Latency</th><th>Models</th></tr>';
    for (const [name, info] of Object.entries(status.providers || {})) {
      const badge = info.healthy
        ? '<span class="badge badge-green">OK</span>'
        : '<span class="badge badge-red">DOWN</span>';
      provHtml += `<tr><td>${name}</td><td>${badge}</td><td>${info.latency_ms?.toFixed(0) || '-'}ms</td><td>${info.total_requests || 0} req</td></tr>`;
    }
    provHtml += '</table>';
    document.getElementById('providers').innerHTML = provHtml;

    // Agents
    let agentHtml = '<table><tr><th>ID</th><th>Name</th><th>Model</th><th>Provider</th></tr>';
    for (const a of agents.agents || []) {
      agentHtml += `<tr><td>${a.id}</td><td>${a.name}</td><td><span class="badge badge-blue">${a.model}</span></td><td>${a.provider}</td></tr>`;
    }
    agentHtml += '</table>';
    document.getElementById('agents').innerHTML = agentHtml;

    // Sessions
    document.getElementById('sessions').innerHTML = `
      <div class="stat"><span class="label">Active Sessions</span><span class="value">${status.sessions || 0}</span></div>
      <div class="stat"><span class="label">Total Models</span><span class="value">${(models.models || []).length}</span></div>
      <div class="stat"><span class="label">Uptime</span><span class="value">${status.uptime || '-'}</span></div>
    `;

    // Usage
    let usageHtml = '';
    const stats = usage.stats || [];
    if (stats.length > 0) {
      usageHtml = '<table><tr><th>Provider/Model</th><th>Requests</th><th>Avg Latency</th><th>Errors</th></tr>';
      for (const s of stats) {
        usageHtml += `<tr><td>${s.provider}/${s.model}</td><td>${s.requests}</td><td>${s.avg_latency?.toFixed(0)}ms</td><td>${s.errors}</td></tr>`;
      }
      usageHtml += '</table>';
    } else {
      usageHtml = '<div class="stat"><span class="label">No requests yet</span></div>';
    }
    const total = usage.total || {};
    const msgs = total.messages || {};
    const usg = total.usage || {};
    usageHtml += `<div class="stat"><span class="label">Total Messages</span><span class="value">${msgs.total_messages || 0}</span></div>`;
    usageHtml += `<div class="stat"><span class="label">Total API Calls</span><span class="value">${usg.total_requests || 0}</span></div>`;
    document.getElementById('usage').innerHTML = usageHtml;

    document.getElementById('uptime').textContent = 'Connected | ' + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById('uptime').textContent = 'Error: ' + e.message;
    document.getElementById('uptime').style.color = '#f87171';
  }
}

async function reloadConfig() {
  const r = await fetch('/api/reload', { method: 'POST' });
  const d = await r.json();
  alert(d.status || d.error);
  fetchData();
}

async function checkHealth() {
  const r = await fetch('/panel/api/health-check', { method: 'POST' });
  const d = await r.json();
  alert('Health check: ' + (d.status || d.error));
  fetchData();
}

fetchData();
setInterval(fetchData, 15000);
</script>
</body>
</html>"""


_start_time = time.time()


@require_auth
async def panel_index(request: Request):
    return HTMLResponse(PANEL_HTML)


@require_auth
async def panel_status(request: Request):
    from core.config import get_config
    config = get_config()

    # Try to get health data
    health_data = {}
    try:
        from main import health_monitor
        if health_monitor:
            for name, status in health_monitor.get_all_status().items():
                health_data[name] = {
                    "healthy": status.healthy,
                    "latency_ms": status.latency_ms,
                    "last_error": status.last_error,
                    "consecutive_failures": status.consecutive_failures,
                    "total_requests": status.total_requests,
                    "total_errors": status.total_errors,
                }
    except ImportError:
        for name in config.providers:
            health_data[name] = {"healthy": True, "latency_ms": 0}

    # Try to get session count
    session_count = 0
    try:
        from main import agent_manager
        if agent_manager:
            session_count = len(agent_manager.list_sessions())
    except ImportError:
        pass

    uptime_secs = int(time.time() - _start_time)
    hours, remainder = divmod(uptime_secs, 3600)
    minutes, seconds = divmod(remainder, 60)

    return JSONResponse({
        "providers": health_data,
        "sessions": session_count,
        "uptime": f"{hours}h {minutes}m {seconds}s",
        "agents": len(config.agents),
    })


@require_auth
async def panel_health_check(request: Request):
    try:
        from main import health_monitor
        if health_monitor:
            await health_monitor.check_all()
            return JSONResponse({"status": "checked"})
        return JSONResponse({"error": "Health monitor not initialized"}, status_code=503)
    except ImportError:
        return JSONResponse({"error": "Health monitor not available"}, status_code=503)


panel_routes = [
    Route("/panel", panel_index),
    Route("/panel/api/status", panel_status),
    Route("/panel/api/health-check", panel_health_check, methods=["POST"]),
]
