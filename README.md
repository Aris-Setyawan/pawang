# Pawang AI

**Lightweight Multi-Agent Gateway** — orchestrate multiple LLM providers through a single Telegram bot with a web admin panel.

Pawang connects 7+ LLM providers (Anthropic, OpenAI, Google Gemini, DeepSeek, ModelStudio/Qwen, OpenRouter, Kie.ai) into a unified multi-agent system. Each agent has its own personality, model, and toolset — managed through YAML config and a dark-theme admin panel.

## Features

- **Multi-Agent System** — 8 agents (4 primary + 4 backup) with automatic failover
- **7 LLM Providers** — Anthropic, OpenAI, Gemini, DeepSeek, ModelStudio, OpenRouter, Kie.ai
- **Telegram Bot** — Full-featured bot with voice messages, inline keyboards, vision analysis
- **Smart Routing** — Automatically routes simple queries to cheaper models
- **Admin Panel** — Dark theme SPA with agent monitoring, API key management, usage charts
- **OpenAI-Compatible API** — `/v1/chat/completions` endpoint for third-party clients
- **MCP Integration** — Model Context Protocol server support
- **Mixture of Agents** — Parallel multi-model reasoning with aggregation
- **Tool System** — Bash, web search, weather, file ops, image generation, YouTube
- **Health Monitor** — Auto failover, provider health checks, rate limiting
- **Context Compression** — Smart context window management for long conversations
- **Checkpoint/Rollback** — Save and restore conversation state
- **Event Hooks** — Lifecycle events for extensibility
- **YAML Skills** — Loadable procedure definitions

## Tech Stack

- **Python 3.10+** / Starlette (ASGI) / uvicorn
- **SQLite WAL** with FTS5 full-text search
- **~65MB RAM** footprint
- **httpx** for async HTTP
- **python-telegram-bot** for Telegram integration

## Installation

### 1. Clone

```bash
git clone https://github.com/Aris-Setyawan/pawang.git
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
ANTHROPIC_API_KEY=sk-ant-...
DEEPSEEK_API_KEY=sk-...
OPENAI_API_KEY=sk-proj-...
GEMINI_API_KEY=AIza...
OPENROUTER_API_KEY=sk-or-...
MODELSTUDIO_API_KEY=sk-...
KIEAI_API_KEY=...
TELEGRAM_BOT_TOKEN=123456:ABC...
PANEL_PASSWORD=your-panel-password
```

You only need the providers you plan to use — leave others blank.

### 4. Configure Agents

Edit `config.yaml` to set up providers, agents, models, and channels. Each agent can use a different provider and model:

```yaml
agents:
  - id: agent1
    name: Santa
    model: qwen-plus
    provider: modelstudio
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

Gateway runs on port **18800**, admin panel on port **18801**.

## Admin Panel

Access the panel at `http://your-server:18801/panel` with your `PANEL_PASSWORD`.

Features:
- Real-time agent & provider health monitoring
- API key management (updates runtime + `.env`)
- Agent model/provider hot-configuration
- Usage statistics with Chart.js graphs
- Config reload without restart

## Project Structure

```
pawang/
├── main.py              # Entry point
├── config.yaml          # Agent/provider/model config
├── .env                 # API keys (not in repo)
├── agents/              # Agent manager, delegation
├── api/                 # OpenAI-compatible API server
├── channels/            # Telegram bot, webhook adapters
├── core/                # Provider adapters, tools, health monitor
├── providers/           # OpenAI-compat, Anthropic, Gemini adapters
├── skills/              # Weather, web search, youtube, YAML procedures
├── panel/               # Admin web panel (SPA)
├── prompts/             # System prompts per agent
└── data/                # SQLite database (auto-created)
```

## License

Private project.
