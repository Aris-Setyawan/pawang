"""Agent Manager — manages agent sessions, conversation history, and delegation."""

import time
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.config import PawangConfig, AgentConfig
from core.database import get_db
from core.logger import log
from providers.base import Message


def _extract_tokens(usage: dict) -> tuple[int, int]:
    """Normalize provider-specific usage dict to (input_tokens, output_tokens).

    OpenAI-compat: prompt_tokens / completion_tokens
    Anthropic:     input_tokens  / output_tokens
    Gemini:        promptTokenCount / candidatesTokenCount
    """
    if not usage:
        return 0, 0
    in_tok = (usage.get("prompt_tokens") or usage.get("input_tokens")
              or usage.get("promptTokenCount") or 0)
    out_tok = (usage.get("completion_tokens") or usage.get("output_tokens")
               or usage.get("candidatesTokenCount") or 0)
    return int(in_tok or 0), int(out_tok or 0)


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

        # Inject learned knowledge (top high-confidence entries)
        try:
            from core.knowledge import get_knowledge_base
            kb = get_knowledge_base()
            stats = kb.get_stats()
            if stats.get("total", 0) > 0:
                top_knowledge = kb.search("", limit=10, min_confidence=0.7)
                if top_knowledge:
                    lines = [f"- Q: {k['question'][:80]} -> A: {k['answer'][:120]}"
                             for k in top_knowledge]
                    prompt_text += (
                        "\n\n## Learned Knowledge\n"
                        "Pengetahuan yang sudah dipelajari dari interaksi sebelumnya "
                        f"({stats['total']} entries, avg confidence {stats.get('avg_conf', 0):.1%}):\n"
                        + "\n".join(lines[:10])
                    )
        except Exception:
            pass

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

    def switch_model(self, session: Session, provider: str, model: str,
                     persist: bool = False):
        """Switch model for a session.

        Args:
            persist: If True, also update agent default config and save to config.yaml.
        """
        session.active_provider = provider
        session.active_model = model
        db = get_db()
        db.save_session_model(session.key, provider, model)
        log.info(f"Session {session.key} switched to {provider}/{model}")

        if persist:
            self._persist_agent_model(session.agent_id, provider, model)

    def _persist_agent_model(self, agent_id: str, provider: str, model: str):
        """Update agent default config and persist to config.yaml."""
        agent = self.config.get_agent(agent_id)
        if not agent:
            return
        agent.provider = provider
        agent.model = model
        try:
            from core.config import save_config
            save_config(self.config)
            log.info(f"Persisted {agent_id} default -> {provider}/{model}")
        except Exception as e:
            log.error(f"Failed to persist config: {e}")

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

    # --- Loop/stuck detection thresholds ---
    LOOP_THRESHOLD = 3        # same tool+args repeated N times = loop
    ERROR_STREAK_LIMIT = 4    # consecutive errors = stuck
    STALE_ITERATIONS = 5      # no new file created/modified in N iterations = stale

    def _detect_loop(self, history: list) -> Optional[str]:
        """Detect if agent is stuck in a loop (same tool+args repeated)."""
        if len(history) < self.LOOP_THRESHOLD:
            return None
        recent = history[-self.LOOP_THRESHOLD:]
        signatures = [f"{h['tool']}:{h['preview']}" for h in recent]
        if len(set(signatures)) == 1:
            return f"Loop detected: {recent[0]['preview']} repeated {self.LOOP_THRESHOLD}x"
        return None

    def _detect_error_streak(self, history: list) -> Optional[str]:
        """Detect consecutive tool failures."""
        if len(history) < self.ERROR_STREAK_LIMIT:
            return None
        recent = history[-self.ERROR_STREAK_LIMIT:]
        if all(not h["ok"] for h in recent):
            return f"Error streak: {self.ERROR_STREAK_LIMIT} consecutive failures"
        return None

    def _get_backup_agent(self, agent_id: str) -> Optional[str]:
        """Get backup agent ID from health failover chain."""
        try:
            from main import health_monitor
            if health_monitor:
                return health_monitor.get_failover_agent(agent_id)
        except ImportError:
            pass
        # Fallback: manual map from config
        for chain in self.config.health.fallback_chain:
            if len(chain) >= 2 and chain[0] == agent_id:
                return chain[1]
        return None

    async def _delegate_claude_code(self, user_id: str, task: str,
                                     on_progress=None) -> str:
        """Delegate task to Claude Code CLI (agent9).

        Auto-matches task to existing project session, or creates new project.
        """
        from core.claude_code import get_cc_manager
        ccm = get_cc_manager()

        # Smart match: find existing project or create new one
        session = ccm.find_or_create_project(task, user_id)

        is_new = not session.session_id
        if on_progress:
            try:
                action = "New project" if is_new else "Resume"
                await on_progress(
                    f"Claude Code: {action} '{session.name}'\n"
                    f"{session.directory}"
                )
            except Exception:
                pass

        result = await ccm.execute(session, task, timeout=300)
        return result

    async def delegate(self, from_agent_id: str, to_agent_id: str,
                       user_id: str, task: str,
                       chat_id: str = "",
                       remaining_budget: int = 0,
                       depth: int = 1,
                       on_progress=None,
                       check_interrupt=None) -> tuple[str, int]:
        """Delegate a task with supervision: loop/stuck detection + backup escalation.

        If the target agent gets stuck (loop, error streak), automatically
        escalates to its backup agent to continue/fix the work.

        Returns (response_text, iterations_used) for shared budget tracking.
        """
        from core import completion
        from core.tools import get_agent_tools, execute_tool

        if depth > self.MAX_DELEGATION_DEPTH:
            return f"[Error: max delegation depth ({self.MAX_DELEGATION_DEPTH}) exceeded]", 0

        target_agent = self.config.get_agent(to_agent_id)
        if not target_agent:
            return f"[Error: Agent '{to_agent_id}' not found]", 0

        # Agent9 (Claude Code) — route via CLI, not API
        if to_agent_id == "agent9":
            return await self._delegate_claude_code(user_id, task, on_progress), 1

        result_text, iters, status = await self._run_delegated_loop(
            from_agent_id, to_agent_id, target_agent, user_id, task,
            chat_id, remaining_budget, on_progress, check_interrupt,
        )

        # If stuck/loop, escalate to backup agent
        if status in ("loop", "error_streak", "budget_exhausted"):
            backup_id = self._get_backup_agent(to_agent_id)
            if backup_id:
                backup_agent = self.config.get_agent(backup_id)
                if backup_agent:
                    log.warning(f"Delegation escalation: {to_agent_id} {status} -> "
                                f"backup {backup_id} ({backup_agent.name})")

                    # Tell backup what happened and continue
                    escalation_task = (
                        f"Agent {target_agent.name} ({to_agent_id}) was working on this task "
                        f"but got stuck ({status}).\n\n"
                        f"Original task:\n{task}\n\n"
                        f"What {target_agent.name} did so far:\n{result_text}\n\n"
                        f"Please continue or fix the work. Focus on completing the task."
                    )

                    if on_progress:
                        try:
                            await on_progress(
                                f"⚠️ {target_agent.name} stuck ({status})\n"
                                f"🔄 Escalating to {backup_agent.name}..."
                            )
                        except Exception:
                            pass

                    backup_budget = remaining_budget - iters if remaining_budget > 0 else 0
                    backup_text, backup_iters, _ = await self._run_delegated_loop(
                        from_agent_id, backup_id, backup_agent, user_id,
                        escalation_task, chat_id, backup_budget,
                        on_progress, check_interrupt,
                    )
                    total_iters = iters + backup_iters
                    combined = (
                        f"[{target_agent.name} stuck ({status}), "
                        f"{backup_agent.name} took over]\n\n{backup_text}"
                    )
                    return combined, total_iters

        return result_text, iters

    async def _run_delegated_loop(
        self, from_agent_id: str, to_agent_id: str,
        target_agent: AgentConfig, user_id: str, task: str,
        chat_id: str, remaining_budget: int,
        on_progress=None, check_interrupt=None,
    ) -> tuple[str, int, str]:
        """Core delegation tool loop with supervision.

        Returns (response_text, iterations_used, status).
        Status: "ok", "interrupted", "loop", "error_streak", "budget_exhausted", "error".
        """
        from core import completion
        from core.tools import get_agent_tools, execute_tool
        from channels.telegram import _get_tool_preview, _get_output_snippet

        # Build system prompt
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
        child_budget = target_agent.max_iterations
        if remaining_budget > 0:
            child_budget = min(child_budget, remaining_budget)

        child_history = []
        start_time = time.time()

        try:
            for i in range(child_budget):
                # Check interrupt
                if check_interrupt and check_interrupt():
                    summary = self._build_child_summary(to_agent_id, child_history, "interrupted")
                    return summary, i + 1, "interrupted"

                iter_start = time.time()
                response = await completion.complete(
                    config=self.config,
                    provider_name=target_agent.provider,
                    model=target_agent.model,
                    messages=messages,
                    temperature=target_agent.temperature,
                    max_tokens=4096,
                    tools=tools if tools else None,
                )

                # Record usage for child agent — this is what makes agent4/agent8/etc
                # show up in usage stats (not just the parent agent1).
                try:
                    in_tok, out_tok = _extract_tokens(response.usage or {})
                    if not in_tok:
                        in_tok = sum(len(m.content or "") for m in messages) // 4
                    if not out_tok:
                        out_tok = len(response.text or "") // 4
                    get_db().record_usage(
                        provider=target_agent.provider,
                        model=target_agent.model,
                        agent_id=to_agent_id,
                        user_id=user_id,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        latency_ms=(time.time() - iter_start) * 1000,
                        success=True,
                    )
                except Exception as rec_err:
                    log.debug(f"record_usage (delegation) failed: {rec_err}")

                if not response.tool_calls:
                    log.info(f"Delegation: {from_agent_id} -> {to_agent_id} OK "
                             f"({i + 1} iterations, {time.time() - start_time:.0f}s)")
                    return response.text, i + 1, "ok"

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

                # Execute each tool with progress
                for tc in response.tool_calls:
                    try:
                        args = json.loads(tc.arguments)
                    except json.JSONDecodeError:
                        args = {}

                    preview = _get_tool_preview(tc.name, args)
                    elapsed = int(time.time() - start_time)
                    log.info(f"Delegation tool: {to_agent_id} -> {preview} ({elapsed}s)")

                    # Progress with timing
                    if on_progress:
                        recent = child_history[-5:]
                        lines = []
                        for h in recent:
                            icon = "✅" if h["ok"] else "❌"
                            line = f"{icon} {h['preview']}"
                            if h.get("output"):
                                line += f"\n   → {h['output']}"
                            lines.append(line)
                        lines.append(f"🔄 {preview}")
                        progress_text = (
                            f"[{i + 1}/{child_budget}] {target_agent.name} "
                            f"({elapsed}s):\n" + "\n".join(lines)
                        )
                        try:
                            await on_progress(progress_text)
                        except Exception:
                            pass

                    result = await execute_tool(tc.name, args, chat_id,
                                                user_id=user_id, agent_id=to_agent_id)
                    messages.append(Message(
                        role="tool", content=result.output,
                        tool_call_id=tc.id, name=tc.name,
                    ))

                    snippet = _get_output_snippet(tc.name, result.output, result.success)
                    child_history.append({
                        "preview": preview,
                        "output": snippet,
                        "ok": result.success,
                        "tool": tc.name,
                    })

                # --- Supervision checks after each iteration ---

                # Loop detection
                loop_msg = self._detect_loop(child_history)
                if loop_msg:
                    log.warning(f"Delegation supervision: {to_agent_id} — {loop_msg}")
                    summary = self._build_child_summary(to_agent_id, child_history, loop_msg)
                    return summary, i + 1, "loop"

                # Error streak detection
                err_msg = self._detect_error_streak(child_history)
                if err_msg:
                    log.warning(f"Delegation supervision: {to_agent_id} — {err_msg}")
                    summary = self._build_child_summary(to_agent_id, child_history, err_msg)
                    return summary, i + 1, "error_streak"

                # Check interrupt after tools
                if check_interrupt and check_interrupt():
                    summary = self._build_child_summary(to_agent_id, child_history, "interrupted")
                    return summary, i + 1, "interrupted"

            summary = self._build_child_summary(to_agent_id, child_history, "budget_exhausted")
            return summary, child_budget, "budget_exhausted"

        except Exception as e:
            log.error(
                f"Delegation failed: {from_agent_id} -> {to_agent_id}: "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )
            return f"[Delegation error: {type(e).__name__}: {e}]", 0, "error"

    @staticmethod
    def _build_child_summary(agent_id: str, history: list, reason: str) -> str:
        """Build a summary of what the child agent did before stopping."""
        lines = [f"[{reason}] Agent {agent_id} — {len(history)} tools executed:"]
        for h in history:
            icon = "✅" if h["ok"] else "❌"
            line = f"  {icon} {h['preview']}"
            if h.get("output"):
                line += f"\n     → {h['output']}"
            lines.append(line)
        return "\n".join(lines)
