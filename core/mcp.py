"""MCP (Model Context Protocol) Client — connect to MCP servers for dynamic tools.

Supports stdio-based MCP servers. Loads tools from configured servers
and injects them into the agent tool loop.

Config in config.yaml:
  mcp:
    servers:
      - name: filesystem
        command: npx
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/root/pawang/workspace"]
      - name: sqlite
        command: npx
        args: ["-y", "@modelcontextprotocol/server-sqlite", "data/pawang.db"]
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Optional

from core.logger import log


def _estimate_tool_tokens(tools: list[dict]) -> int:
    """Rough per-message token overhead for a list of tool definitions.

    Uses ~4 chars per token heuristic on JSON-serialized tool defs.
    Not exact, but good enough to warn when MCP adds thousands of tokens.
    """
    if not tools:
        return 0
    try:
        serialized = json.dumps(tools, separators=(",", ":"), ensure_ascii=False)
        return len(serialized) // 4
    except Exception:
        return 0


@dataclass
class MCPServer:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    tools: list[dict] = field(default_factory=list)  # OpenAI format
    _process: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)
    _request_id: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MCPManager:
    """Manages connections to MCP servers."""

    def __init__(self):
        self._servers: dict[str, MCPServer] = {}

    async def load_servers(self, config_list: list[dict]):
        """Initialize and connect to configured MCP servers."""
        for srv_cfg in config_list:
            name = srv_cfg.get("name", "")
            if not name:
                continue
            server = MCPServer(
                name=name,
                command=srv_cfg.get("command", ""),
                args=srv_cfg.get("args", []),
                env=srv_cfg.get("env", {}),
            )
            try:
                await self._connect(server)
                self._servers[name] = server
                overhead = _estimate_tool_tokens(server.tools)
                log.info(
                    f"MCP server '{name}' connected — {len(server.tools)} tools, "
                    f"~{overhead:,} tokens/message overhead"
                )
                if overhead > 5000:
                    log.warning(
                        f"MCP '{name}' adds ~{overhead:,} tokens PER message. "
                        f"Disconnect if unused. See 'Token Management Hacks' tip 2/9."
                    )
            except Exception as e:
                log.error(f"MCP server '{name}' failed to connect: {e}")

    def overhead_summary(self) -> dict:
        """Return per-server + total token overhead estimates for /status."""
        per_server = {n: _estimate_tool_tokens(s.tools) for n, s in self._servers.items()}
        return {
            "servers": per_server,
            "total_tokens": sum(per_server.values()),
            "tool_count": sum(len(s.tools) for s in self._servers.values()),
        }

    async def _connect(self, server: MCPServer):
        """Start MCP server process and discover tools."""
        import os
        env = {**os.environ, **server.env}

        server._process = await asyncio.create_subprocess_exec(
            server.command, *server.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Send initialize request
        init_resp = await self._send_request(server, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pawang", "version": "1.0"},
        })

        if not init_resp:
            raise RuntimeError("MCP initialize failed — no response")

        # Send initialized notification
        await self._send_notification(server, "notifications/initialized", {})

        # List tools
        tools_resp = await self._send_request(server, "tools/list", {})
        if tools_resp and "tools" in tools_resp:
            for tool in tools_resp["tools"]:
                openai_tool = self._mcp_to_openai_tool(server.name, tool)
                server.tools.append(openai_tool)

    def _mcp_to_openai_tool(self, server_name: str, mcp_tool: dict) -> dict:
        """Convert MCP tool definition to OpenAI function calling format."""
        name = mcp_tool.get("name", "unknown")
        # Prefix with server name to avoid collisions
        prefixed_name = f"mcp_{server_name}_{name}"

        return {
            "type": "function",
            "function": {
                "name": prefixed_name,
                "description": mcp_tool.get("description", ""),
                "parameters": mcp_tool.get("inputSchema", {
                    "type": "object", "properties": {}, "required": [],
                }),
            },
            "_mcp_server": server_name,
            "_mcp_tool": name,
        }

    async def _send_request(self, server: MCPServer, method: str, params: dict) -> Optional[dict]:
        """Send JSON-RPC request and wait for response."""
        if not server._process or not server._process.stdin or not server._process.stdout:
            return None

        async with server._lock:
            server._request_id += 1
            req = {
                "jsonrpc": "2.0",
                "id": server._request_id,
                "method": method,
                "params": params,
            }

            msg = json.dumps(req) + "\n"
            server._process.stdin.write(msg.encode())
            await server._process.stdin.drain()

            # Read response (with timeout)
            try:
                line = await asyncio.wait_for(
                    server._process.stdout.readline(), timeout=30,
                )
                if not line:
                    return None
                resp = json.loads(line.decode())
                return resp.get("result")
            except (asyncio.TimeoutError, json.JSONDecodeError) as e:
                log.warning(f"MCP response error from {server.name}: {e}")
                return None

    async def _send_notification(self, server: MCPServer, method: str, params: dict):
        """Send JSON-RPC notification (no response expected)."""
        if not server._process or not server._process.stdin:
            return
        notif = {"jsonrpc": "2.0", "method": method, "params": params}
        msg = json.dumps(notif) + "\n"
        server._process.stdin.write(msg.encode())
        await server._process.stdin.drain()

    def get_all_tools(self) -> list[dict]:
        """Get all tools from all connected MCP servers."""
        tools = []
        for server in self._servers.values():
            tools.extend(server.tools)
        return tools

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call an MCP tool by its prefixed name.

        tool_name format: mcp_{server_name}_{tool_name}
        """
        # Parse server name and original tool name
        parts = tool_name.split("_", 2)
        if len(parts) < 3 or parts[0] != "mcp":
            return f"Invalid MCP tool name: {tool_name}"

        server_name = parts[1]
        # The original tool name might have been in the rest
        original_name = None
        server = self._servers.get(server_name)
        if not server:
            return f"MCP server not found: {server_name}"

        # Find the original tool name
        for tool_def in server.tools:
            if tool_def["function"]["name"] == tool_name:
                original_name = tool_def.get("_mcp_tool", "")
                break

        if not original_name:
            return f"MCP tool not found: {tool_name}"

        result = await self._send_request(server, "tools/call", {
            "name": original_name,
            "arguments": arguments,
        })

        if result is None:
            return "(MCP tool returned no result)"

        # Extract text content from result
        content = result.get("content", [])
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item.get("text", ""))
        return "\n".join(texts) if texts else json.dumps(result)

    async def shutdown(self):
        """Stop all MCP server processes."""
        for name, server in self._servers.items():
            if server._process:
                try:
                    server._process.terminate()
                    await asyncio.wait_for(server._process.wait(), timeout=5)
                except Exception:
                    server._process.kill()
                log.info(f"MCP server '{name}' stopped")
        self._servers.clear()

    @property
    def server_count(self) -> int:
        return len(self._servers)

    @property
    def tool_count(self) -> int:
        return sum(len(s.tools) for s in self._servers.values())
