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
from core.rate_limit import RateLimiter
from core.intent import classify_intent
from core.pairing import PairingManager
from core.hooks import hooks
from core.approval import approval_manager
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
        self.rate_limiter = RateLimiter(max_requests=20, window_seconds=60)
        self.pairing = PairingManager()
        self.app: Optional[Application] = None
        self._user_agent: dict[int, str] = {}
        self._waiting_key: dict[int, str] = {}
        self._disabled_keys: dict[str, str] = {}
        self._task_messages: dict[str, object] = {}  # user_id -> telegram message being edited
        self._thinking_mode: dict[int, str] = {}  # user_id -> effort level
        self._voice_reply: dict[int, bool] = {}  # user_id -> voice reply enabled
        self._restore_settings()

    def _restore_settings(self):
        """Restore user settings from DB."""
        db = get_db()
        for s in db.get_all_user_settings():
            try:
                uid = int(s["user_id"])
            except (ValueError, TypeError):
                continue
            if s["agent_id"]:
                self._user_agent[uid] = s["agent_id"]
            if s["thinking_mode"]:
                self._thinking_mode[uid] = s["thinking_mode"]
            if s["voice_reply"]:
                self._voice_reply[uid] = True
        count = len(db.get_all_user_settings())
        if count:
            log.info(f"Restored settings for {count} users")

    def _save_settings(self, user_id: int):
        """Persist current settings for a user to DB."""
        db = get_db()
        db.save_user_settings(
            user_id=str(user_id),
            agent_id=self._user_agent.get(user_id, ""),
            thinking_mode=self._thinking_mode.get(user_id, ""),
            voice_reply=self._voice_reply.get(user_id, False),
        )

    def _get_agent_id(self, user_id: int) -> str:
        return self._user_agent.get(user_id, self.config.telegram.default_agent)

    def _is_allowed(self, user_id: int) -> bool:
        if not self.config.telegram.allowed_users:
            return True
        if user_id in self.config.telegram.allowed_users:
            return True
        # Check DM pairing
        return self.pairing.is_approved(user_id)

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
        """Build provider selection grid (2 columns) with health status."""
        models = self.manager.list_available_models()
        by_provider: dict[str, int] = {}
        for m in models:
            by_provider[m["provider"]] = by_provider.get(m["provider"], 0) + 1

        buttons = []
        row = []
        for provider, count in sorted(by_provider.items()):
            emoji, _ = self._get_provider_status(provider)
            row.append(InlineKeyboardButton(
                f"{emoji} {provider} ({count})",
                callback_data=f"prov:{provider}:0",
            ))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        return InlineKeyboardMarkup(buttons)

    def _build_model_keyboard(self, provider: str, page: int = 0,
                              user_id: int = 0) -> InlineKeyboardMarkup:
        """Build model selection grid with pagination and active marker."""
        prov = self.config.get_provider(provider)
        if not prov:
            return InlineKeyboardMarkup([])

        # Get current active model for this user
        active_model = ""
        active_provider = ""
        if user_id:
            agent_id = self._get_agent_id(user_id)
            session = self.manager.get_session(agent_id, str(user_id))
            active_provider, active_model = self.manager.get_agent_model(session)

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
            display = model_id
            if len(display) > 38:
                display = display[:35] + "..."
            # Mark active model
            if model_id == active_model and provider == active_provider:
                display = f"\u2705 {display}"
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

    # Agent role descriptions for UI
    _AGENT_ROLES = {
        "agent1": "\U0001f3af Orchestrator",
        "agent2": "\U0001f3a8 Creative",
        "agent3": "\U0001f4ca Analyst",
        "agent4": "\U0001f4bb Coder",
    }

    def _build_agent_keyboard(self, current_agent_id: str) -> InlineKeyboardMarkup:
        """Build agent selection grid — primary agents only, with roles."""
        buttons = []
        for agent in self.config.agents:
            # Skip backup agents (agent5-8)
            if agent.id not in self._AGENT_ROLES:
                continue
            role = self._AGENT_ROLES.get(agent.id, "")
            if agent.id == current_agent_id:
                label = f"\u2705 {agent.name} — {role}"
            else:
                label = f"{agent.name} — {role}"
            buttons.append([InlineKeyboardButton(
                label, callback_data=f"agent:{agent.id}",
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
            keyboard = self._build_model_keyboard(provider, page, user_id=user_id)
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
                self._save_settings(user_id)
                await query.edit_message_text(
                    f"Switched to agent {agent.name} ({agent.id})"
                )

        # --- Settings callbacks ---
        elif data == "set:think":
            # Toggle thinking on/off
            if user_id in self._thinking_mode:
                del self._thinking_mode[user_id]
            else:
                self._thinking_mode[user_id] = "high"
            self._save_settings(user_id)
            keyboard = self._build_settings_keyboard(user_id)
            await query.edit_message_reply_markup(reply_markup=keyboard)

        elif data.startswith("set:think:"):
            parts = data.split(":")
            if len(parts) < 3:
                return
            level = parts[2]
            if level in ("low", "medium", "high", "max"):
                self._thinking_mode[user_id] = level
                self._save_settings(user_id)
            keyboard = self._build_settings_keyboard(user_id)
            await query.edit_message_reply_markup(reply_markup=keyboard)

        elif data == "set:voice":
            # Toggle voice reply
            current = self._voice_reply.get(user_id, False)
            self._voice_reply[user_id] = not current
            self._save_settings(user_id)
            keyboard = self._build_settings_keyboard(user_id)
            await query.edit_message_reply_markup(reply_markup=keyboard)

        elif data == "set:agent":
            current = self._get_agent_id(user_id)
            keyboard = self._build_agent_keyboard(current)
            await query.edit_message_text(
                "Select an agent:", reply_markup=keyboard,
            )

        elif data == "set:model":
            keyboard = self._build_provider_keyboard()
            await query.edit_message_text(
                "Select a provider:", reply_markup=keyboard,
            )

        elif data == "set:memory":
            uid_str = str(user_id)
            db = get_db()
            memories = db.get_memories(uid_str, limit=20)
            if not memories:
                await query.edit_message_text("Belum ada memory tersimpan.")
                return
            lines = ["\U0001f4dd Stored Memories:\n"]
            buttons = []
            for m in memories:
                lines.append(f"[{m['id']}] ({m['category']}) {m['content']}")
            for m in memories[:10]:
                short = m['content'][:30] + "..." if len(m['content']) > 30 else m['content']
                buttons.append([InlineKeyboardButton(
                    f"\u274c {short}", callback_data=f"memdel:{m['id']}",
                )])
            buttons.append([InlineKeyboardButton(
                "\u2b05 Back", callback_data="set:back",
            )])
            text = "\n".join(lines)
            if len(text) > 4096:
                text = text[:4093] + "..."
            await query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(buttons),
            )

        elif data.startswith("memdel:"):
            try:
                mem_id = int(data.split(":")[1])
            except (IndexError, ValueError):
                return
            uid_str = str(user_id)
            db = get_db()
            db.delete_memory(mem_id, uid_str)
            # Refresh memory list
            memories = db.get_memories(uid_str, limit=20)
            if not memories:
                await query.edit_message_text("Semua memory dihapus.")
                return
            lines = ["\U0001f4dd Stored Memories:\n"]
            buttons = []
            for m in memories:
                lines.append(f"[{m['id']}] ({m['category']}) {m['content']}")
            for m in memories[:10]:
                short = m['content'][:30] + "..." if len(m['content']) > 30 else m['content']
                buttons.append([InlineKeyboardButton(
                    f"\u274c {short}", callback_data=f"memdel:{m['id']}",
                )])
            buttons.append([InlineKeyboardButton(
                "\u2b05 Back", callback_data="set:back",
            )])
            text = "\n".join(lines)
            await query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(buttons),
            )

        elif data == "memclear":
            uid_str = str(user_id)
            db = get_db()
            db.conn.execute("DELETE FROM memories WHERE user_id = ?", (uid_str,))
            db.conn.commit()
            await query.edit_message_text("Semua memory dihapus.")

        elif data == "set:back":
            keyboard = self._build_settings_keyboard(user_id)
            await query.edit_message_text(
                "\u2699\ufe0f Settings", reply_markup=keyboard,
            )

        elif data.startswith("approve:"):
            approval_id = data.split(":")[1]
            if approval_manager.approve(approval_id):
                await query.edit_message_text(f"Approved: {approval_id}")
            else:
                await query.edit_message_text("Approval expired or already handled.")

        elif data.startswith("deny:"):
            approval_id = data.split(":")[1]
            if approval_manager.deny(approval_id):
                await query.edit_message_text(f"Denied: {approval_id}")
            else:
                await query.edit_message_text("Approval expired or already handled.")

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
            "/usage - Statistik penggunaan\n"
            "/settings - Pengaturan (thinking, voice, dll)\n"
            "/memory - Lihat/kelola memory tersimpan\n"
            "/search - Cari di riwayat percakapan\n"
            "/insights - Statistik & analytics\n"
            "/moa - Multi-model reasoning\n"
            "/checkpoint - Simpan conversation state\n"
            "/rollback - Rollback ke checkpoint\n"
            "/profile - Lihat profil user\n"
            "/pair - Pairing kode untuk akses bot"
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
        self._save_settings(update.effective_user.id)
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
            self._save_settings(uid)
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
            return
        self._save_settings(uid)

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

    async def _cmd_pair(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """DM pairing — generate or redeem a pairing code.
        Admin: /pair generate
        User:  /pair <CODE>
        """
        uid = update.effective_user.id
        args = context.args

        if not args:
            await update.message.reply_text(
                "Usage:\n"
                "/pair generate — (admin) buat kode pairing\n"
                "/pair <CODE> — redeem kode untuk akses bot"
            )
            return

        action = args[0].lower()

        if action == "generate":
            # Only admins can generate codes
            admin_id = self.config.telegram.admin_chat_id
            if admin_id and str(uid) != str(admin_id):
                # Also allow users in allowed_users list
                if uid not in self.config.telegram.allowed_users:
                    await update.message.reply_text("Hanya admin yang bisa generate kode.")
                    return
            code = self.pairing.generate_code(admin_id=str(uid))
            pending = self.pairing.list_pending()
            await update.message.reply_text(
                f"Pairing code: `{code}`\n"
                f"Expires: 1 jam\n"
                f"Pending codes: {len(pending)}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # Try to redeem code
        code = args[0].strip().upper()
        if self._is_allowed(uid):
            await update.message.reply_text("Kamu sudah punya akses.")
            return

        success = self.pairing.try_pair(uid, code)
        if success:
            await update.message.reply_text(
                "Pairing berhasil! Kamu sekarang punya akses ke bot."
            )
        else:
            await update.message.reply_text(
                "Kode invalid atau expired. Coba lagi."
            )

    async def _cmd_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Search conversation history using FTS5.
        /search <query>
        """
        if not self._is_allowed(update.effective_user.id):
            return
        args = context.args
        if not args:
            await update.message.reply_text("Usage: /search <kata kunci>")
            return

        query = " ".join(args)
        user_id = str(update.effective_user.id)
        db = get_db()
        results = db.search_sessions(user_id, query, limit=10)

        if not results:
            await update.message.reply_text(f"Tidak ditemukan hasil untuk \"{query}\".")
            return

        lines = [f"Search: \"{query}\" — {len(results)} hasil\n"]
        for r in results:
            snippet = r.get("snippet", "")
            role = r.get("role", "?")
            agent = r.get("agent_id", "")
            lines.append(f"[{role}] ({agent}) {snippet}")

        text = "\n".join(lines)
        if len(text) > 4096:
            text = text[:4093] + "..."
        await update.message.reply_text(text)

    async def _cmd_insights(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show usage insights and analytics.
        /insights [hours]
        """
        if not self._is_allowed(update.effective_user.id):
            return

        # Only admin can see insights
        uid = update.effective_user.id
        admin_id = self.config.telegram.admin_chat_id
        if admin_id and str(uid) != str(admin_id):
            if uid not in self.config.telegram.allowed_users:
                await update.message.reply_text("Hanya admin yang bisa lihat insights.")
                return

        args = context.args
        hours = 168  # default 7 days
        if args:
            try:
                hours = int(args[0])
            except ValueError:
                pass

        from core.insights import generate_insights
        report = generate_insights(hours)
        if len(report) > 4096:
            report = report[:4093] + "..."
        await update.message.reply_text(report)

    async def _cmd_moa(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mixture of Agents — multi-model reasoning.
        /moa <question>
        """
        if not self._is_allowed(update.effective_user.id):
            return
        args = context.args
        if not args:
            await update.message.reply_text(
                "Usage: /moa <pertanyaan>\n"
                "Spawn multiple AI models, lalu synthesize jawaban terbaik."
            )
            return

        question = " ".join(args)
        user_id = str(update.effective_user.id)
        agent_id = self._get_agent_id(update.effective_user.id)
        provider_name, model = self.manager.get_agent_model(
            self.manager.get_session(agent_id, user_id))

        await update.message.chat.send_action(ChatAction.TYPING)
        msg = await update.message.reply_text("MoA: spawning reference models...")

        from core.moa import mixture_of_agents, get_available_reference_models
        refs = get_available_reference_models(self.config, provider_name, model)
        if not refs:
            await msg.edit_text("Tidak ada reference model tersedia untuk MoA.")
            return

        try:
            ref_names = [f"{p}/{m}" for p, m in refs]
            await msg.edit_text(f"MoA: {len(refs)} models ({', '.join(ref_names)})...")

            result = await mixture_of_agents(
                self.config, question, refs,
                master_provider=provider_name, master_model=model,
            )

            header = f"[MoA — {len(refs)} models]\n\n"
            text = header + result
            if len(text) <= 4096:
                await msg.edit_text(text)
            else:
                await msg.edit_text(text[:4096])
                remaining = text[4096:]
                while remaining:
                    await update.message.reply_text(remaining[:4096])
                    remaining = remaining[4096:]

            db = get_db()
            db.record_usage(provider_name, model, agent_id, user_id,
                            len(question) // 4, len(result) // 4, 0)
        except Exception as e:
            log.error(f"MoA error: {e}")
            await msg.edit_text(f"MoA error: {str(e)[:200]}")

    async def _cmd_checkpoint(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Save or list conversation checkpoints.
        /checkpoint       — list checkpoints
        /checkpoint save  — save current state
        """
        if not self._is_allowed(update.effective_user.id):
            return
        user_id = str(update.effective_user.id)
        agent_id = self._get_agent_id(update.effective_user.id)
        session = self.manager.get_session(agent_id, user_id)
        session_key = f"{agent_id}:{user_id}"

        args = context.args
        if args and args[0].lower() == "save":
            label = " ".join(args[1:]) if len(args) > 1 else ""
            messages = [{"role": m.role, "content": m.content}
                        for m in session.get_messages(include_system=False)]
            if not messages:
                await update.message.reply_text("Tidak ada pesan untuk di-checkpoint.")
                return
            from core.checkpoint import save_checkpoint
            cp_id = save_checkpoint(session_key, user_id, messages, label)
            await update.message.reply_text(
                f"Checkpoint #{cp_id} tersimpan ({len(messages)} pesan)"
            )
            return

        # List checkpoints
        from core.checkpoint import list_checkpoints
        cps = list_checkpoints(session_key, user_id)
        if not cps:
            await update.message.reply_text(
                "Belum ada checkpoint.\nGunakan /checkpoint save untuk simpan."
            )
            return
        lines = ["Checkpoints:\n"]
        for cp in cps:
            from datetime import datetime
            dt = datetime.utcfromtimestamp(cp["created_at"]).strftime("%m/%d %H:%M")
            lines.append(f"  #{cp['id']} — {cp.get('label', '')} ({dt})")
        lines.append("\nGunakan /rollback <id> untuk restore.")
        await update.message.reply_text("\n".join(lines))

    async def _cmd_rollback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Rollback conversation to a checkpoint.
        /rollback <checkpoint_id>
        """
        if not self._is_allowed(update.effective_user.id):
            return
        args = context.args
        if not args:
            await update.message.reply_text("Usage: /rollback <checkpoint_id>")
            return

        try:
            cp_id = int(args[0])
        except ValueError:
            await update.message.reply_text("ID harus berupa angka.")
            return

        user_id = str(update.effective_user.id)
        from core.checkpoint import load_checkpoint
        messages = load_checkpoint(cp_id, user_id)
        if messages is None:
            await update.message.reply_text(f"Checkpoint #{cp_id} tidak ditemukan.")
            return

        # Restore session
        agent_id = self._get_agent_id(update.effective_user.id)
        session = self.manager.get_session(agent_id, user_id)
        session.messages.clear()
        for m in messages:
            from providers.base import Message
            session.messages.append(Message(role=m["role"], content=m["content"]))

        await update.message.reply_text(
            f"Rollback ke checkpoint #{cp_id} ({len(messages)} pesan)"
        )

    async def _cmd_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user profile built from conversation history.
        /profile [user_id]
        """
        if not self._is_allowed(update.effective_user.id):
            return

        args = context.args
        user_id = args[0] if args else str(update.effective_user.id)

        from core.user_profile import get_profile_summary
        summary = get_profile_summary(user_id)
        await update.message.reply_text(summary)

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle photo messages — analyze with vision model."""
        if not update.message or not update.message.photo:
            return
        if not self._is_allowed(update.effective_user.id):
            return

        user_id = str(update.effective_user.id)

        # Rate limit
        allowed, _ = self.rate_limiter.check(user_id)
        if not allowed:
            wait = self.rate_limiter.get_wait_time(user_id)
            await update.message.reply_text(f"Rate limit — tunggu {int(wait)} detik.")
            return

        await update.message.chat.send_action(ChatAction.TYPING)

        # Get highest resolution photo
        photo = update.message.photo[-1]
        caption = update.message.caption or "Describe this image in detail. Match the user's language."

        try:
            tg_file = await photo.get_file()
            image_data = await tg_file.download_as_bytearray()
        except Exception as e:
            log.error(f"Photo download error: {e}")
            await update.message.reply_text("Gagal download foto.")
            return

        from core.vision import analyze_image
        msg = await update.message.reply_text("Analyzing image...")

        result = await analyze_image(self.config, bytes(image_data), caption)

        if result:
            text = f"[Vision Analysis]\n\n{result}"
            if len(text) <= 4096:
                await msg.edit_text(text)
            else:
                await msg.edit_text(text[:4096])
        else:
            await msg.edit_text("Tidak ada vision model yang tersedia. Pastikan Google/OpenAI API key terpasang.")

        # Record usage
        agent_id = self._get_agent_id(update.effective_user.id)
        db = get_db()
        db.record_usage("vision", "auto", agent_id, user_id, 0, len(result or "") // 4, 0)

    async def _cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return
        user_id = str(update.effective_user.id)
        agent_id = self._get_agent_id(update.effective_user.id)
        self.manager.clear_session(agent_id, user_id)
        await hooks.emit("session:reset", user_id=user_id, agent_id=agent_id)
        await update.message.reply_text("Conversation cleared.")

    async def _cmd_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Export conversation history as a text file."""
        if not self._is_allowed(update.effective_user.id):
            return
        import tempfile
        import os

        user_id = str(update.effective_user.id)
        agent_id = self._get_agent_id(update.effective_user.id)
        session = self.manager.get_session(agent_id, user_id)
        messages = session.get_messages(include_system=False)

        if not messages:
            await update.message.reply_text("Tidak ada pesan untuk di-export.")
            return

        # Build text
        lines = [f"Pawang Conversation Export — {agent_id}", "=" * 40, ""]
        for m in messages:
            role_label = "You" if m.role == "user" else "AI"
            content = m.content[:2000] if len(m.content) > 2000 else m.content
            lines.append(f"[{role_label}]")
            lines.append(content)
            lines.append("")

        text = "\n".join(lines)

        # Send as file
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="pawang_export_",
            delete=False, encoding="utf-8",
        )
        tmp.write(text)
        tmp_path = tmp.name
        tmp.close()

        try:
            await update.message.reply_document(
                document=open(tmp_path, "rb"),
                filename=f"pawang_{agent_id}_{user_id}.txt",
                caption=f"Export: {len(messages)} messages",
            )
        finally:
            os.unlink(tmp_path)

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

    # --- Settings ---

    def _build_settings_keyboard(self, uid: int) -> InlineKeyboardMarkup:
        """Build settings inline keyboard."""
        # Thinking mode
        think_on = uid in self._thinking_mode
        think_level = self._thinking_mode.get(uid, "off")
        think_label = f"\U0001f9e0 Thinking: {think_level}" if think_on else "\U0001f9e0 Thinking: off"

        # Voice reply
        voice_on = self._voice_reply.get(uid, False)
        voice_label = "\U0001f50a Voice Reply: on" if voice_on else "\U0001f508 Voice Reply: off"

        # Current agent & model
        agent_id = self._get_agent_id(uid)
        agent = self.config.get_agent(agent_id)
        session = self.manager.get_session(agent_id, str(uid))
        _, model = self.manager.get_agent_model(session)

        buttons = [
            [InlineKeyboardButton(think_label, callback_data="set:think")],
            [
                InlineKeyboardButton("\U0001f9e0 low", callback_data="set:think:low"),
                InlineKeyboardButton("med", callback_data="set:think:medium"),
                InlineKeyboardButton("high", callback_data="set:think:high"),
                InlineKeyboardButton("max", callback_data="set:think:max"),
            ],
            [InlineKeyboardButton(voice_label, callback_data="set:voice")],
            [InlineKeyboardButton(
                f"\U0001f916 Agent: {agent.name}" if agent else "\U0001f916 Agent",
                callback_data="set:agent",
            )],
            [InlineKeyboardButton(
                f"\u2699\ufe0f Model: {model[:30]}",
                callback_data="set:model",
            )],
            [InlineKeyboardButton(
                "\U0001f4dd Memories", callback_data="set:memory",
            )],
        ]
        return InlineKeyboardMarkup(buttons)

    async def _cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """User settings — thinking, voice reply, agent, model, memories."""
        if not self._is_allowed(update.effective_user.id):
            return
        uid = update.effective_user.id
        keyboard = self._build_settings_keyboard(uid)
        await update.message.reply_text(
            "\u2699\ufe0f Settings", reply_markup=keyboard,
        )

    # --- Rename Agent ---

    async def _cmd_rename(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Rename the current agent: /rename <nama_baru>"""
        if not self._is_allowed(update.effective_user.id):
            return
        args = context.args
        if not args:
            await update.message.reply_text(
                "Usage: /rename <nama_baru>\n"
                "Contoh: /rename Wulan"
            )
            return

        new_name = " ".join(args).strip()
        if len(new_name) > 50:
            await update.message.reply_text("Nama terlalu panjang (maks 50 karakter).")
            return

        user_id = str(update.effective_user.id)
        agent_id = self._get_agent_id(update.effective_user.id)
        agent = self.config.get_agent(agent_id)
        if not agent:
            await update.message.reply_text("Agent tidak ditemukan.")
            return

        old_name = agent.name
        agent.name = new_name

        # Refresh system prompt in active session so agent knows its new name
        if self.manager:
            self.manager.refresh_system_prompt(agent_id, user_id)

        # Persist to config.yaml
        from core.config import save_config
        try:
            save_config(self.config)
        except Exception as e:
            log.error(f"Failed to persist rename to config.yaml: {e}")

        log.info(f"Agent {agent_id} renamed: {old_name} -> {new_name} (by user {user_id})")
        await update.message.reply_text(
            f"Agent {agent_id} renamed: {old_name} -> **{new_name}**",
            parse_mode="Markdown",
        )

    # --- Memory Management ---

    async def _cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """View and manage stored memories."""
        if not self._is_allowed(update.effective_user.id):
            return
        user_id = str(update.effective_user.id)
        db = get_db()
        memories = db.get_memories(user_id, limit=20)

        if not memories:
            await update.message.reply_text(
                "Belum ada memory tersimpan.\n"
                "Agent akan otomatis menyimpan fakta penting dari percakapan."
            )
            return

        lines = ["\U0001f4dd Stored Memories:\n"]
        for m in memories:
            lines.append(f"[{m['id']}] ({m['category']}) {m['content']}")

        buttons = []
        # Show delete buttons for each memory (max 10)
        for m in memories[:10]:
            short = m['content'][:30] + "..." if len(m['content']) > 30 else m['content']
            buttons.append([InlineKeyboardButton(
                f"\u274c {short}",
                callback_data=f"memdel:{m['id']}",
            )])
        buttons.append([InlineKeyboardButton(
            "\U0001f5d1 Clear All", callback_data="memclear",
        )])

        keyboard = InlineKeyboardMarkup(buttons)
        text = "\n".join(lines)
        if len(text) > 4096:
            text = text[:4093] + "..."
        await update.message.reply_text(text, reply_markup=keyboard)

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
                masked = new_key[:8] + "..." + new_key[-4:] if len(new_key) > 12 else "***"
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

    async def _handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle voice/audio messages: transcribe then process as text."""
        if not update.message:
            return
        if not self._is_allowed(update.effective_user.id):
            return

        import tempfile
        from core.transcribe import transcribe

        voice = update.message.voice or update.message.audio
        if not voice:
            return

        user_id = str(update.effective_user.id)
        await update.message.chat.send_action(ChatAction.TYPING)

        # Download voice file
        try:
            tg_file = await voice.get_file()
            suffix = ".ogg" if update.message.voice else ".mp3"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = tmp.name
            await tg_file.download_to_drive(tmp_path)
        except Exception as e:
            log.error(f"Voice download error: {e}")
            await update.message.reply_text("Gagal download voice message.")
            return

        # Transcribe
        try:
            text = await transcribe(tmp_path)
        except Exception as e:
            log.error(f"Transcribe error: {e}")
            await update.message.reply_text("Gagal transcribe voice message.")
            return
        finally:
            import os
            os.unlink(tmp_path)

        if not text:
            await update.message.reply_text("Voice kosong / tidak terdeteksi.")
            return

        # Show transcription
        await update.message.reply_text(f"🎤 \"{text}\"")

        # Process as text message — inject text into update and reuse _handle_message
        update.message.text = text
        await self._handle_message(update, context)

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

    async def _send_voice_reply(self, chat_id: int, text: str):
        """TTS the text and send as voice message. Best-effort, errors logged."""
        import os
        from core.tts import text_to_speech

        if not text or len(text) > 3000:
            return  # Too long for TTS

        try:
            audio_path = await text_to_speech(text[:2000])
            with open(audio_path, "rb") as f:
                await self.app.bot.send_voice(chat_id=chat_id, voice=f)
            os.unlink(audio_path)
        except Exception as e:
            log.warning(f"Voice reply error: {e}")

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
        agent_cfg = self.config.get_agent(agent_id)
        max_iterations = agent_cfg.max_iterations if agent_cfg else 15

        # Compress context if approaching limit
        from core.context_compressor import compress_context
        max_ctx = agent_cfg.max_context_tokens if agent_cfg else 100000
        messages = await compress_context(
            self.config, provider_name, model, messages,
            max_tokens=max_ctx,
        )

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
                    await hooks.emit("message:sent", user_id=user_id, agent_id=agent_id,
                                     text=final_text[:200])
                    self._record_health(provider_name, True)

                    # Voice reply if enabled
                    uid_int = update.effective_user.id
                    if self._voice_reply.get(uid_int, False):
                        await self._send_voice_reply(update.effective_chat.id, final_text)

                    latency = (time.monotonic() - start_time) * 1000
                    db = get_db()
                    input_tokens = sum(len(m.content) for m in messages) // 4
                    output_tokens = len(final_text) // 4
                    db.record_usage(provider_name, model, agent_id, user_id,
                                    input_tokens, output_tokens, latency)
                    return

                # --- Tool calls detected — execute them ---

                # Build progress status message
                _TOOL_EMOJI = {
                    "delegate_task": "\U0001f4e4", "check_balances": "\U0001f4b0",
                    "generate_image": "\U0001f3a8", "generate_video": "\U0001f3ac",
                    "generate_audio": "\U0001f3b5", "web_search": "\U0001f50d",
                    "run_bash": "\U0001f4bb", "save_memory": "\U0001f4be",
                    "recall_memories": "\U0001f9e0", "weather": "\u2600\ufe0f",
                    "send_file": "\U0001f4ce",
                }
                default_emoji = "\u2699\ufe0f"
                tool_labels = [
                    f"{_TOOL_EMOJI.get(tc.name, default_emoji)} {tc.name}"
                    for tc in response.tool_calls
                ]
                budget_info = f"[{iteration + 1}/{max_iterations}]"
                status = f"{budget_info} {', '.join(tool_labels)}..."
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
                    await hooks.emit("tool:called", tool=tc.name, agent_id=agent_id,
                                     user_id=user_id)
                    try:
                        args = json.loads(tc.arguments)
                    except json.JSONDecodeError:
                        args = {}

                    if tc.name == "delegate_task":
                        # Delegation to another agent (shared budget)
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

                        remaining = max_iterations - iteration - 1
                        result_text, child_used = await self.manager.delegate(
                            agent_id, target_id, user_id, task_desc,
                            chat_id=chat_id,
                            remaining_budget=remaining,
                        )
                        # Deduct child iterations from parent budget
                        iteration += child_used
                        messages.append(Message(
                            role="tool", content=result_text,
                            tool_call_id=tc.id, name=tc.name,
                        ))
                    else:
                        # Regular tool execution
                        result = await execute_tool(tc.name, args, chat_id,
                                                           user_id=user_id, agent_id=agent_id)
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
            await hooks.emit("error", error=str(e), agent_id=agent_id, user_id=user_id)

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
        # Rate limit check
        allowed, remaining = self.rate_limiter.check(user_id)
        if not allowed:
            wait = self.rate_limiter.get_wait_time(user_id)
            await update.message.reply_text(
                f"Rate limit — tunggu {int(wait)} detik."
            )
            return

        agent_id = self._get_agent_id(update.effective_user.id)
        session = self.manager.get_session(agent_id, user_id)
        provider_name, model = self.manager.get_agent_model(session)

        # Smart routing: simple messages → cheap model
        from core.smart_routing import route_message
        prompt = update.message.text
        provider_name, model, was_routed = route_message(
            self.config, prompt, provider_name, model,
        )

        if not resume_from:
            self.manager.save_message(session, "user", prompt,
                                      model=model, provider=provider_name)
            await hooks.emit("message:received", user_id=user_id, agent_id=agent_id,
                             text=prompt, routed=was_routed)

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

        # Compress context if approaching limit, then build task messages
        from core.context_compressor import compress_context
        agent_cfg = self.config.get_agent(agent_id)
        max_ctx = agent_cfg.max_context_tokens if agent_cfg else 100000
        task_messages = await compress_context(
            self.config, provider_name, model, session.get_messages(),
            max_tokens=max_ctx,
        )
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
                await hooks.emit("message:sent", user_id=user_id, agent_id=agent_id,
                                 text=full_text[:200])

                # Voice reply if enabled
                uid_int = update.effective_user.id
                if self._voice_reply.get(uid_int, False):
                    await self._send_voice_reply(update.effective_chat.id, full_text)

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
            await hooks.emit("error", error=str(e), agent_id=agent_id, user_id=user_id)

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
        self.app.add_handler(CommandHandler("settings", self._cmd_settings))
        self.app.add_handler(CommandHandler("memory", self._cmd_memory))
        self.app.add_handler(CommandHandler("export", self._cmd_export))
        self.app.add_handler(CommandHandler("search", self._cmd_search))
        self.app.add_handler(CommandHandler("pair", self._cmd_pair))
        self.app.add_handler(CommandHandler("insights", self._cmd_insights))
        self.app.add_handler(CommandHandler("moa", self._cmd_moa))
        self.app.add_handler(CommandHandler("checkpoint", self._cmd_checkpoint))
        self.app.add_handler(CommandHandler("rollback", self._cmd_rollback))
        self.app.add_handler(CommandHandler("profile", self._cmd_profile))
        self.app.add_handler(CommandHandler("rename", self._cmd_rename))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))
        self.app.add_handler(
            MessageHandler(filters.PHOTO, self._handle_photo)
        )
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )
        self.app.add_handler(
            MessageHandler(filters.VOICE | filters.AUDIO, self._handle_voice)
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
            BotCommand("settings", "Pengaturan bot"),
            BotCommand("memory", "Lihat/kelola memory"),
            BotCommand("export", "Export riwayat percakapan"),
            BotCommand("search", "Cari di riwayat percakapan"),
            BotCommand("pair", "Pairing kode untuk akses bot"),
            BotCommand("insights", "Statistik & analytics"),
            BotCommand("moa", "Multi-model reasoning"),
            BotCommand("checkpoint", "Simpan/lihat checkpoint"),
            BotCommand("rollback", "Rollback ke checkpoint"),
            BotCommand("profile", "Lihat profil user"),
            BotCommand("rename", "Ganti nama agent"),
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
