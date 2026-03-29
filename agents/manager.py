"""Agent Manager — manages agent sessions, conversation history, and delegation."""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.config import PawangConfig, AgentConfig
from core.database import get_db
from core.logger import log
from providers.base import Message


@dataclass
class Session:
    """A conversation session for an agent with a specific user."""
    agent_id: str
    user_id: str
    messages: list[Message] = field(default_factory=list)
    active_model: Optional[str] = None
    active_provider: Optional[str] = None
    last_active: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        return f"{self.agent_id}:{self.user_id}"

    def add_message(self, role: str, content: str):
        self.messages.append(Message(role=role, content=content))

    def get_messages(self, include_system: bool = True) -> list[Message]:
        return self.messages if include_system else [
            m for m in self.messages if m.role != "system"
        ]

    def clear(self):
        system = [m for m in self.messages if m.role == "system"]
        self.messages = system

    @property
    def token_estimate(self) -> int:
        return sum(len(m.content) for m in self.messages) // 4


class AgentManager:
    """Manages agents, sessions, persistence, and inter-agent delegation."""

    MAX_CACHED_SESSIONS = 200

    def __init__(self, config: PawangConfig):
        self.config = config
        self._sessions: dict[str, Session] = {}

    def _session_key(self, agent_id: str, user_id: str) -> str:
        return f"{agent_id}:{user_id}"

    def _build_system_prompt(self, agent: 'AgentConfig', user_id: str,
                             platform: str = "telegram") -> str:
        """Build system prompt with SOUL.md, platform hints, and user memories."""
        prompt_text = ""
        if agent.system_prompt_file:
            prompt_path = Path(__file__).parent.parent / agent.system_prompt_file
            if prompt_path.exists():
                prompt_text = prompt_path.read_text().strip()

        # Load SOUL.md personality (per-agent identity)
        soul_path = Path(__file__).parent.parent / "prompts" / f"SOUL_{agent.id}.md"
        if soul_path.exists():
            soul = soul_path.read_text().strip()
            prompt_text = soul + "\n\n" + prompt_text if prompt_text else soul

        # Inject current agent name (supports runtime rename)
        prompt_text = (
            f"## Current Identity\n"
            f"Nama kamu sekarang: **{agent.name}**. "
            f"Gunakan nama ini saat memperkenalkan diri atau ditanya siapa kamu.\n\n"
            + prompt_text
        )

        # Platform formatting hints
        platform_hints = {
            "telegram": (
                "\n\n## Platform: Telegram\n"
                "- Use markdown sparingly (bold, italic, code blocks).\n"
                "- Keep messages under 4096 chars. Split long responses.\n"
                "- Use emoji naturally but not excessively.\n"
                "- For code, use ``` blocks with language hint."
            ),
            "api": (
                "\n\n## Platform: API\n"
                "- Full markdown supported.\n"
                "- No message length limit."
            ),
        }
        prompt_text += platform_hints.get(platform, "")

        # Inject memories (dual: user facts + agent observations)
        db = get_db()
        memories = db.get_memories(user_id, limit=30)
        if memories:
            user_mems = [m for m in memories if m.get("memory_type", "user") == "user"]
            agent_mems = [m for m in memories if m.get("memory_type") == "agent"]

            if user_mems:
                lines = [f"- ({m['category']}) {m['content']}" for m in user_mems]
                prompt_text += (
                    "\n\n## User Memories\n"
                    "Fakta tentang user ini:\n"
                    + "\n".join(lines)
                )

            if agent_mems:
                lines = [f"- {m['content']}" for m in agent_mems]
                prompt_text += (
                    "\n\n## Agent Notes\n"
                    "Catatan dari interaksi sebelumnya:\n"
                    + "\n".join(lines)
                )

            prompt_text += (
                "\n\nGunakan informasi ini untuk personalisasi jawaban. "
                "Simpan fakta baru yang penting dengan tool save_memory."
            )

        return prompt_text

    def get_session(self, agent_id: str, user_id: str) -> Session:
        """Get or create a session, restoring from DB if available.

        Auto-resets idle sessions based on config.session_timeout.
        """
        key = self._session_key(agent_id, user_id)

        # Auto-reset idle sessions
        if key in self._sessions:
            session = self._sessions[key]
            timeout = getattr(self.config, 'session_timeout', 14400)
            if time.time() - session.last_active > timeout:
                log.info(f"Session auto-reset (idle {timeout}s): {key}")
                del self._sessions[key]

        if key not in self._sessions:
            session = Session(agent_id=agent_id, user_id=user_id)

            # Load system prompt with memories
            agent = self.config.get_agent(agent_id)
            if agent:
                prompt_text = self._build_system_prompt(agent, user_id)
                if prompt_text:
                    session.add_message("system", prompt_text)

            # Restore from DB
            db = get_db()
            db_session = db.get_session(key)
            if db_session:
                if db_session["active_model"]:
                    session.active_model = db_session["active_model"]
                if db_session["active_provider"]:
                    session.active_provider = db_session["active_provider"]

                # Restore last N messages
                history = db.get_history(key, limit=30)
                for msg in history:
                    if msg["role"] != "system":
                        session.add_message(msg["role"], msg["content"])
                if history:
                    log.info(f"Restored {len(history)} messages for {key}")

            self._sessions[key] = session
            self._evict_if_needed()
        return self._sessions[key]

    def _evict_if_needed(self):
        """Evict oldest sessions when cache exceeds max size."""
        if len(self._sessions) <= self.MAX_CACHED_SESSIONS:
            return
        # Remove oldest entries (first inserted, since dict is ordered in Python 3.7+)
        to_remove = len(self._sessions) - self.MAX_CACHED_SESSIONS
        keys = list(self._sessions.keys())[:to_remove]
        for k in keys:
            del self._sessions[k]
        log.info(f"Evicted {to_remove} cached sessions (max={self.MAX_CACHED_SESSIONS})")

    def save_message(self, session: Session, role: str, content: str,
                     model: str = "", provider: str = ""):
        """Add message to session AND persist to DB + daily log."""
        session.add_message(role, content)
        session.last_active = time.time()
        db = get_db()
        db.save_message(session.key, session.agent_id, session.user_id,
                        role, content, model, provider)
        # Daily memory log (audit trail)
        from core.daily_log import append_daily_log
        append_daily_log(session.agent_id, session.user_id, role, content)

    def get_agent_model(self, session: Session) -> tuple[str, str]:
        agent = self.config.get_agent(session.agent_id)
        if not agent:
            raise ValueError(f"Agent '{session.agent_id}' not found")
        provider = session.active_provider or agent.provider
        model = session.active_model or agent.model
        return provider, model

    def switch_model(self, session: Session, provider: str, model: str):
        session.active_provider = provider
        session.active_model = model
        db = get_db()
        db.save_session_model(session.key, provider, model)
        log.info(f"Session {session.key} switched to {provider}/{model}")

    def refresh_system_prompt(self, agent_id: str, user_id: str):
        """Rebuild system prompt (e.g. after rename) and update cached session."""
        key = self._session_key(agent_id, user_id)
        session = self._sessions.get(key)
        if not session:
            return
        agent = self.config.get_agent(agent_id)
        if not agent:
            return
        new_prompt = self._build_system_prompt(agent, user_id)
        if not new_prompt:
            return
        if session.messages and session.messages[0].role == "system":
            session.messages[0] = Message(role="system", content=new_prompt)
        else:
            session.messages.insert(0, Message(role="system", content=new_prompt))
        log.info(f"Refreshed system prompt for {key} (name={agent.name})")

    def refresh_memories(self, agent_id: str, user_id: str):
        """Refresh the system prompt in a cached session with latest memories."""
        key = self._session_key(agent_id, user_id)
        session = self._sessions.get(key)
        if not session:
            return
        agent = self.config.get_agent(agent_id)
        if not agent:
            return
        new_prompt = self._build_system_prompt(agent, user_id)
        if not new_prompt:
            return
        # Replace the system message (always first)
        if session.messages and session.messages[0].role == "system":
            session.messages[0] = Message(role="system", content=new_prompt)
        else:
            session.messages.insert(0, Message(role="system", content=new_prompt))
        log.info(f"Refreshed memories in session {key}")

    def list_sessions(self) -> list[Session]:
        return list(self._sessions.values())

    def clear_session(self, agent_id: str, user_id: str):
        key = self._session_key(agent_id, user_id)
        if key in self._sessions:
            self._sessions[key].clear()
        db = get_db()
        db.clear_history(key)

    def list_available_models(self) -> list[dict]:
        models = []
        for name, prov in self.config.providers.items():
            for model_id in prov.models:
                models.append({
                    "provider": name,
                    "model": model_id,
                    "format": prov.api_format,
                })
        return models

    # --- Agent Delegation ---

    MAX_DELEGATION_DEPTH = 2

    async def delegate(self, from_agent_id: str, to_agent_id: str,
                       user_id: str, task: str,
                       chat_id: str = "",
                       remaining_budget: int = 0,
                       depth: int = 1) -> tuple[str, int]:
        """Delegate a task from one agent to another.

        Runs a tool execution loop so the target agent can use its tools
        (generate_image, run_bash, etc.) before responding.
        Child agents have restricted toolsets (no delegate, no memory writes).
        Max depth=2 to prevent recursive delegation chains.

        Returns (response_text, iterations_used) for shared budget tracking.
        """
        import json
        from core import completion
        from core.tools import get_agent_tools, execute_tool

        if depth > self.MAX_DELEGATION_DEPTH:
            return f"[Error: max delegation depth ({self.MAX_DELEGATION_DEPTH}) exceeded]", 0

        target_agent = self.config.get_agent(to_agent_id)
        if not target_agent:
            return f"[Error: Agent '{to_agent_id}' not found]", 0

        # Build system prompt from agent's prompt file
        system_content = (
            f"You are {target_agent.name}. Another agent has delegated a task to you. "
            f"Respond concisely and directly to the task."
        )
        if target_agent.system_prompt_file:
            prompt_path = Path(__file__).parent.parent / target_agent.system_prompt_file
            if prompt_path.exists():
                system_content = prompt_path.read_text().strip()
                system_content += (
                    "\n\n---\n"
                    "This is a delegated task from another agent. "
                    "Use your tools to complete the task, then respond concisely."
                )

        messages = [
            Message(role="system", content=system_content),
            Message(role="user", content=task),
        ]

        tools = get_agent_tools(to_agent_id, is_delegated=True)
        # Child gets min of its own budget or remaining parent budget
        child_budget = target_agent.max_iterations
        if remaining_budget > 0:
            child_budget = min(child_budget, remaining_budget)

        try:
            for i in range(child_budget):
                response = await completion.complete(
                    config=self.config,
                    provider_name=target_agent.provider,
                    model=target_agent.model,
                    messages=messages,
                    temperature=target_agent.temperature,
                    max_tokens=4096,
                    tools=tools if tools else None,
                )

                if not response.tool_calls:
                    # No tools called — final text response
                    log.info(f"Delegation: {from_agent_id} -> {to_agent_id} OK "
                             f"({i + 1} iterations)")
                    return response.text, i + 1

                # Add assistant message with tool_calls
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
                    log.info(f"Delegation tool: {to_agent_id} -> {tc.name}")
                    try:
                        args = json.loads(tc.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    result = await execute_tool(tc.name, args, chat_id,
                                                       user_id=user_id, agent_id=to_agent_id)
                    messages.append(Message(
                        role="tool", content=result.output,
                        tool_call_id=tc.id, name=tc.name,
                    ))

            return f"[Error: too many tool iterations in delegation (budget={child_budget})]", child_budget

        except Exception as e:
            log.error(f"Delegation failed: {from_agent_id} -> {to_agent_id}: {e}")
            return f"[Delegation error: {e}]", 0
