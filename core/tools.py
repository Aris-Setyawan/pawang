"""Tool Execution — allows agents to run scripts and tools.

Defines tools as OpenAI-compatible function definitions.
Handles execution and returns results to the LLM.
"""

import asyncio
import os
from dataclasses import dataclass
from typing import Optional

from core.logger import log

SCRIPTS_DIR = "/root/pawang/scripts"


@dataclass
class ToolResult:
    name: str
    output: str
    success: bool = True


# --- Tool Definitions (OpenAI function calling format) ---

DELEGATE_TOOL = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": (
            "Delegasikan tugas ke agent spesialis lain. "
            "agent2=Creative (gambar/video/audio/musik), "
            "agent3=Analyst (analisis/riset/data), "
            "agent4=Coder (coding/debug/server). "
            "Gunakan ini untuk tugas yang bukan keahlian kamu."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "enum": ["agent2", "agent3", "agent4"],
                    "description": "ID agent tujuan delegasi",
                },
                "task": {
                    "type": "string",
                    "description": "Deskripsi tugas lengkap untuk agent tujuan",
                },
            },
            "required": ["agent_id", "task"],
        },
    },
}

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "check_balances",
            "description": "Cek saldo/balance semua API provider (DeepSeek, OpenRouter, OpenAI, Gemini, ModelStudio, kie.ai)",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate gambar/foto dari prompt teks dan kirim ke Telegram",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Deskripsi gambar yang ingin dibuat (English, detailed)",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Caption untuk gambar di Telegram",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_video",
            "description": "Generate video dari prompt teks dan kirim ke Telegram",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Deskripsi video yang ingin dibuat (English, detailed)",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Caption untuk video",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_audio",
            "description": "Generate audio/suara TTS atau musik, lalu kirim ke Telegram",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Teks yang ingin dijadikan suara, atau prompt musik",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Caption untuk audio",
                    },
                    "voice": {
                        "type": "string",
                        "description": "Voice: Aoede, Kore, Charon, Puck (Google) atau nova, alloy, echo (OpenAI)",
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["google", "openai", "kieai"],
                        "description": "Provider TTS. kieai untuk generate musik",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": "Kirim file/gambar/video ke Telegram",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path ke file yang ingin dikirim",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Caption untuk file",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Jalankan perintah bash/shell. Untuk coding, debugging, cek status server, dll.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Perintah bash yang ingin dijalankan",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Cari informasi di internet via DuckDuckGo (gratis)",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Query pencarian",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Simpan fakta/preferensi tentang user yang penting untuk diingat di percakapan mendatang. Contoh: nama, lokasi, pekerjaan, preferensi, project yang sedang dikerjakan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Fakta yang ingin disimpan (singkat, jelas)",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["profile", "preference", "project", "general"],
                        "description": "Kategori memory: profile (data diri), preference (preferensi), project (project/pekerjaan), general (lain-lain)",
                    },
                    "memory_type": {
                        "type": "string",
                        "enum": ["user", "agent"],
                        "description": "Tipe: user (fakta tentang user) atau agent (catatan/observasi agent)",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memories",
            "description": "Cari/lihat memory yang tersimpan tentang user. Tanpa query = lihat semua, dengan query = search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Kata kunci pencarian (opsional, kosongkan untuk lihat semua)",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["profile", "preference", "project", "general"],
                        "description": "Filter berdasarkan kategori (opsional)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_memory",
            "description": "Hapus memory tertentu berdasarkan ID. Gunakan recall_memories dulu untuk lihat ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "integer",
                        "description": "ID memory yang ingin dihapus",
                    },
                },
                "required": ["memory_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "weather",
            "description": "Cek cuaca saat ini dan prakiraan 3 hari",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "Nama kota/lokasi (default: Jakarta)",
                    },
                },
                "required": [],
            },
        },
    },
]


# --- Per-Agent Tool Mapping ---

# Which tools each agent can use
AGENT_TOOLS = {
    "agent1": ["delegate_task", "check_balances", "web_search", "weather", "save_memory", "recall_memories", "delete_memory"],
    "agent2": ["generate_image", "generate_video", "generate_audio", "send_file", "run_bash"],
    "agent3": ["web_search", "weather", "run_bash"],
    "agent4": ["run_bash", "send_file", "web_search"],
    # Backup agents mirror their primary
    "agent5": ["delegate_task", "check_balances", "web_search", "weather", "save_memory", "recall_memories", "delete_memory"],
    "agent6": ["generate_image", "generate_video", "generate_audio", "send_file", "run_bash"],
    "agent7": ["web_search", "weather", "run_bash"],
    "agent8": ["run_bash", "send_file", "web_search"],
}

# Tools blocked for child agents during delegation
DELEGATION_BLOCKED_TOOLS = {"delegate_task", "save_memory", "delete_memory"}

# Build lookup: tool_name -> tool_definition
_TOOL_BY_NAME = {t["function"]["name"]: t for t in TOOL_DEFINITIONS}
_TOOL_BY_NAME["delegate_task"] = DELEGATE_TOOL


def get_agent_tools(agent_id: str, is_delegated: bool = False) -> list[dict]:
    """Get tool definitions for a specific agent.

    If is_delegated=True, blocks tools that child agents shouldn't use
    (delegate_task, memory writes) to prevent recursive delegation and
    unauthorized memory modification.
    """
    allowed = AGENT_TOOLS.get(agent_id, [])
    if not allowed:
        return []
    if is_delegated:
        allowed = [t for t in allowed if t not in DELEGATION_BLOCKED_TOOLS]
    return [_TOOL_BY_NAME[name] for name in allowed if name in _TOOL_BY_NAME]


# --- Tool Execution ---

def _safe_env() -> dict:
    """Return environment with only necessary variables for scripts."""
    safe_keys = {"PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL",
                 "TELEGRAM_BOT_TOKEN", "TMPDIR", "TZ"}
    env = {k: v for k, v in os.environ.items()
           if k in safe_keys or k.endswith("_API_KEY") or k.endswith("_KEY")}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


async def _run_script(script_name: str, args: list[str], timeout: int = 120) -> ToolResult:
    """Run a script from the scripts directory."""
    script_path = os.path.join(SCRIPTS_DIR, script_name)
    if not os.path.exists(script_path):
        return ToolResult(name=script_name, output=f"Script not found: {script_path}", success=False)

    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", script_path, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_safe_env(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode(errors="replace")
        if stderr:
            output += "\n" + stderr.decode(errors="replace")
        return ToolResult(
            name=script_name,
            output=output[:4000],
            success=proc.returncode == 0,
        )
    except asyncio.TimeoutError:
        return ToolResult(name=script_name, output=f"Timeout after {timeout}s", success=False)
    except Exception as e:
        return ToolResult(name=script_name, output=str(e), success=False)


async def _run_bash(command: str, timeout: int = 30) -> ToolResult:
    """Run an arbitrary bash command."""
    import re
    # Safety: block dangerous commands
    dangerous = [
        "rm -rf /", "rm -rf /*", "mkfs", "dd if=", "> /dev/sd",
        ":(){ :|:& };:", "chmod -R 777 /", "chown -R", "shutdown",
        "reboot", "init 0", "init 6", "halt", "poweroff",
    ]
    # Normalize whitespace for bypass prevention
    cmd_normalized = re.sub(r'\s+', ' ', command.strip())
    for d in dangerous:
        if d in cmd_normalized:
            return ToolResult(name="bash", output="Blocked: dangerous command", success=False)
    # Block attempts to read sensitive files
    sensitive = ["/etc/shadow", "/etc/gshadow", ".env", "credentials", "id_rsa"]
    for s in sensitive:
        if s in command and ("cat " in command or "head " in command or "tail " in command or "cp " in command):
            return ToolResult(name="bash", output="Blocked: access to sensitive file", success=False)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/root/pawang",
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode(errors="replace")
        if stderr:
            output += "\n" + stderr.decode(errors="replace")
        return ToolResult(
            name="bash",
            output=output[:4000] if output else "(no output)",
            success=proc.returncode == 0,
        )
    except asyncio.TimeoutError:
        return ToolResult(name="bash", output=f"Timeout after {timeout}s", success=False)
    except Exception as e:
        return ToolResult(name="bash", output=str(e), success=False)


async def execute_tool(name: str, arguments: dict, chat_id: str = "",
                       user_id: str = "", agent_id: str = "") -> ToolResult:
    """Execute a tool by name with given arguments."""
    # Inject user/agent context for memory tools
    if name in ("save_memory", "recall_memories", "delete_memory"):
        arguments["_user_id"] = user_id
        arguments["_agent_id"] = agent_id
    log.info(f"Tool call: {name}({arguments})")

    if name == "check_balances":
        return await _run_script("check-balances.sh", [])

    elif name == "generate_image":
        prompt = arguments.get("prompt", "")
        caption = arguments.get("caption", prompt[:50])
        return await _run_script("generate-image.sh", [prompt, caption, chat_id], timeout=180)

    elif name == "generate_video":
        prompt = arguments.get("prompt", "")
        caption = arguments.get("caption", "Video AI")
        return await _run_script("generate-video.sh", [prompt, caption, chat_id], timeout=360)

    elif name == "generate_audio":
        text = arguments.get("text", "")
        caption = arguments.get("caption", "Audio")
        voice = arguments.get("voice", "Aoede")
        provider = arguments.get("provider", "google")
        return await _run_script("generate-audio.sh", [text, caption, voice, provider], timeout=180)

    elif name == "send_file":
        file_path = arguments.get("file_path", "")
        caption = arguments.get("caption", "")
        # Path traversal protection: only allow files in workspace/scripts/tmp
        from pathlib import Path
        resolved = Path(file_path).resolve()
        allowed_dirs = [Path("/root/pawang/workspace"), Path("/root/pawang/scripts"), Path("/tmp")]
        if not any(str(resolved).startswith(str(d)) for d in allowed_dirs):
            return ToolResult(name="send_file", output=f"Blocked: path not in allowed directory", success=False)
        return await _run_script("telegram-send.sh", [file_path, caption, chat_id])

    elif name == "run_bash":
        command = arguments.get("command", "")
        return await _run_bash(command)

    elif name == "web_search":
        from skills.web_search import WebSearchSkill
        skill = WebSearchSkill()
        result = await skill.execute(arguments.get("query", ""))
        return ToolResult(name="web_search", output=result.output, success=result.success)

    elif name == "weather":
        from skills.weather import WeatherSkill
        skill = WeatherSkill()
        result = await skill.execute(arguments.get("location", "Jakarta"))
        return ToolResult(name="weather", output=result.output, success=result.success)

    elif name == "save_memory":
        from core.database import get_db
        from core.memory_guard import scan_memory
        db = get_db()
        content = arguments.get("content", "")
        category = arguments.get("category", "general")
        if not content:
            return ToolResult(name="save_memory", output="Error: content kosong", success=False)
        is_safe, reason = scan_memory(content)
        if not is_safe:
            return ToolResult(name="save_memory", output=f"Memory ditolak: {reason}", success=False)
        user_id = arguments.get("_user_id", "unknown")
        agent_id = arguments.get("_agent_id", "")
        memory_type = arguments.get("memory_type", "user")
        mem_id = db.save_memory(user_id, content, category, agent_id, memory_type=memory_type)
        # Refresh memories in active session
        try:
            from main import agent_manager
            if agent_manager:
                agent_manager.refresh_memories(agent_id, user_id)
        except Exception:
            pass
        return ToolResult(name="save_memory", output=f"Memory disimpan (id={mem_id}, category={category}): {content}", success=True)

    elif name == "recall_memories":
        from core.database import get_db
        db = get_db()
        user_id = arguments.get("_user_id", "unknown")
        query = arguments.get("query", "")
        category = arguments.get("category")
        if query:
            memories = db.search_memories(user_id, query)
        else:
            memories = db.get_memories(user_id, category=category)
        if not memories:
            return ToolResult(name="recall_memories", output="Belum ada memory tersimpan untuk user ini.", success=True)
        lines = []
        for m in memories:
            lines.append(f"[{m['id']}] ({m['category']}) {m['content']}")
        return ToolResult(name="recall_memories", output="\n".join(lines), success=True)

    elif name == "delete_memory":
        from core.database import get_db
        db = get_db()
        user_id = arguments.get("_user_id", "unknown")
        memory_id = arguments.get("memory_id", 0)
        if db.delete_memory(memory_id, user_id):
            return ToolResult(name="delete_memory", output=f"Memory #{memory_id} dihapus.", success=True)
        return ToolResult(name="delete_memory", output=f"Memory #{memory_id} tidak ditemukan.", success=False)

    else:
        return ToolResult(name=name, output=f"Unknown tool: {name}", success=False)
