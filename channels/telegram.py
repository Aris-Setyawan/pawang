"""Telegram Bot — task orchestrator, intent classifier, streaming."""

import asyncio
import math
import time
from typing import Optional

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)
from telegram.constants import ChatAction, ParseMode

from core.config import PawangConfig
from core.database import get_db
from core.logger import log
from core.tasks import TaskManager, TaskState
from core.intent import classify_intent
from core import completion
from agents.manager import AgentManager
from providers.base import Message, ThinkingConfig
from skills.manager import SkillManager


EDIT_INTERVAL = 1.5
EDIT_CHAR_THRESHOLD = 80
MODELS_PER_PAGE = 8  # models per page in inline keyboard


class TelegramBot:
    """Telegram bot with inline keyboard model picker."""

    def __init__(self, config: PawangConfig, agent_manager: AgentManager):
        self.config = config
        self.manager = agent_manager
        self.tasks = TaskManager()
        self.skill_manager = SkillManager()
        self.app: Optional[Application] = None
        self._user_agent: dict[int, str] = {}
        self._waiting_key: dict[int, str] = {}
        self._disabled_keys: dict[str, str] = {}
        self._task_messages: dict[str, object] = {}  # user_id -> telegram message being edited
        self._thinking_mode: dict[int, str] = {}  # user_id -> effort level

    def _get_agent_id(self, user_id: int) -> str:
        return self._user_agent.get(user_id, self.config.telegram.default_agent)

    def _is_allowed(self, user_id: int) -> bool:
        if not self.config.telegram.allowed_users:
            return True
        return user_id in self.config.telegram.allowed_users

    # --- Provider Status ---

    def _get_provider_status(self, provider_name: str) -> tuple[str, str]:
        """Get provider status indicator and label.

        Returns (emoji, label):
          \U0001f7e2 Green  = healthy, key valid
          \U0001f7e1 Yellow = rate limited or degraded
          \U0001f534 Red    = invalid key, down, or disabled
        """
        prov = self.config.get_provider(provider_name)
        if not prov or not prov.api_key:
            return "\U0001f534", "OFF"

        try:
            from main import health_monitor
            if health_monitor:
                status = health_monitor.get_status(provider_name)
                if status:
                    if not status.auth_valid:
                        return "\U0001f534", "INVALID KEY"
                    if not status.healthy:
                        return "\U0001f534", "DOWN"
                    if status.rate_limited:
                        return "\U0001f7e1", "RATE LIMITED"
                    if status.consecutive_failures > 0:
                        return "\U0001f7e1", "DEGRADED"
                    return "\U0001f7e2", "OK"
        except ImportError:
            pass

        return "\U0001f7e2", "OK"

    # --- Inline Keyboard Builders ---

    def _build_provider_keyboard(self) -> InlineKeyboardMarkup:
        """Build provider selection grid (2 columns)."""
        models = self.manager.list_available_models()
        by_provider: dict[str, int] = {}
        for m in models:
            by_provider[m["provider"]] = by_provider.get(m["provider"], 0) + 1

        buttons = []
        row = []
        for provider, count in sorted(by_provider.items()):
            row.append(InlineKeyboardButton(
                f"{provider} ({count})",
                callback_data=f"prov:{provider}:0",
            ))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        return InlineKeyboardMarkup(buttons)

    def _build_model_keyboard(self, provider: str, page: int = 0) -> InlineKeyboardMarkup:
        """Build model selection grid with pagination."""
        prov = self.config.get_provider(provider)
        if not prov:
            return InlineKeyboardMarkup([])

        all_models = prov.models
        total = len(all_models)
        total_pages = max(1, math.ceil(total / MODELS_PER_PAGE))
        page = max(0, min(page, total_pages - 1))

        start = page * MODELS_PER_PAGE
        end = start + MODELS_PER_PAGE
        page_models = all_models[start:end]

        # Model buttons (1 per row for readability)
        buttons = []
        for model_id in page_models:
            # Shorten display name if too long
            display = model_id
            if len(display) > 40:
                display = display[:37] + "..."
            buttons.append([InlineKeyboardButton(
                display,
                callback_data=f"model:{provider}:{model_id[:45]}",
            )])

        # Navigation row
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(
                "<< Prev", callback_data=f"prov:{provider}:{page - 1}",
            ))
        nav_row.append(InlineKeyboardButton(
            f"{page + 1}/{total_pages}", callback_data="noop",
        ))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(
                "Next >>", callback_data=f"prov:{provider}:{page + 1}",
            ))
        buttons.append(nav_row)

        # Back to providers
        buttons.append([InlineKeyboardButton(
            "<< Back to Providers", callback_data="providers",
        )])

        return InlineKeyboardMarkup(buttons)

    def _build_agent_keyboard(self, current_agent_id: str) -> InlineKeyboardMarkup:
        """Build agent selection grid."""
        buttons = []
        for agent in self.config.agents:
            marker = " *" if agent.id == current_agent_id else ""
            buttons.append([InlineKeyboardButton(
                f"{agent.name} ({agent.id}){marker}",
                callback_data=f"agent:{agent.id}",
            )])
        return InlineKeyboardMarkup(buttons)

    # --- Callback Handler ---

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard button presses."""
        query = update.callback_query
        if not query or not query.data:
            return
        await query.answer()

        user_id = query.from_user.id
        if not self._is_allowed(user_id):
            return

        data = query.data

        if data == "noop":
            return

        if data == "providers":
            # Show provider list
            keyboard = self._build_provider_keyboard()
            await query.edit_message_text(
                "Select a provider:", reply_markup=keyboard,
            )

        elif data.startswith("prov:"):
            # Show models for provider
            parts = data.split(":")
            provider = parts[1]
            page = int(parts[2]) if len(parts) > 2 else 0

            # Refresh OpenRouter if needed
            if provider == "openrouter":
                await self._refresh_openrouter_models()

            prov = self.config.get_provider(provider)
            count = len(prov.models) if prov else 0
            keyboard = self._build_model_keyboard(provider, page)
            await query.edit_message_text(
                f"{provider} — {count} models\nSelect a model:",
                reply_markup=keyboard,
            )

        elif data.startswith("model:"):
            # Switch model — resolve truncated ID
            parts = data.split(":", 2)
            provider = parts[1]
            model_prefix = parts[2]

            # Find full model ID (callback_data may be truncated)
            prov = self.config.get_provider(provider)
            model = model_prefix
            if prov:
                for m in prov.models:
                    if m.startswith(model_prefix):
                        model = m
                        break

            agent_id = self._get_agent_id(user_id)
            session = self.manager.get_session(agent_id, str(user_id))
            self.manager.switch_model(session, provider, model)

            await query.edit_message_text(
                f"Switched to {provider}/{model}"
            )

        elif data.startswith("mgmt:"):
            # Show provider detail
            provider = data.split(":")[1]
            await self._show_provider_detail(query, provider)

        elif data.startswith("editkey:"):
            # Enter key-edit mode — next text message = new key
            provider = data.split(":")[1]
            # Store state: waiting for key input
            self._waiting_key[user_id] = provider
            await query.edit_message_text(
                f"Send the new API key for {provider}:\n\n"
                f"(message will be auto-deleted for security)",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Cancel", callback_data=f"mgmt:{provider}"),
                ]]),
            )

        elif data.startswith("toggle:"):
            # Enable/disable provider
            provider = data.split(":")[1]
            prov = self.config.get_provider(provider)
            if not prov:
                return

            if prov.api_key:
                # Disable — backup key and clear
                self._disabled_keys[provider] = prov.api_key
                prov.api_key = ""
                from core.completion import reset_providers
                reset_providers()
                await query.answer("Disabled!")
            else:
                # Enable — restore backed-up key
                backed = self._disabled_keys.get(provider, "")
                if backed:
                    prov.api_key = backed
                    del self._disabled_keys[provider]
                    from core.completion import reset_providers
                    reset_providers()
                    await query.answer("Enabled!")
                else:
                    await query.answer("No key to restore. Use Edit API Key.")

            # Refresh detail view
            await self._show_provider_detail(query, provider)

        elif data == "mgmt_back":
            keyboard = self._build_provider_mgmt_keyboard()
            await query.edit_message_text(
                "Provider Management\nPilih provider:", reply_markup=keyboard,
            )

        elif data.startswith("agent:"):
            agent_id = data.split(":")[1]
            agent = self.config.get_agent(agent_id)
            if agent:
                self._user_agent[user_id] = agent_id
                await query.edit_message_text(
                    f"Switched to agent {agent.name} ({agent.id})"
                )

    # --- Commands ---

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return
        agent = self.config.get_agent(self._get_agent_id(update.effective_user.id))
        name = agent.name if agent else "Pawang"
        await update.message.reply_text(
            f"Hai! Saya {name}, powered by Pawang.\n\n"
            "Commands:\n"
            "/models - Manage API keys\n"
            "/switch - Ganti model\n"
            "/agent - Pilih agent\n"
            "/ask <agent> <tanya> - Tanya agent lain\n"
            "/btw <tanya> - Quick question (no history)\n"
            "/think [level] - Toggle thinking mode\n"
            "/skill [name] - Run a skill\n"
            "/clear - Reset percakapan\n"
            "/status - Status saat ini\n"
            "/usage - Statistik penggunaan"
        )

    def _build_provider_mgmt_keyboard(self) -> InlineKeyboardMarkup:
        """Build provider management grid — shows status emoji per provider."""
        buttons = []
        row = []
        for name in sorted(self.config.providers.keys()):
            emoji, _ = self._get_provider_status(name)
            prov = self.config.get_provider(name)
            count = len(prov.models) if prov else 0
            label = f"{emoji} {name} ({count})"
            row.append(InlineKeyboardButton(
                label, callback_data=f"mgmt:{name}",
            ))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        return InlineKeyboardMarkup(buttons)

    def _build_provider_detail_keyboard(self, provider_name: str) -> InlineKeyboardMarkup:
        """Build detail view for a provider — edit key, enable/disable."""
        prov = self.config.get_provider(provider_name)
        has_key = bool(prov.api_key) if prov else False

        toggle_label = "\U0001f534 Disable" if has_key else "\U0001f7e2 Enable"
        buttons = [
            [InlineKeyboardButton(
                "\u270f\ufe0f Edit API Key", callback_data=f"editkey:{provider_name}",
            )],
            [InlineKeyboardButton(
                toggle_label, callback_data=f"toggle:{provider_name}",
            )],
            [InlineKeyboardButton(
                "\u2b05 Back", callback_data="mgmt_back",
            )],
        ]
        return InlineKeyboardMarkup(buttons)

    async def _show_provider_detail(self, query, provider: str):
        """Show provider detail with status emoji."""
        prov = self.config.get_provider(provider)
        if not prov:
            return

        emoji, label = self._get_provider_status(provider)
        has_key = bool(prov.api_key)
        masked = (prov.api_key[:8] + "..." + prov.api_key[-4:]) if has_key else "not set"

        # Get latency from health monitor
        latency = ""
        try:
            from main import health_monitor
            if health_monitor:
                st = health_monitor.get_status(provider)
                if st and st.latency_ms > 0:
                    latency = f"\nLatency: {st.latency_ms:.0f}ms"
                if st and st.total_requests > 0:
                    latency += f"\nRequests: {st.total_requests} ({st.total_errors} errors)"
        except ImportError:
            pass

        keyboard = self._build_provider_detail_keyboard(provider)
        await query.edit_message_text(
            f"{emoji} {provider}\n"
            f"Status: {label}\n"
            f"API Key: {masked}\n"
            f"Format: {prov.api_format}\n"
            f"Models: {len(prov.models)}"
            f"{latency}",
            reply_markup=keyboard,
        )

    async def _cmd_models(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Provider management — view/edit API keys, enable/disable providers."""
        if not self._is_allowed(update.effective_user.id):
            return

        keyboard = self._build_provider_mgmt_keyboard()
        await update.message.reply_text(
            "Provider Management\nPilih provider:", reply_markup=keyboard,
        )

    def _update_env_key(self, provider_name: str, new_key: str):
        """Update API key in .env file."""
        from pathlib import Path
        env_path = Path(__file__).parent.parent / ".env"

        # Map provider name to env var
        env_map = {
            "anthropic": "ANTHROPIC_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "openai": "OPENAI_API_KEY",
            "google": "GEMINI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "modelstudio": "MODELSTUDIO_API_KEY",
            "kieai": "KIEAI_API_KEY",
        }
        env_var = env_map.get(provider_name)
        if not env_var or not env_path.exists():
            return

        lines = env_path.read_text().splitlines()
        updated = False
        for i, line in enumerate(lines):
            if line.startswith(f"{env_var}=") or line.startswith(f"# {env_var}"):
                lines[i] = f"{env_var}={new_key}"
                updated = True
                break
        if not updated:
            lines.append(f"{env_var}={new_key}")

        env_path.write_text("\n".join(lines) + "\n")
        log.info(f"Updated {env_var} in .env")

    async def _cmd_switch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Switch model — inline keyboard or direct command."""
        if not self._is_allowed(update.effective_user.id):
            return
        args = context.args
        if not args or len(args) < 2:
            # No args — show inline keyboard picker
            await self._refresh_openrouter_models()
            keyboard = self._build_provider_keyboard()
            await update.message.reply_text(
                "Select a provider:", reply_markup=keyboard,
            )
            return

        provider_name, model_name = args[0], args[1]
        provider = self.config.get_provider(provider_name)
        if not provider:
            await update.message.reply_text(
                f"Provider '{provider_name}' tidak ditemukan.\n"
                f"Available: {', '.join(self.config.providers.keys())}"
            )
            return

        if provider.models and model_name not in provider.models:
            await update.message.reply_text(
                f"Model '{model_name}' tidak ada di {provider_name}."
            )
            return

        user_id = str(update.effective_user.id)
        agent_id = self._get_agent_id(update.effective_user.id)
        session = self.manager.get_session(agent_id, user_id)
        self.manager.switch_model(session, provider_name, model_name)

        await update.message.reply_text(
            f"Switched to {provider_name}/{model_name}"
        )

    async def _cmd_agent(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return
        args = context.args

        if not args:
            # Show inline keyboard
            current = self._get_agent_id(update.effective_user.id)
            keyboard = self._build_agent_keyboard(current)
            await update.message.reply_text(
                "Select an agent:", reply_markup=keyboard,
            )
            return

        agent_id = args[0]
        agent = self.config.get_agent(agent_id)
        if not agent:
            await update.message.reply_text(f"Agent '{agent_id}' tidak ditemukan.")
            return

        self._user_agent[update.effective_user.id] = agent_id
        await update.message.reply_text(
            f"Switched to agent {agent.name} ({agent.id})"
        )

    async def _cmd_ask(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return
        args = context.args
        if not args or len(args) < 2:
            await update.message.reply_text(
                "Usage: /ask <agent_id> <pertanyaan>\n"
                "Example: /ask agent4 buatkan fungsi fibonacci"
            )
            return

        target_agent_id = args[0]
        question = " ".join(args[1:])
        from_agent_id = self._get_agent_id(update.effective_user.id)
        user_id = str(update.effective_user.id)

        target_agent = self.config.get_agent(target_agent_id)
        if not target_agent:
            await update.message.reply_text(f"Agent '{target_agent_id}' tidak ditemukan.")
            return

        msg = await update.message.reply_text(f"Asking {target_agent.name}...")

        chat_id = str(update.effective_chat.id)
        response = await self.manager.delegate(
            from_agent_id, target_agent_id, user_id, question,
            chat_id=chat_id,
        )

        db = get_db()
        session_key = f"{from_agent_id}:{user_id}"
        db.save_message(session_key, from_agent_id, user_id, "user",
                        f"[Delegated to {target_agent.name}]: {question}")
        db.save_message(session_key, from_agent_id, user_id, "assistant",
                        f"[{target_agent.name}]: {response}")

        text = f"{target_agent.name}:\n\n{response}"
        if len(text) <= 4096:
            await msg.edit_text(text)
        else:
            await msg.edit_text(text[:4096])
            remaining = text[4096:]
            while remaining:
                await update.message.reply_text(remaining[:4096])
                remaining = remaining[4096:]

    async def _cmd_btw(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Quick side question — doesn't touch main conversation history.
        Uses fastest model available. Like interrupting with 'btw, ...'"""
        if not self._is_allowed(update.effective_user.id):
            return
        args = context.args
        if not args:
            await update.message.reply_text(
                "Usage: /btw <pertanyaan cepat>\n"
                "Quick side question tanpa ganggu conversation utama."
            )
            return

        question = " ".join(args)
        user_id = str(update.effective_user.id)
        await update.message.chat.send_action(ChatAction.TYPING)

        # Pick fastest model — priority: deepseek > google > openai > anything
        fast_providers = [
            ("deepseek", "deepseek-chat"),
            ("google", "gemini-2.0-flash"),
            ("openai", "gpt-4o-mini"),
        ]
        fast_provider = None
        fast_model = None
        for pname, mname in fast_providers:
            prov = self.config.get_provider(pname)
            if prov and prov.api_key:
                fast_provider = pname
                fast_model = mname
                break

        if not fast_provider:
            # Fallback to whatever is available
            for name, prov in self.config.providers.items():
                if prov.api_key and prov.models:
                    fast_provider = name
                    fast_model = prov.models[0]
                    break

        if not fast_provider:
            await update.message.reply_text("No provider available.")
            return

        try:
            messages = [
                Message(role="system", content=(
                    "You are a quick assistant. Answer concisely in 1-3 sentences. "
                    "Match the user's language (Indonesian/English)."
                )),
                Message(role="user", content=question),
            ]

            response = await completion.complete(
                config=self.config,
                provider_name=fast_provider,
                model=fast_model,
                messages=messages,
                temperature=0.5,
                max_tokens=500,
            )

            header = f"[btw — {fast_model}]\n\n"
            text = header + response.text
            await update.message.reply_text(text[:4096])

            # Record usage but NOT in session history
            db = get_db()
            db.record_usage(fast_provider, fast_model,
                            self._get_agent_id(update.effective_user.id),
                            user_id, len(question) // 4, len(response.text) // 4, 0)

        except Exception as e:
            log.error(f"BTW error: {e}")
            await update.message.reply_text(f"Error: {str(e)[:200]}")

    async def _cmd_think(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle thinking/reasoning mode.
        /think        → toggle on/off (default: high)
        /think high   → set effort level
        /think off    → disable
        """
        if not self._is_allowed(update.effective_user.id):
            return
        uid = update.effective_user.id
        args = context.args

        if not args:
            # Toggle
            if uid in self._thinking_mode:
                del self._thinking_mode[uid]
                await update.message.reply_text("Thinking mode: OFF")
            else:
                self._thinking_mode[uid] = "high"
                await update.message.reply_text(
                    "Thinking mode: ON (high)\n\n"
                    "Model akan 'berpikir' lebih dalam sebelum jawab.\n"
                    "Levels: low | medium | high | max\n"
                    "Usage: /think [level] atau /think off"
                )
            return

        level = args[0].lower()
        if level in ("off", "0", "false"):
            self._thinking_mode.pop(uid, None)
            await update.message.reply_text("Thinking mode: OFF")
        elif level in ("low", "medium", "high", "max"):
            self._thinking_mode[uid] = level
            await update.message.reply_text(f"Thinking mode: ON ({level})")
        else:
            await update.message.reply_text(
                "Usage: /think [low|medium|high|max|off]"
            )

    async def _cmd_skill(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Execute a skill or list available skills.
        /skill            → list all skills
        /skill <name> ... → execute skill
        """
        if not self._is_allowed(update.effective_user.id):
            return

        args = context.args
        if not args:
            # List skills
            skills = self.skill_manager.list_skills()
            lines = ["Available Skills:\n"]
            for s in skills:
                lines.append(f"  {s.name} — {s.description}")
            lines.append(f"\nUsage: /skill <name> <args>")
            await update.message.reply_text("\n".join(lines))
            return

        skill_name = args[0]
        skill_args = " ".join(args[1:])

        await update.message.chat.send_action(ChatAction.TYPING)

        result = await self.skill_manager.execute(
            skill_name, skill_args, config=self.config
        )

        prefix = "" if result.success else "Error: "
        text = f"[{skill_name}]\n\n{prefix}{result.output}"
        if len(text) > 4096:
            text = text[:4093] + "..."
        await update.message.reply_text(text)

    async def _cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return
        user_id = str(update.effective_user.id)
        agent_id = self._get_agent_id(update.effective_user.id)
        self.manager.clear_session(agent_id, user_id)
        await update.message.reply_text("Conversation cleared.")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return
        user_id = str(update.effective_user.id)
        agent_id = self._get_agent_id(update.effective_user.id)
        agent = self.config.get_agent(agent_id)
        session = self.manager.get_session(agent_id, user_id)
        provider, model = self.manager.get_agent_model(session)

        health_line = ""
        try:
            from main import health_monitor
            if health_monitor:
                status = health_monitor.get_status(provider)
                if status:
                    state = "OK" if status.healthy else "DOWN"
                    health_line = f"Health: {state} ({status.latency_ms:.0f}ms)\n"
        except ImportError:
            pass

        thinking_line = ""
        uid = update.effective_user.id
        if uid in self._thinking_mode:
            thinking_line = f"Thinking: ON ({self._thinking_mode[uid]})\n"

        await update.message.reply_text(
            f"Status:\n"
            f"Agent: {agent.name} ({agent.id})\n"
            f"Provider: {provider}\n"
            f"Model: {model}\n"
            f"{thinking_line}"
            f"{health_line}"
            f"Messages: {len(session.messages)}\n"
            f"Est. tokens: ~{session.token_estimate:,}"
        )

    async def _cmd_usage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return
        db = get_db()
        stats = db.get_usage_stats(hours=24)
        total = db.get_total_stats()

        lines = ["Usage (24h):\n"]
        if stats:
            for s in stats:
                lines.append(
                    f"  {s['provider']}/{s['model']}: "
                    f"{s['requests']} req, "
                    f"{s['avg_latency']:.0f}ms avg, "
                    f"{s['errors']} err"
                )
        else:
            lines.append("  No requests yet")

        lines.append(f"\nTotal:")
        lines.append(f"  Messages: {total['messages']['total_messages']}")
        lines.append(f"  Sessions: {total['messages']['total_sessions']}")
        lines.append(f"  API calls: {total['usage']['total_requests'] or 0}")

        await update.message.reply_text("\n".join(lines))

    # --- Task-Aware Message Handler ---

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.message.text:
            return
        if not self._is_allowed(update.effective_user.id):
            return

        # Check if user is in key-edit mode
        uid = update.effective_user.id
        if uid in self._waiting_key:
            provider_name = self._waiting_key.pop(uid)
            new_key = update.message.text.strip()
            try:
                await update.message.delete()
            except Exception:
                pass
            prov = self.config.get_provider(provider_name)
            if prov:
                prov.api_key = new_key
                self._update_env_key(provider_name, new_key)
                from core.completion import reset_providers
                reset_providers()
                masked = new_key[:8] + "..." + new_key[-4:] if len(new_key) > 12 else new_key
                keyboard = self._build_provider_detail_keyboard(provider_name)
                await update.message.chat.send_message(
                    f"API key {provider_name} updated!\nKey: {masked}",
                    reply_markup=keyboard,
                )
            return

        user_id = str(update.effective_user.id)
        active_task = self.tasks.get_active_task(user_id)

        # --- If a task is running, classify intent first ---
        if active_task:
            intent = await classify_intent(
                self.config,
                update.message.text,
                active_task.prompt,
            )
            log.info(f"Task active for {user_id}, intent: {intent}")

            if intent == "stop":
                await self._handle_stop(update, user_id, active_task)
                return

            elif intent == "info":
                await self._handle_side_question(update, user_id)
                return

            elif intent == "continue":
                paused = self.tasks.get_paused_task(user_id)
                if paused:
                    await self._handle_resume(update, user_id, paused)
                else:
                    await update.message.reply_text("No paused task to continue.")
                return

            elif intent == "modify":
                # Stop current, start modified version
                active_task.request_cancel()
                active_task.state = TaskState.CANCELLED
                await update.message.reply_text("[Task modified, restarting...]")
                # Fall through to start new task with this message

            elif intent == "new_task":
                # Cancel old, start new
                active_task.request_cancel()
                active_task.state = TaskState.CANCELLED
                await update.message.reply_text("[New task, previous cancelled]")
                # Fall through to start new task

        # --- Check for paused task and "continue" message ---
        paused = self.tasks.get_paused_task(user_id)
        if paused and update.message.text.lower().strip() in (
            "lanjut", "continue", "resume", "gas", "lanjutkan"
        ):
            await self._handle_resume(update, user_id, paused)
            return

        # --- Start new task ---
        await self._run_task(update, user_id)

    async def _handle_stop(self, update: Update, user_id: str, task):
        """Stop/pause running task, save partial response."""
        task = self.tasks.pause_task(user_id)
        if task:
            partial = task.partial_response
            saved_len = len(partial)
            await update.message.reply_text(
                f"Task paused.\n"
                f"Saved: {saved_len} chars partial response.\n\n"
                f"Say 'lanjut' to resume, or send new message for new task."
            )
        else:
            await update.message.reply_text("No running task to stop.")

    async def _handle_side_question(self, update: Update, user_id: str):
        """Answer a side question without stopping the task (like /btw)."""
        # Use fast model, don't touch session
        fast_provider, fast_model = self._pick_fast_model()
        if not fast_provider:
            await update.message.reply_text("No provider available for side question.")
            return

        await update.message.chat.send_action(ChatAction.TYPING)

        try:
            messages = [
                Message(role="system", content=(
                    "Answer concisely in 1-3 sentences. "
                    "Match the user's language. This is a side question."
                )),
                Message(role="user", content=update.message.text),
            ]
            response = await completion.complete(
                config=self.config,
                provider_name=fast_provider,
                model=fast_model,
                messages=messages,
                temperature=0.5,
                max_tokens=500,
            )
            await update.message.reply_text(
                f"[side answer — task still running]\n\n{response.text[:4000]}"
            )
        except Exception as e:
            await update.message.reply_text(f"Side question error: {str(e)[:200]}")

    async def _handle_resume(self, update: Update, user_id: str, task):
        """Resume a paused task."""
        task.resume()
        await update.message.reply_text(
            f"[Resuming task...]\n"
            f"Previous partial: {len(task.partial_response)} chars"
        )
        # Re-run the task, appending to partial response
        await self._run_task(update, user_id, resume_from=task)

    def _pick_fast_model(self) -> tuple[Optional[str], Optional[str]]:
        """Pick fastest available model for side questions."""
        for pname, mname in [
            ("deepseek", "deepseek-chat"),
            ("google", "gemini-2.0-flash"),
            ("openai", "gpt-4o-mini"),
        ]:
            prov = self.config.get_provider(pname)
            if prov and prov.api_key:
                return pname, mname
        # Fallback
        for name, prov in self.config.providers.items():
            if prov.api_key and prov.models:
                return name, prov.models[0]
        return None, None

    async def _run_tool_loop(self, update: Update, user_id: str, session,
                              agent_id: str, provider_name: str, model: str,
                              tools: list[dict]):
        """Run non-streaming tool execution loop.

        Agent calls tools (check_balances, delegate_task, generate_image, etc.),
        results feed back, repeats until the LLM gives a final text answer.
        """
        import json
        from core.tools import execute_tool

        start_time = time.monotonic()
        task = self.tasks.create_task(user_id, agent_id,
                                      update.message.text, provider_name, model)

        # Build thinking config
        thinking = None
        uid_int = update.effective_user.id
        if uid_int in self._thinking_mode:
            thinking = ThinkingConfig(enabled=True,
                                      effort=self._thinking_mode[uid_int])

        sent_msg = await update.message.reply_text("Processing...")
        chat_id = str(update.effective_chat.id)
        messages = session.get_messages()
        max_iterations = 10

        try:
            for iteration in range(max_iterations):
                await update.message.chat.send_action(ChatAction.TYPING)

                response = await completion.complete(
                    config=self.config,
                    provider_name=provider_name,
                    model=model,
                    messages=messages,
                    temperature=self.config.get_agent(agent_id).temperature,
                    max_tokens=4096,
                    thinking=thinking,
                    tools=tools,
                )

                if not response.tool_calls:
                    # Final text response — display it
                    final_text = response.text or "(empty response)"
                    if len(final_text) <= 4096:
                        await sent_msg.edit_text(final_text)
                    else:
                        await sent_msg.edit_text(final_text[:4096])
                        remaining = final_text[4096:]
                        while remaining:
                            await update.message.reply_text(remaining[:4096])
                            remaining = remaining[4096:]

                    # Save to session & record usage
                    self.tasks.complete_task(user_id, final_text)
                    self.manager.save_message(session, "assistant", final_text,
                                              model=model, provider=provider_name)
                    self._record_health(provider_name, True)

                    latency = (time.monotonic() - start_time) * 1000
                    db = get_db()
                    input_tokens = sum(len(m.content) for m in messages) // 4
                    output_tokens = len(final_text) // 4
                    db.record_usage(provider_name, model, agent_id, user_id,
                                    input_tokens, output_tokens, latency)
                    return

                # --- Tool calls detected — execute them ---

                # Build status message
                tool_names = [tc.name for tc in response.tool_calls]
                status = f"Executing: {', '.join(tool_names)}..."
                try:
                    await sent_msg.edit_text(status)
                except Exception:
                    pass

                # Add assistant message with tool_calls to context
                raw_tc = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name, "arguments": tc.arguments}}
                    for tc in response.tool_calls
                ]
                messages.append(Message(
                    role="assistant", content=response.text or "",
                    tool_calls=raw_tc,
                ))

                # Execute each tool
                for tc in response.tool_calls:
                    log.info(f"Tool exec: {tc.name} (agent={agent_id})")
                    try:
                        args = json.loads(tc.arguments)
                    except json.JSONDecodeError:
                        args = {}

                    if tc.name == "delegate_task":
                        # Delegation to another agent
                        target_id = args.get("agent_id", "")
                        task_desc = args.get("task", "")
                        target_agent = self.config.get_agent(target_id)
                        target_name = target_agent.name if target_agent else target_id

                        try:
                            await sent_msg.edit_text(
                                f"Delegating to {target_name}..."
                            )
                        except Exception:
                            pass

                        result_text = await self.manager.delegate(
                            agent_id, target_id, user_id, task_desc,
                            chat_id=chat_id,
                        )
                        messages.append(Message(
                            role="tool", content=result_text,
                            tool_call_id=tc.id, name=tc.name,
                        ))
                    else:
                        # Regular tool execution
                        result = await execute_tool(tc.name, args, chat_id)
                        messages.append(Message(
                            role="tool", content=result.output,
                            tool_call_id=tc.id, name=tc.name,
                        ))

            # Exceeded max iterations
            await sent_msg.edit_text(
                "[Error: terlalu banyak tool iterations, silakan coba lagi]"
            )
            self.tasks.fail_task(user_id, "max tool iterations")

        except Exception as e:
            log.error(f"Tool loop error: {e}")
            self.tasks.fail_task(user_id, str(e))
            self._record_health(provider_name, False, str(e))

            latency = (time.monotonic() - start_time) * 1000
            db = get_db()
            db.record_usage(provider_name, model, agent_id, user_id,
                            0, 0, latency, success=False, error=str(e)[:200])

            fallback_ok = await self._try_failover(agent_id, session, update)
            if not fallback_ok:
                try:
                    await sent_msg.edit_text(
                        f"Error: {type(e).__name__}: {str(e)[:200]}"
                    )
                except Exception:
                    await update.message.reply_text(
                        f"Error: {type(e).__name__}: {str(e)[:200]}"
                    )
            self._task_messages.pop(user_id, None)

    async def _run_task(self, update: Update, user_id: str, resume_from=None):
        """Run a task with streaming, cancellation support."""
        agent_id = self._get_agent_id(update.effective_user.id)
        session = self.manager.get_session(agent_id, user_id)
        provider_name, model = self.manager.get_agent_model(session)

        prompt = update.message.text
        if not resume_from:
            self.manager.save_message(session, "user", prompt,
                                      model=model, provider=provider_name)

        # Check if agent has tools — use tool loop instead of streaming
        from core.tools import get_agent_tools
        tools = get_agent_tools(agent_id)
        if tools and not resume_from:
            await self._run_tool_loop(update, user_id, session,
                                       agent_id, provider_name, model, tools)
            return

        # Create task
        task = self.tasks.create_task(user_id, agent_id, prompt,
                                      provider_name, model)

        await update.message.chat.send_action(ChatAction.TYPING)
        start_time = time.monotonic()

        # If resuming, prepend partial response context
        task_messages = session.get_messages()
        if resume_from and resume_from.partial_response:
            task_messages = task_messages + [
                Message(role="assistant", content=resume_from.partial_response),
                Message(role="user", content="Continue from where you left off."),
            ]

        # Build thinking config if enabled
        thinking = None
        uid_int = update.effective_user.id
        if uid_int in self._thinking_mode:
            effort = self._thinking_mode[uid_int]
            thinking = ThinkingConfig(enabled=True, effort=effort)

        try:
            full_text = resume_from.partial_response if resume_from else ""
            thinking_text = ""
            status_prefix = "Thinking..." if thinking else "..."
            sent_msg = await update.message.reply_text(status_prefix)
            self._task_messages[user_id] = sent_msg
            last_edit = time.monotonic()
            last_edit_len = len(full_text)

            async for chunk in completion.stream(
                config=self.config,
                provider_name=provider_name,
                model=model,
                messages=task_messages,
                temperature=self.config.get_agent(agent_id).temperature,
                max_tokens=4096,
                thinking=thinking,
            ):
                # Check cancellation
                if task.should_cancel:
                    task.partial_response = full_text
                    log.info(f"Task cancelled for {user_id}, saved {len(full_text)} chars")
                    break

                # Collect thinking text separately
                if chunk.thinking_text:
                    thinking_text += chunk.thinking_text

                full_text += chunk.text
                task.partial_response = full_text
                now = time.monotonic()

                chars_since_edit = len(full_text) - last_edit_len
                time_since_edit = now - last_edit
                if chars_since_edit >= EDIT_CHAR_THRESHOLD and time_since_edit >= EDIT_INTERVAL:
                    try:
                        await sent_msg.edit_text(full_text[:4096])
                        last_edit = now
                        last_edit_len = len(full_text)
                    except Exception:
                        pass

            # Final update
            if full_text and not task.should_cancel:
                try:
                    if len(full_text) <= 4096:
                        await sent_msg.edit_text(full_text)
                    else:
                        await sent_msg.edit_text(full_text[:4096])
                        remaining = full_text[4096:]
                        while remaining:
                            await update.message.reply_text(remaining[:4096])
                            remaining = remaining[4096:]
                except Exception as e:
                    log.warning(f"Final edit failed: {e}")

            if task.should_cancel:
                # Task was paused/cancelled — show what we have
                if full_text:
                    try:
                        display = full_text[:4000] + "\n\n[...paused]"
                        await sent_msg.edit_text(display)
                    except Exception:
                        pass
            else:
                # Task completed normally
                self.tasks.complete_task(user_id, full_text)

            latency = (time.monotonic() - start_time) * 1000
            self._record_health(provider_name, True)

            if not task.should_cancel:
                self.manager.save_message(session, "assistant", full_text,
                                          model=model, provider=provider_name)

            db = get_db()
            input_tokens = sum(len(m.content) for m in session.get_messages()) // 4
            output_tokens = len(full_text) // 4
            db.record_usage(provider_name, model, agent_id, user_id,
                            input_tokens, output_tokens, latency)

            self._task_messages.pop(user_id, None)

        except Exception as e:
            log.error(f"Task error: {e}")
            self.tasks.fail_task(user_id, str(e))
            self._record_health(provider_name, False, str(e))

            latency = (time.monotonic() - start_time) * 1000
            db = get_db()
            db.record_usage(provider_name, model, agent_id, user_id,
                            0, 0, latency, success=False, error=str(e)[:200])

            fallback_ok = await self._try_failover(agent_id, session, update)
            if not fallback_ok:
                await update.message.reply_text(
                    f"Error: {type(e).__name__}: {str(e)[:200]}"
                )
            self._task_messages.pop(user_id, None)

    # --- Health & Failover ---

    def _record_health(self, provider_name: str, success: bool, error: str = ""):
        try:
            from main import health_monitor
            if health_monitor:
                health_monitor.record_request(provider_name, success, error)
        except ImportError:
            pass

    async def _try_failover(self, agent_id: str, session, update: Update) -> bool:
        try:
            from main import health_monitor
            if not health_monitor or not self.config.health.auto_failover:
                return False

            fallback_agent_id = health_monitor.get_failover_agent(agent_id)
            if not fallback_agent_id:
                return False

            fallback_agent = self.config.get_agent(fallback_agent_id)
            if not fallback_agent:
                return False

            log.info(f"Failover: {agent_id} -> {fallback_agent_id}")
            await update.message.reply_text(
                f"[Failover: switching to {fallback_agent.name}]"
            )

            response = await completion.complete(
                config=self.config,
                provider_name=fallback_agent.provider,
                model=fallback_agent.model,
                messages=session.get_messages(),
                temperature=fallback_agent.temperature,
                max_tokens=4096,
            )

            self.manager.save_message(session, "assistant", response.text,
                                      model=fallback_agent.model,
                                      provider=fallback_agent.provider)
            text = response.text
            while text:
                await update.message.reply_text(text[:4096])
                text = text[4096:]
            return True

        except Exception as e:
            log.error(f"Failover also failed: {e}")
            return False

    # --- OpenRouter Dynamic Models ---

    async def _refresh_openrouter_models(self):
        import httpx
        prov = self.config.get_provider("openrouter")
        if not prov or not prov.api_key:
            return
        if prov.models:
            return

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{prov.base_url}/models",
                    headers={"Authorization": f"Bearer {prov.api_key}"},
                )
                resp.raise_for_status()
                data = resp.json()

            models = []
            for m in data.get("data", []):
                model_id = m.get("id", "")
                if model_id:
                    models.append(model_id)

            prov.models = sorted(models)
            log.info(f"OpenRouter: loaded {len(models)} models")
        except Exception as e:
            log.warning(f"Failed to fetch OpenRouter models: {e}")

    # --- Lifecycle ---

    async def setup(self):
        self.app = Application.builder().token(self.config.telegram.token).build()

        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("models", self._cmd_models))
        self.app.add_handler(CommandHandler("switch", self._cmd_switch))
        self.app.add_handler(CommandHandler("agent", self._cmd_agent))
        self.app.add_handler(CommandHandler("ask", self._cmd_ask))
        self.app.add_handler(CommandHandler("btw", self._cmd_btw))
        self.app.add_handler(CommandHandler("think", self._cmd_think))
        self.app.add_handler(CommandHandler("skill", self._cmd_skill))
        self.app.add_handler(CommandHandler("clear", self._cmd_clear))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("usage", self._cmd_usage))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        await self.app.bot.set_my_commands([
            BotCommand("start", "Mulai bot"),
            BotCommand("models", "Manage API keys"),
            BotCommand("switch", "Ganti model"),
            BotCommand("agent", "Pilih agent"),
            BotCommand("ask", "Tanya agent lain"),
            BotCommand("btw", "Quick side question"),
            BotCommand("think", "Toggle thinking mode"),
            BotCommand("skill", "Run a skill (web search, weather, etc)"),
            BotCommand("clear", "Reset percakapan"),
            BotCommand("status", "Status agent & model"),
            BotCommand("usage", "Statistik penggunaan"),
        ])

        log.info("Telegram bot configured")

    async def start(self):
        await self.setup()
        log.info("Telegram bot starting...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

    async def stop(self):
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            log.info("Telegram bot stopped")
