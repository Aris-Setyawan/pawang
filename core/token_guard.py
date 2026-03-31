"""Token Guard — spike detection, per-agent budgets, real-time alerts.

Features:
1. Token spike detector: tracks moving average per agent, alerts if usage > Nx normal
2. Per-agent token budget: max tokens/hour, auto-throttle when exceeded
3. Real-time Telegram alerts to admin on spike or budget breach
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.logger import log


@dataclass
class AgentTokenStats:
    """In-memory token tracking for one agent in the current hour window."""
    tokens_in: int = 0
    tokens_out: int = 0
    request_count: int = 0
    window_start: float = 0.0

    @property
    def total(self) -> int:
        return self.tokens_in + self.tokens_out

    def reset(self, now: float):
        self.tokens_in = 0
        self.tokens_out = 0
        self.request_count = 0
        self.window_start = now


@dataclass
class SpikeEvent:
    """Record of a detected spike."""
    agent_id: str
    current_tokens: int
    avg_tokens: int
    ratio: float
    timestamp: float = field(default_factory=time.time)


class TokenGuard:
    """Monitors token usage for spikes and budget enforcement.

    Uses in-memory sliding windows for real-time tracking,
    queries DB for historical moving averages.
    """

    def __init__(self, spike_threshold: float = 2.0, window_hours: int = 24,
                 default_budget: int = 0):
        """
        Args:
            spike_threshold: Alert if current hour > threshold * moving_avg (default 2x)
            window_hours: Hours of history for moving average (default 24)
            default_budget: Default max tokens/hour per agent (0 = no limit)
        """
        self.spike_threshold = spike_threshold
        self.window_hours = window_hours
        self.default_budget = default_budget

        # Per-agent budget overrides (agent_id -> max tokens/hour)
        self._budgets: dict[str, int] = {}

        # In-memory current-hour stats per agent
        self._current: dict[str, AgentTokenStats] = defaultdict(AgentTokenStats)

        # Cached moving averages (refreshed periodically)
        self._moving_avgs: dict[str, float] = {}
        self._avg_last_refresh: float = 0.0

        # Recent spike events (for dedup — don't spam alerts)
        self._recent_spikes: dict[str, float] = {}  # agent_id -> last alert time
        self._recent_budget_alerts: dict[str, float] = {}

        # Alert cooldown (seconds between repeated alerts for same agent)
        self.alert_cooldown = 1800  # 30 minutes

        # Notification function (async)
        self._notify_func: Optional[Callable] = None

        # Stats
        self.total_spikes_detected = 0
        self.total_budget_blocks = 0

    def set_notify(self, func: Callable):
        """Set async notification function (e.g. Telegram admin alert)."""
        self._notify_func = func

    def set_budget(self, agent_id: str, max_tokens_per_hour: int):
        """Set per-agent hourly token budget."""
        self._budgets[agent_id] = max_tokens_per_hour
        log.info(f"TokenGuard: budget for {agent_id} = {max_tokens_per_hour:,} tokens/hour")

    def set_budgets(self, budgets: dict[str, int]):
        """Set multiple budgets at once."""
        for agent_id, budget in budgets.items():
            self._budgets[agent_id] = budget
        if budgets:
            log.info(f"TokenGuard: configured budgets for {len(budgets)} agents")

    def get_budget(self, agent_id: str) -> int:
        """Get effective budget for an agent."""
        return self._budgets.get(agent_id, self.default_budget)

    def _get_or_reset_stats(self, agent_id: str) -> AgentTokenStats:
        """Get current hour stats, resetting if the window has rolled over."""
        now = time.time()
        stats = self._current[agent_id]
        if stats.window_start == 0.0:
            stats.window_start = now
        # Roll over if current window > 1 hour
        elif now - stats.window_start >= 3600:
            stats.reset(now)
        return stats

    async def record(self, agent_id: str, input_tokens: int, output_tokens: int,
                     provider: str = "", model: str = "") -> bool:
        """Record token usage and check for spikes/budget.

        Returns True if allowed, False if throttled (over budget).
        """
        stats = self._get_or_reset_stats(agent_id)
        stats.tokens_in += input_tokens
        stats.tokens_out += output_tokens
        stats.request_count += 1

        total = stats.total

        # --- Spike detection ---
        avg = self._moving_avgs.get(agent_id, 0)
        if avg > 100:  # only check if we have meaningful baseline
            ratio = total / avg
            if ratio >= self.spike_threshold:
                await self._alert_spike(agent_id, total, avg, ratio, provider, model)

        # --- Budget check ---
        budget = self.get_budget(agent_id)
        if budget > 0 and total > budget:
            await self._alert_budget(agent_id, total, budget, provider, model)
            self.total_budget_blocks += 1
            return False  # throttled

        return True

    def check_budget(self, agent_id: str) -> tuple[bool, int]:
        """Pre-check if agent has budget remaining.

        Returns (allowed, remaining_tokens). remaining=-1 means no budget set.
        """
        budget = self.get_budget(agent_id)
        if budget <= 0:
            return True, -1

        stats = self._get_or_reset_stats(agent_id)
        remaining = budget - stats.total
        return remaining > 0, max(0, remaining)

    async def _alert_spike(self, agent_id: str, current: int, avg: float,
                           ratio: float, provider: str, model: str):
        """Send spike alert (with cooldown to prevent spam)."""
        now = time.time()
        last = self._recent_spikes.get(agent_id, 0)
        if now - last < self.alert_cooldown:
            return  # cooldown active

        self._recent_spikes[agent_id] = now
        self.total_spikes_detected += 1

        msg = (
            f"🚨 Token Spike — {agent_id}\n"
            f"Current hour: {current:,} tokens\n"
            f"Moving avg: {avg:,.0f} tokens/hour\n"
            f"Ratio: {ratio:.1f}x normal\n"
            f"Provider: {provider}/{model}\n"
            f"Requests this hour: {self._current[agent_id].request_count}"
        )
        log.warning(f"TokenGuard SPIKE: {agent_id} {current:,} tokens ({ratio:.1f}x avg)")

        if self._notify_func:
            try:
                await self._notify_func(msg)
            except Exception as e:
                log.error(f"TokenGuard alert failed: {e}")

    async def _alert_budget(self, agent_id: str, current: int, budget: int,
                            provider: str, model: str):
        """Send budget exceeded alert (with cooldown)."""
        now = time.time()
        last = self._recent_budget_alerts.get(agent_id, 0)
        if now - last < self.alert_cooldown:
            return

        self._recent_budget_alerts[agent_id] = now
        pct = (current / budget * 100) if budget > 0 else 0

        msg = (
            f"⛔ Token Budget Exceeded — {agent_id}\n"
            f"Used: {current:,} / {budget:,} tokens ({pct:.0f}%)\n"
            f"Provider: {provider}/{model}\n"
            f"Agent will be throttled until next hour window."
        )
        log.warning(f"TokenGuard BUDGET: {agent_id} {current:,}/{budget:,} tokens")

        if self._notify_func:
            try:
                await self._notify_func(msg)
            except Exception as e:
                log.error(f"TokenGuard budget alert failed: {e}")

    def refresh_moving_averages(self):
        """Recompute moving averages from DB. Call periodically (e.g. every 5 min)."""
        try:
            from core.database import get_db
            db = get_db()

            since = time.time() - (self.window_hours * 3600)
            rows = db.conn.execute(
                "SELECT agent_id, "
                "SUM(input_tokens + output_tokens) as total_tokens, "
                "COUNT(DISTINCT CAST((created_at / 3600) AS INTEGER)) as active_hours "
                "FROM usage WHERE created_at > ? AND success = 1 "
                "GROUP BY agent_id",
                (since,),
            ).fetchall()

            for row in rows:
                agent_id = row["agent_id"]
                total = row["total_tokens"] or 0
                hours = row["active_hours"] or 1
                self._moving_avgs[agent_id] = total / hours

            self._avg_last_refresh = time.time()
            if rows:
                log.info(f"TokenGuard: refreshed averages for {len(rows)} agents")

        except Exception as e:
            log.error(f"TokenGuard refresh error: {e}")

    def get_status(self) -> dict:
        """Get current guard status for API/panel.

        Always includes all configured agents, even without traffic.
        """
        agents = {}
        # Include all agents with configured budgets
        all_agent_ids = set(self._budgets.keys()) | set(self._current.keys()) | set(self._moving_avgs.keys())
        for agent_id in sorted(all_agent_ids):
            stats = self._current.get(agent_id, AgentTokenStats())
            budget = self.get_budget(agent_id)
            avg = self._moving_avgs.get(agent_id, 0)
            agents[agent_id] = {
                "current_hour_tokens": stats.total,
                "current_hour_requests": stats.request_count,
                "moving_avg_per_hour": round(avg, 0),
                "budget": budget,
                "budget_remaining": max(0, budget - stats.total) if budget > 0 else -1,
                "budget_pct": round(stats.total / budget * 100, 1) if budget > 0 else 0,
                "window_start": stats.window_start,
            }
        return {
            "spike_threshold": self.spike_threshold,
            "window_hours": self.window_hours,
            "default_budget": self.default_budget,
            "total_spikes_detected": self.total_spikes_detected,
            "total_budget_blocks": self.total_budget_blocks,
            "agents": agents,
        }

    def get_report(self) -> str:
        """Generate human-readable status report."""
        status = self.get_status()
        lines = [
            f"🛡️ Token Guard Status",
            f"Spike threshold: {status['spike_threshold']}x",
            f"Avg window: {status['window_hours']}h",
            f"Spikes detected: {status['total_spikes_detected']}",
            f"Budget blocks: {status['total_budget_blocks']}",
            "",
        ]
        for agent_id, data in status["agents"].items():
            budget_str = f"{data['budget']:,}" if data["budget"] > 0 else "unlimited"
            lines.append(
                f"  {agent_id}: {data['current_hour_tokens']:,} tokens "
                f"(avg {data['moving_avg_per_hour']:,.0f}/h) "
                f"[budget: {budget_str}]"
            )
        return "\n".join(lines)


# --- Singleton ---

_guard: Optional[TokenGuard] = None


def get_token_guard() -> TokenGuard:
    global _guard
    if _guard is None:
        _guard = TokenGuard()
    return _guard


def init_token_guard(config) -> TokenGuard:
    """Initialize TokenGuard from config.yaml settings."""
    global _guard

    tg_cfg = getattr(config, 'token_guard', None)
    if tg_cfg and isinstance(tg_cfg, dict):
        _guard = TokenGuard(
            spike_threshold=tg_cfg.get("spike_threshold", 2.0),
            window_hours=tg_cfg.get("window_hours", 24),
            default_budget=tg_cfg.get("default_budget", 0),
        )
        budgets = tg_cfg.get("budgets", {})
        if budgets:
            _guard.set_budgets(budgets)
    else:
        # Sensible defaults
        _guard = TokenGuard(
            spike_threshold=2.0,
            window_hours=24,
            default_budget=500000,  # 500k tokens/hour default
        )
        # Per-agent budgets (primary agents get more)
        _guard.set_budgets({
            "agent1": 800000,   # orchestrator — highest traffic
            "agent2": 500000,   # creative
            "agent3": 500000,   # analyst
            "agent4": 500000,   # coder
            "agent5": 400000,   # backup
            "agent6": 400000,
            "agent7": 400000,
            "agent8": 400000,
        })

    # Seed moving averages from DB
    _guard.refresh_moving_averages()
    log.info("TokenGuard initialized")
    return _guard
