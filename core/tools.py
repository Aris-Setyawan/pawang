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
            "WAJIB PANGGIL tool ini untuk delegasi tugas CODING dan CREATIVE ke agent lain. "
            "Semua tugas coding HARUS didelegasi, jangan dikerjakan sendiri. "
            "agent2=Creative (gambar/video/audio/musik), "
            "agent3=Coder Primary (coding/scripting/bug fix), "
            "agent4=Coder Advanced (architecture/algorithm/deep debug). "
            "PENGECUALIAN — jangan delegate: pertanyaan umum, status system, config, list model, cek saldo."
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
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "Baca isi file di workspace. Untuk lihat file yang dibuat/dimodifikasi.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path ke file (harus di workspace/ atau /tmp)",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Jumlah baris maksimum (default 200)",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": "Tulis/buat file di workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path file tujuan (harus di workspace/ atau /tmp)",
                    },
                    "content": {
                        "type": "string",
                        "description": "Isi file yang ingin ditulis",
                    },
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_search",
            "description": "Cari file berdasarkan pattern di workspace. Contoh: *.py, *.log",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern pencarian (contoh: *.py, *.json)",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch dan ekstrak konten teks dari URL. Untuk membaca halaman web.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL yang ingin di-fetch (http/https)",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "python_exec",
            "description": "Jalankan kode Python. Untuk kalkulasi, data processing, analisis, generate chart, regex, JSON parsing. Lebih aman dan powerful dari bash untuk tugas non-system.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Kode Python yang ingin dijalankan. Gunakan print() untuk output.",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_pdf",
            "description": "Baca dan ekstrak teks dari file PDF. Untuk membaca dokumen, paper, laporan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path ke file PDF",
                    },
                    "pages": {
                        "type": "string",
                        "description": "Range halaman (contoh: '1-5', '3', 'all'). Default: all",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wikipedia",
            "description": "Cari dan baca artikel Wikipedia. Untuk fakta, definisi, referensi pengetahuan umum.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Topik yang ingin dicari di Wikipedia",
                    },
                    "lang": {
                        "type": "string",
                        "description": "Bahasa Wikipedia: 'id' (Indonesia) atau 'en' (English). Default: en",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "translate",
            "description": "Terjemahkan teks antar bahasa menggunakan Google Translate (gratis).",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Teks yang ingin diterjemahkan",
                    },
                    "target": {
                        "type": "string",
                        "description": "Bahasa tujuan (contoh: 'id', 'en', 'ja', 'zh', 'ko', 'fr', 'de')",
                    },
                    "source": {
                        "type": "string",
                        "description": "Bahasa asal (opsional, auto-detect jika kosong)",
                    },
                },
                "required": ["text", "target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "Hitung ekspresi matematika dengan presisi tinggi. Untuk aritmatika, konversi, persentase, statistik.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Ekspresi matematika (contoh: '15000 * 1.11', 'sqrt(144)', '2**10', 'sin(pi/4)')",
                    },
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "code_search",
            "description": "Cari teks/pattern di dalam file-file project. Seperti grep — untuk menemukan fungsi, variabel, string di codebase.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Teks atau regex yang dicari",
                    },
                    "path": {
                        "type": "string",
                        "description": "Direktori atau file untuk search (default: workspace/)",
                    },
                    "file_type": {
                        "type": "string",
                        "description": "Filter tipe file (contoh: '*.py', '*.js', '*.yaml')",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_hub",
            "description": (
                "Browse dan install skill dari Hermes/ClawHub/OpenClaw skill marketplace. "
                "Aksi: search (cari skill), browse (lihat kategori), list (skill terinstall), "
                "install (install skill), uninstall (hapus), read (baca isi skill), info (detail)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["search", "browse", "list", "install", "uninstall", "read", "info"],
                        "description": "Aksi yang dilakukan",
                    },
                    "query": {
                        "type": "string",
                        "description": "Keyword pencarian atau nama kategori (untuk search/browse/install/read/info)",
                    },
                },
                "required": ["action"],
            },
        },
    },
]


# --- Per-Agent Tool Mapping ---

# Which tools each agent can use
AGENT_TOOLS = {
    "agent1": ["delegate_task", "check_balances", "web_search", "web_fetch", "weather",
               "save_memory", "recall_memories", "delete_memory",
               "python_exec", "wikipedia", "translate", "calculator", "code_search",
               "skill_hub"],
    "agent2": ["generate_image", "generate_video", "generate_audio", "send_file", "run_bash",
               "file_read", "file_write", "python_exec", "translate"],
    "agent3": ["web_search", "web_fetch", "weather", "run_bash", "file_read", "file_search",
               "python_exec", "wikipedia", "calculator", "read_pdf", "code_search"],
    "agent4": ["run_bash", "send_file", "web_search", "file_read", "file_write", "file_search",
               "python_exec", "code_search", "read_pdf"],
    # Backup agents mirror their primary
    "agent5": ["delegate_task", "check_balances", "web_search", "web_fetch", "weather",
               "save_memory", "recall_memories", "delete_memory",
               "python_exec", "wikipedia", "translate", "calculator", "code_search",
               "skill_hub"],
    "agent6": ["generate_image", "generate_video", "generate_audio", "send_file", "run_bash",
               "file_read", "file_write", "python_exec", "translate"],
    "agent7": ["web_search", "web_fetch", "weather", "run_bash", "file_read", "file_search",
               "python_exec", "wikipedia", "calculator", "read_pdf", "code_search"],
    "agent8": ["run_bash", "send_file", "web_search", "file_read", "file_write", "file_search",
               "python_exec", "code_search", "read_pdf"],
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

    Also injects MCP tools if MCP servers are connected.
    """
    allowed = AGENT_TOOLS.get(agent_id, [])
    if not allowed:
        return []
    if is_delegated:
        allowed = [t for t in allowed if t not in DELEGATION_BLOCKED_TOOLS]
    tools = [_TOOL_BY_NAME[name] for name in allowed if name in _TOOL_BY_NAME]

    # Inject MCP tools (if available)
    if not is_delegated:
        try:
            from main import mcp_manager
            if mcp_manager:
                tools.extend(mcp_manager.get_all_tools())
        except (ImportError, AttributeError):
            pass

    return tools


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


import re as _re
import unicodedata as _ud

# Regex-based dangerous command patterns (from Hermes approval.py)
_DANGEROUS_PATTERNS = [
    (_re.compile(r'\brm\s+(-[^\s]*\s+)*/'), "recursive delete in root"),
    (_re.compile(r'\brm\s+-[^\s]*r'), "recursive delete"),
    (_re.compile(r'\bchmod\s+(-[^\s]*\s+)*777\b'), "world-writable permissions"),
    (_re.compile(r'\bdd\s+.*if='), "disk copy"),
    (_re.compile(r'>\s*/dev/sd'), "write to block device"),
    (_re.compile(r'\bDROP\s+(TABLE|DATABASE)\b', _re.I), "SQL DROP"),
    (_re.compile(r'\bDELETE\s+FROM\b(?!.*\bWHERE\b)', _re.I), "SQL DELETE without WHERE"),
    (_re.compile(r'\b(curl|wget)\b.*\|\s*(ba)?sh\b'), "pipe remote content to shell"),
    (_re.compile(r'\b(python[23]?|perl|ruby|node)\s+-[ec]\s+'), "script exec via -e/-c"),
    (_re.compile(r'\bsystemctl\s+(stop|disable|mask)\b'), "stop system service"),
    (_re.compile(r'\bkill\s+-9\s+-1\b'), "kill all processes"),
    (_re.compile(r':\(\)\s*\{'), "fork bomb"),
    (_re.compile(r'\bmkfs\b'), "format filesystem"),
    (_re.compile(r'\b(shutdown|reboot|poweroff|halt|init\s+[06])\b'), "system shutdown/reboot"),
]

# Sensitive file patterns
_SENSITIVE_FILES = ["/etc/shadow", "/etc/gshadow", ".env", "credentials", "id_rsa", ".ssh/"]


def _normalize_command(command: str) -> str:
    """Anti-obfuscation: normalize unicode, strip ANSI, collapse whitespace."""
    # Strip ANSI escape sequences
    cmd = _re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', command)
    cmd = _re.sub(r'\x1b\][^\x07]*\x07', '', cmd)
    # Strip null bytes
    cmd = cmd.replace('\x00', '')
    # Unicode NFKC normalization (fullwidth chars -> ASCII)
    cmd = _ud.normalize('NFKC', cmd)
    # Collapse whitespace
    cmd = _re.sub(r'\s+', ' ', cmd.strip())
    return cmd


def _detect_dangerous(command: str) -> tuple[bool, str]:
    """Check command against dangerous patterns. Returns (is_dangerous, reason)."""
    normalized = _normalize_command(command)
    for pattern, desc in _DANGEROUS_PATTERNS:
        if pattern.search(normalized):
            log.warning(f"Dangerous command blocked: {desc} in: {command[:100]}")
            return True, desc

    # Sensitive file access
    for s in _SENSITIVE_FILES:
        if s in normalized and _re.search(r'\b(cat|head|tail|cp|scp|mv|less|more)\b', normalized):
            return True, f"access to sensitive file ({s})"

    return False, ""


async def _run_bash(command: str, timeout: int = 30,
                    user_id: str = "", agent_id: str = "") -> ToolResult:
    """Run an arbitrary bash command."""
    is_dangerous, reason = _detect_dangerous(command)
    if is_dangerous:
        # Try approval flow instead of outright block
        try:
            from core.approval import approval_manager
            if approval_manager._notify:
                approved = await approval_manager.request_approval(
                    command, reason, user_id, agent_id,
                )
                if not approved:
                    return ToolResult(name="bash", output=f"Blocked (denied/timeout): {reason}", success=False)
                # Approved — continue execution
            else:
                return ToolResult(name="bash", output=f"Blocked: {reason}", success=False)
        except Exception:
            return ToolResult(name="bash", output=f"Blocked: {reason}", success=False)

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
        return await _run_bash(command, user_id=user_id, agent_id=agent_id)

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

    elif name == "file_read":
        from core.file_tools import file_read as _file_read
        path = arguments.get("file_path", "")
        max_lines = arguments.get("max_lines", 200)
        result = _file_read(path, max_lines=max_lines)
        return ToolResult(name="file_read", output=result, success=not result.startswith("Error"))

    elif name == "file_write":
        from core.file_tools import file_write as _file_write
        path = arguments.get("file_path", "")
        content = arguments.get("content", "")
        result = _file_write(path, content)
        return ToolResult(name="file_write", output=result, success=not result.startswith("Error"))

    elif name == "file_search":
        from core.file_tools import file_search as _file_search
        pattern = arguments.get("pattern", "*")
        result = _file_search(pattern)
        return ToolResult(name="file_search", output=result, success=True)

    elif name == "web_fetch":
        from core.file_tools import web_fetch_async
        url = arguments.get("url", "")
        result = await web_fetch_async(url)
        return ToolResult(name="web_fetch", output=result, success=not result.startswith("Error"))

    elif name == "python_exec":
        code = arguments.get("code", "")
        return await _run_python(code)

    elif name == "read_pdf":
        file_path = arguments.get("file_path", "")
        pages = arguments.get("pages", "all")
        return await _read_pdf(file_path, pages)

    elif name == "wikipedia":
        query = arguments.get("query", "")
        lang = arguments.get("lang", "en")
        return await _wikipedia_search(query, lang)

    elif name == "translate":
        text = arguments.get("text", "")
        target = arguments.get("target", "en")
        source = arguments.get("source", "")
        return await _translate_text(text, target, source)

    elif name == "calculator":
        expression = arguments.get("expression", "")
        return _calculate(expression)

    elif name == "code_search":
        pattern = arguments.get("pattern", "")
        path = arguments.get("path", "")
        file_type = arguments.get("file_type", "")
        return await _code_search(pattern, path, file_type)

    elif name == "skill_hub":
        from skills.hub import execute_skill_hub
        action = arguments.get("action", "")
        query = arguments.get("query", "")
        result = await execute_skill_hub(action, query=query, name=query)
        return ToolResult(name="skill_hub", output=result, success=True)

    elif name.startswith("mcp_"):
        # MCP tool call — route to MCP manager
        try:
            from main import mcp_manager
            if mcp_manager:
                result = await mcp_manager.call_tool(name, arguments)
                return ToolResult(name=name, output=result, success=True)
            return ToolResult(name=name, output="MCP not initialized", success=False)
        except Exception as e:
            return ToolResult(name=name, output=f"MCP error: {e}", success=False)

    else:
        return ToolResult(name=name, output=f"Unknown tool: {name}", success=False)


# --- New Tool Implementations ---

async def _run_python(code: str, timeout: int = 30) -> ToolResult:
    """Run Python code in a subprocess. Captures stdout + stderr."""
    if not code.strip():
        return ToolResult(name="python_exec", output="Error: kode kosong", success=False)
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", "-c", code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/root/pawang/workspace",
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode(errors="replace")
        if stderr:
            err = stderr.decode(errors="replace")
            output = output + "\n[stderr]\n" + err if output else err
        return ToolResult(
            name="python_exec",
            output=output[:4000] if output else "(no output)",
            success=proc.returncode == 0,
        )
    except asyncio.TimeoutError:
        return ToolResult(name="python_exec", output=f"Timeout after {timeout}s", success=False)
    except Exception as e:
        return ToolResult(name="python_exec", output=str(e), success=False)


async def _read_pdf(file_path: str, pages: str = "all") -> ToolResult:
    """Extract text from PDF using Python (pymupdf or pdfplumber fallback)."""
    from pathlib import Path
    resolved = Path(file_path).resolve()
    if not resolved.exists():
        return ToolResult(name="read_pdf", output=f"File not found: {file_path}", success=False)
    if not str(resolved).endswith(".pdf"):
        return ToolResult(name="read_pdf", output="Error: file bukan PDF", success=False)

    # Use python subprocess to extract text
    code = f'''
import sys
try:
    import fitz  # pymupdf
    doc = fitz.open("{resolved}")
    pages_range = "{pages}"
    if pages_range == "all":
        page_nums = range(len(doc))
    elif "-" in pages_range:
        start, end = pages_range.split("-", 1)
        page_nums = range(int(start)-1, min(int(end), len(doc)))
    else:
        page_nums = [int(pages_range)-1]
    text = ""
    for i in page_nums:
        if 0 <= i < len(doc):
            text += f"--- Page {{i+1}} ---\\n"
            text += doc[i].get_text() + "\\n"
    print(f"PDF: {{len(doc)}} pages, extracted {{len(page_nums)}} pages")
    print(text[:8000])
except ImportError:
    try:
        import subprocess
        result = subprocess.run(["pdftotext", "{resolved}", "-"], capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            print(result.stdout[:8000])
        else:
            print("Error: pdftotext failed:", result.stderr)
    except FileNotFoundError:
        print("Error: install pymupdf (pip install pymupdf) or pdftotext for PDF support")
'''
    return await _run_python(code, timeout=20)


async def _wikipedia_search(query: str, lang: str = "en") -> ToolResult:
    """Search Wikipedia and return article summary."""
    import httpx
    if not query:
        return ToolResult(name="wikipedia", output="Error: query kosong", success=False)
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{query.replace(' ', '_')}"
    headers = {"User-Agent": "Pawang/1.0 (https://github.com/Aris-Setyawan/pawang)"}
    try:
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
            r = await client.get(url, follow_redirects=True)
            if r.status_code == 200:
                data = r.json()
                title = data.get("title", query)
                extract = data.get("extract", "No content")
                desc = data.get("description", "")
                page_url = data.get("content_urls", {}).get("desktop", {}).get("page", "")
                output = f"# {title}\n"
                if desc:
                    output += f"_{desc}_\n\n"
                output += extract
                if page_url:
                    output += f"\n\nSource: {page_url}"
                return ToolResult(name="wikipedia", output=output[:4000], success=True)
            elif r.status_code == 404:
                # Try search API
                search_url = f"https://{lang}.wikipedia.org/w/api.php?action=opensearch&search={query}&limit=5&format=json"
                sr = await client.get(search_url)
                if sr.status_code == 200:
                    data = sr.json()
                    titles = data[1] if len(data) > 1 else []
                    if titles:
                        return ToolResult(
                            name="wikipedia",
                            output=f"Artikel '{query}' tidak ditemukan. Mungkin maksud:\n" + "\n".join(f"- {t}" for t in titles),
                            success=True,
                        )
                return ToolResult(name="wikipedia", output=f"Artikel '{query}' tidak ditemukan di Wikipedia {lang}.", success=False)
            else:
                return ToolResult(name="wikipedia", output=f"Wikipedia API error: {r.status_code}", success=False)
    except Exception as e:
        return ToolResult(name="wikipedia", output=f"Error: {e}", success=False)


async def _translate_text(text: str, target: str, source: str = "") -> ToolResult:
    """Translate text using Google Translate (free, unofficial API)."""
    import httpx
    import urllib.parse
    if not text:
        return ToolResult(name="translate", output="Error: teks kosong", success=False)
    src = source or "auto"
    encoded = urllib.parse.quote(text[:3000])
    url = (
        f"https://translate.googleapis.com/translate_a/single"
        f"?client=gtx&sl={src}&tl={target}&dt=t&q={encoded}"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            if r.status_code == 200:
                data = r.json()
                translated = "".join(part[0] for part in data[0] if part[0])
                detected = data[2] if len(data) > 2 else src
                return ToolResult(
                    name="translate",
                    output=f"[{detected} -> {target}]\n{translated}",
                    success=True,
                )
            return ToolResult(name="translate", output=f"Translation API error: {r.status_code}", success=False)
    except Exception as e:
        return ToolResult(name="translate", output=f"Error: {e}", success=False)


def _calculate(expression: str) -> ToolResult:
    """Evaluate math expression safely using Python's math module."""
    import math
    if not expression:
        return ToolResult(name="calculator", output="Error: ekspresi kosong", success=False)
    # Whitelist safe names
    safe_names = {
        k: v for k, v in math.__dict__.items()
        if not k.startswith("_")
    }
    safe_names.update({
        "abs": abs, "round": round, "min": min, "max": max,
        "sum": sum, "len": len, "int": int, "float": float,
        "pow": pow, "divmod": divmod,
    })
    try:
        result = eval(expression, {"__builtins__": {}}, safe_names)
        return ToolResult(name="calculator", output=f"{expression} = {result}", success=True)
    except Exception as e:
        return ToolResult(name="calculator", output=f"Error: {e}\nExpression: {expression}", success=False)


async def _code_search(pattern: str, path: str = "", file_type: str = "") -> ToolResult:
    """Search for text/pattern in files using grep."""
    if not pattern:
        return ToolResult(name="code_search", output="Error: pattern kosong", success=False)
    search_path = path or "/root/pawang/workspace"
    # Build grep command
    cmd = ["grep", "-rn", "--color=never", "-I"]
    if file_type:
        cmd.extend(["--include", file_type])
    cmd.extend(["-m", "50", pattern, search_path])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        output = stdout.decode(errors="replace")
        if not output:
            return ToolResult(name="code_search", output=f"No matches for '{pattern}' in {search_path}", success=True)
        lines = output.strip().split("\n")
        header = f"Found {len(lines)} match(es) for '{pattern}':\n\n"
        return ToolResult(name="code_search", output=(header + output)[:4000], success=True)
    except asyncio.TimeoutError:
        return ToolResult(name="code_search", output="Timeout", success=False)
    except Exception as e:
        return ToolResult(name="code_search", output=str(e), success=False)
