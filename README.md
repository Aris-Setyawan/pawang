# Pawang AI

**Lightweight Multi-Agent Gateway** — orchestrate multiple LLM providers through a single Telegram bot with a web admin panel.

Pawang connects 9+ LLM providers (OpenAI, DeepSeek, Google Gemini, Z.ai/GLM, Anthropic, ModelStudio/Qwen, OpenRouter, SumoPod, Kie.ai) into a unified multi-agent system. Each agent has its own personality, model, and toolset — managed through YAML config and a dark-theme admin panel.

## Features

### Core
- **Multi-Agent System** — 8 agents (4 primary + 4 backup) with automatic failover
- **9 LLM Providers** — OpenAI, DeepSeek, Gemini, Z.ai, Anthropic, ModelStudio, OpenRouter, SumoPod, Kie.ai
- **Dual-Model Agents** — Cheap model for chat, powerful model for tool calling (per agent)
- **Smart Routing** — Automatically routes simple queries to cheaper models (~70% cost savings)
- **Fallback Chains** — Configurable model failover per agent

### Telegram Bot
- **Full-featured Bot** — Voice messages, inline keyboards, vision analysis
- **Background Delegation** — Agents work in background, user stays available to chat
- **Live Monitoring** — `/watch` with multi-agent selection, `/mute`, `/stop`
- **Command Approval** — DM-based approval flow for sensitive commands

### Admin Panel
- **Dark Theme SPA** — Agent monitoring, API key management, usage charts
- **Provider Management** — Add/edit/delete providers with custom endpoints
- **Agent Config** — Edit model, provider, fallbacks, temperature, chat model from UI
- **Token Guard** — Per-agent budgets, spike detection, real-time alerts

### API & Integration
- **OpenAI-Compatible API** — `/v1/chat/completions` endpoint for third-party clients
- **MCP Integration** — Model Context Protocol server support
- **Mixture of Agents** — Parallel multi-model reasoning with aggregation
- **Event Hooks** — Lifecycle events for extensibility
- **YAML Skills** — Loadable procedure definitions

### Tools
- **13 Built-in Tools** — Bash, Python exec, web search, file ops, code search, Wikipedia, translate, calculator, PDF reader, image/video/audio generation, delegation
- **Context Compression** — Smart context window management for long conversations
- **Checkpoint/Rollback** — Save and restore conversation state
- **Agent Memory** — Persistent memory across sessions

## Tech Stack

- **Python 3.10+** / Starlette (ASGI) / uvicorn
- **SQLite WAL** with FTS5 full-text search
- **~65MB RAM** footprint
- **httpx** for async HTTP
- **python-telegram-bot** for Telegram integration

## Quick Start

### 1. Clone

```bash
git clone https://github.com/ArisSetiworker/pawang.git
cd pawang
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your API keys:

```env
DEEPSEEK_API_KEY=sk-...
OPENAI_API_KEY=sk-proj-...
ZAI_API_KEY=abc123.secretkey
TELEGRAM_BOT_TOKEN=123456:ABC...
PANEL_PASSWORD=your-secure-password
```

You only need the providers you plan to use — leave others blank.

### 4. Configure Agents

Edit `config.yaml` to set up providers, agents, models, and channels:

```yaml
agents:
  - id: agent1
    name: Wulan
    model: gpt-5.4
    provider: openai
    chat_model: deepseek-chat     # cheap model for regular chat
    chat_provider: deepseek
    fallbacks:
      - openai/gpt-4.1
      - deepseek/deepseek-chat
    system_prompt_file: prompts/agent1.md
    channels: [telegram]
    temperature: 0.7
```

### 5. Run

```bash
# Development
python3 main.py

# Production
nohup python3 main.py > /tmp/pawang.log 2>&1 &
```

Gateway runs on port **18800**. Admin panel at `/panel`.

## Admin Panel

Access at `http://your-server:18800/panel` with your `PANEL_PASSWORD`.

Features:
- Real-time agent & provider health monitoring
- API key management (updates runtime + `.env`)
- Agent config: model, provider, fallbacks, chat model, temperature
- Provider management: add/edit/delete with custom endpoints
- Token Guard: per-agent budgets and spike detection
- Usage statistics with cost tracking
- Config reload without restart

## Dual-Model System

Agents can use two models simultaneously:
- **Primary model** (`model`/`provider`) — Used for tool calling, delegation, complex tasks
- **Chat model** (`chat_model`/`chat_provider`) — Used for simple chat, cheaper and faster

Detection is automatic via smart routing — simple messages (greetings, short questions) go to the chat model, complex messages (coding, analysis, tools) go to the primary model.

## Project Structure

```
pawang/
├── main.py              # Entry point, uvicorn + Starlette
├── config.yaml          # Agent/provider/model config
├── .env                 # API keys (not in repo)
├── agents/              # Agent manager, delegation, failover
├── api/                 # OpenAI-compatible API server
├── channels/            # Telegram bot, webhook adapters
├── core/                # Config, tools, health, smart routing, token guard
│   ├── approval.py      # Command approval DM flow
│   ├── checkpoint.py    # Conversation checkpoint/rollback
│   ├── file_tools.py    # File read/write/search tools
│   ├── hooks.py         # Event hooks system
│   ├── insights.py      # Usage analytics engine
│   ├── mcp.py           # MCP server integration
│   ├── moa.py           # Mixture of Agents
│   ├── smart_routing.py # Dual-model routing
│   ├── token_guard.py   # Budget enforcement & spike detection
│   ├── user_profile.py  # Auto user profiling
│   └── vision.py        # Image analysis
├── providers/           # OpenAI-compat, Anthropic, Gemini adapters
├── skills/              # Weather, web search, youtube, YAML procedures
├── scripts/             # Image/video/audio generation scripts
├── panel/               # Admin web panel (SPA)
├── prompts/             # System prompts per agent
├── data/                # SQLite database (auto-created)
└── workspace/           # Agent workspace files
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Start bot |
| `/switch` | Switch model/provider (inline keyboard) |
| `/watch` | Monitor background delegations (multi-agent selection) |
| `/mute` | Mute progress updates |
| `/stop` | Stop running tasks |
| `/clear` | Clear conversation history |
| `/export` | Export conversation as file |
| `/rename <name>` | Rename current agent |
| `/ask <agent> <msg>` | Ask a specific agent |
| `/btw <msg>` | Quick side-question |
| `/settings` | Bot settings |

## License

Private project.
