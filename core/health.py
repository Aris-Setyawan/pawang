"""Health Monitor — checks provider health and triggers auto-failover."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from core.config import PawangConfig
from core.logger import log


@dataclass
class ProviderStatus:
    name: str
    healthy: bool = True
    last_check: float = 0.0
    last_error: str = ""
    latency_ms: float = 0.0
    consecutive_failures: int = 0
    total_requests: int = 0
    total_errors: int = 0
    auth_valid: bool = True  # False if 401/403
    rate_limited: bool = False  # True if 429


class HealthMonitor:
    """Monitors provider health and manages failover."""

    def __init__(self, config: PawangConfig):
        self.config = config
        self._status: dict[str, ProviderStatus] = {}
        self._task: Optional[asyncio.Task] = None
        self._failover_map: dict[str, str] = {}  # agent_id -> fallback_agent_id

        # Init status for all providers
        for name in config.providers:
            self._status[name] = ProviderStatus(name=name)

        # Build failover map from config
        for chain in config.health.fallback_chain:
            if len(chain) >= 2:
                self._failover_map[chain[0]] = chain[1]

    def get_status(self, provider_name: str) -> Optional[ProviderStatus]:
        return self._status.get(provider_name)

    def get_all_status(self) -> dict[str, ProviderStatus]:
        return self._status.copy()

    def get_failover_agent(self, agent_id: str) -> Optional[str]:
        """Get fallback agent ID for a given agent."""
        return self._failover_map.get(agent_id)

    async def check_provider(self, provider_name: str) -> bool:
        """Health check a single provider. Returns True if healthy."""
        prov = self.config.get_provider(provider_name)
        if not prov or not prov.api_key:
            return False

        status = self._status[provider_name]
        start = time.monotonic()

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                if prov.api_format == "gemini":
                    # Gemini: list models endpoint
                    url = f"{prov.base_url}/models?key={prov.api_key}&pageSize=1"
                    resp = await client.get(url)
                elif prov.api_format == "anthropic":
                    # Anthropic: just check the endpoint responds
                    resp = await client.get(
                        f"{prov.base_url}/models",
                        headers={
                            "x-api-key": prov.api_key,
                            "anthropic-version": "2023-06-01",
                        },
                    )
                else:
                    # OpenAI-compat: models endpoint
                    resp = await client.get(
                        f"{prov.base_url}/models",
                        headers={"Authorization": f"Bearer {prov.api_key}"},
                    )

            latency = (time.monotonic() - start) * 1000
            status.latency_ms = latency
            status.last_check = time.time()

            code = resp.status_code

            if code in (401, 403):
                # Auth failed — key invalid
                status.auth_valid = False
                status.rate_limited = False
                status.healthy = False
                status.last_error = f"HTTP {code} — invalid API key"
                return False
            elif code == 429:
                # Rate limited
                status.auth_valid = True
                status.rate_limited = True
                status.healthy = True  # still works, just throttled
                status.consecutive_failures = 0
                status.last_error = "rate limited"
                return True
            elif code < 500:
                # OK
                status.auth_valid = True
                status.rate_limited = False
                status.healthy = True
                status.consecutive_failures = 0
                status.last_error = ""
                return True
            else:
                raise Exception(f"HTTP {code}")

        except Exception as e:
            status.healthy = False
            status.consecutive_failures += 1
            status.total_errors += 1
            status.last_error = str(e)[:200]
            status.last_check = time.time()
            log.warning(f"Health check failed for {provider_name}: {e}")
            return False

    async def check_all(self):
        """Check all providers concurrently."""
        tasks = [self.check_provider(name) for name in self.config.providers]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_loop(self):
        """Background health check loop."""
        interval = self.config.health.check_interval
        log.info(f"Health monitor started (interval={interval}s)")
        while True:
            try:
                await self.check_all()
                healthy = [n for n, s in self._status.items() if s.healthy]
                unhealthy = [n for n, s in self._status.items() if not s.healthy]
                if unhealthy:
                    log.warning(f"Unhealthy providers: {unhealthy}")
                else:
                    log.debug(f"All providers healthy: {healthy}")
            except Exception as e:
                log.error(f"Health check loop error: {e}")
            await asyncio.sleep(interval)

    def start(self):
        """Start background health monitoring."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())

    def stop(self):
        """Stop health monitoring."""
        if self._task and not self._task.done():
            self._task.cancel()
            log.info("Health monitor stopped")

    def record_request(self, provider_name: str, success: bool, error: str = ""):
        """Record a real request result for health tracking."""
        status = self._status.get(provider_name)
        if not status:
            return
        status.total_requests += 1
        if not success:
            status.total_errors += 1
            status.consecutive_failures += 1
            status.last_error = error[:200]
            if status.consecutive_failures >= 3:
                status.healthy = False
                log.warning(f"Provider {provider_name} marked unhealthy after {status.consecutive_failures} failures")
        else:
            status.consecutive_failures = 0
