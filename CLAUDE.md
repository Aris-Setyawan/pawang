# Pawang AI — Custom Multi-Agent Gateway

## Overview
Pawang adalah custom multi-agent gateway (Python/Starlette) — alternatif ringan dari OpenClaw.
- **Stack**: Python 3.10, Starlette (ASGI), httpx, python-telegram-bot, uvicorn, SQLite WAL
- **RAM**: ~65MB (vs OpenClaw ~400MB)
- **Port**: 18800
- **DB**: `data/pawang.db` (messages, sessions, usage)
- **Log**: `/tmp/pawang.log`

## Project Structure
```
pawang/
├── main.py              # Entry point, uvicorn + Starlette
├── config.yaml          # Agent/model/provider config
├── .env                 # API keys, bot token
├── agents/              # Agent manager, delegation
├── channels/            # Telegram bot handler
├── core/                # Provider adapters, tools, health monitor
├── providers/           # OpenAI-compat, Anthropic, Gemini adapters
├── skills/              # Weather, web search, youtube
├── scripts/             # generate-image, check-balances, etc
├── panel/               # Admin web panel
├── prompts/             # System prompts per agent
├── data/                # SQLite DB
├── tools/               # Tool definitions
└── workspace/           # Agent workspace files
```

## Running
```bash
# Development
cd /root/openclaw/pawang && python3 main.py

# Production (masih nohup, belum systemd)
nohup python3 /root/openclaw/pawang/main.py > /tmp/pawang.log 2>&1 &

# Original location (still active)
/root/pawang/
```

## Current Status
- Phase 1–4 complete
- All agents on Qwen/ModelStudio (OpenRouter habis, DeepSeek reserved for OpenClaw)
- Telegram bot token: 8736732111 (separate from OpenClaw's 8746504916)

## Completed Phases
- **Phase 1**: Core gateway, provider adapters, agent manager, tool system
- **Phase 2**: Telegram bot, streaming, delegation, health monitor, admin panel
- **Phase 3**: Systemd service, agent memory, scheduler, voice messages, inline keyboard, settings UI
- **Phase 4**: Iteration budget, memory injection scanning, context compression, persistent scheduler, tool progress feedback
- **Phase 5**: Subagent isolation, platform formatting hints, dual memory, rate limiting, /export command
- **Audit**: 23 fixes (4 critical, 8 high, 4 medium, 7 low)

## Important Notes
- Pawang runs alongside OpenClaw — different bot token, different port, no conflict
- Config is YAML-based (`config.yaml`), NOT JSON like OpenClaw
- Provider adapters are in `providers/` — each implements a common interface
- Tool system: `core/tools.py` has AGENT_TOOLS mapping + execute_tool dispatcher
