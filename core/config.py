"""Pawang Config — Load YAML config with env var substitution."""

import os
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import yaml
from dotenv import load_dotenv

# Load .env file from project root
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path, override=True)


CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

_ENV_PATTERN = re.compile(r"\$\{(\w+)\}")


def _resolve_env(value):
    """Replace ${VAR} with environment variable value."""
    if isinstance(value, str):
        def _replace(m):
            val = os.environ.get(m.group(1), "")
            if not val:
                from core.logger import log
                log.warning(f"Config: env var ${{{m.group(1)}}} not set")
            return val
        return _ENV_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    api_format: str  # openai | anthropic | gemini
    models: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)  # sdk, python, nodejs, openai, curl (reference only)


@dataclass
class AgentConfig:
    id: str
    name: str
    model: str
    provider: str
    system_prompt_file: str = ""
    channels: list[str] = field(default_factory=list)
    max_context_tokens: int = 100000
    temperature: float = 0.7
    max_iterations: int = 15  # tool loop iteration budget
    fallbacks: list[str] = field(default_factory=list)  # model fallback chain
    chat_model: str = ""      # cheap model for regular chat (no tools)
    chat_provider: str = ""   # provider for chat_model


@dataclass
class TelegramConfig:
    token: str
    allowed_users: list[int] = field(default_factory=list)
    default_agent: str = "agent1"
    admin_chat_id: str = ""


@dataclass
class GatewayConfig:
    host: str = "0.0.0.0"
    port: int = 18800
    log_level: str = "info"


@dataclass
class HealthConfig:
    check_interval: int = 60
    auto_failover: bool = True
    fallback_chain: list[list[str]] = field(default_factory=list)


@dataclass
class PanelConfig:
    enabled: bool = True
    port: int = 18801
    username: str = "admin"
    password: str = ""


@dataclass
class SchedulerConfig:
    enabled: bool = True
    balance_alert_interval: int = 21600  # 6 hours
    health_report_interval: int = 300    # 5 minutes
    db_cleanup_interval: int = 86400     # 24 hours


@dataclass
class PawangConfig:
    gateway: GatewayConfig
    providers: dict[str, ProviderConfig]
    agents: list[AgentConfig]
    telegram: TelegramConfig
    health: HealthConfig
    panel: PanelConfig
    scheduler: SchedulerConfig
    smart_routing: dict = None  # {enabled, cheap_provider, cheap_model}
    session_timeout: int = 14400  # idle timeout in seconds (default 4h)

    def get_provider(self, name: str) -> Optional[ProviderConfig]:
        return self.providers.get(name)

    def get_agent(self, agent_id: str) -> Optional[AgentConfig]:
        for agent in self.agents:
            if agent.id == agent_id:
                return agent
        return None

    def get_agent_provider(self, agent_id: str) -> Optional[ProviderConfig]:
        agent = self.get_agent(agent_id)
        if agent:
            return self.get_provider(agent.provider)
        return None


def load_config(path: Path = CONFIG_PATH) -> PawangConfig:
    """Load and parse config.yaml with env var resolution."""
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise RuntimeError(f"Config file not found: {path}")
    except yaml.YAMLError as e:
        raise RuntimeError(f"Invalid YAML in config: {e}")
    if not raw or not isinstance(raw, dict):
        raise RuntimeError(f"Config file is empty or invalid: {path}")
    raw = _resolve_env(raw)

    gateway = GatewayConfig(**raw.get("gateway", {}))

    providers = {}
    for name, prov in raw.get("providers", {}).items():
        providers[name] = ProviderConfig(name=name, **prov)

    agents = [AgentConfig(**a) for a in raw.get("agents", [])]

    tg_raw = raw.get("channels", {}).get("telegram", {})
    telegram = TelegramConfig(**tg_raw)

    health_raw = raw.get("health", {})
    health = HealthConfig(**health_raw)

    panel_raw = raw.get("panel", {})
    auth = panel_raw.pop("auth", {})
    panel = PanelConfig(
        enabled=panel_raw.get("enabled", True),
        port=panel_raw.get("port", 18801),
        username=auth.get("username", "admin"),
        password=auth.get("password", ""),
    )

    scheduler_raw = raw.get("scheduler", {})
    scheduler = SchedulerConfig(**scheduler_raw)

    smart_routing = raw.get("smart_routing", None)
    session_timeout = raw.get("session_timeout", 14400)

    return PawangConfig(
        gateway=gateway,
        providers=providers,
        agents=agents,
        telegram=telegram,
        health=health,
        panel=panel,
        scheduler=scheduler,
        smart_routing=smart_routing,
        session_timeout=session_timeout,
    )


# Singleton
_config: Optional[PawangConfig] = None


def get_config() -> PawangConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config():
    global _config
    _config = load_config()


def save_config(config: PawangConfig, path: Path = CONFIG_PATH):
    """Write current runtime config back to config.yaml, preserving structure."""
    raw = yaml.safe_load(path.read_text())

    # Update providers from runtime
    raw_providers = raw.get("providers", {})
    for name, prov in config.providers.items():
        if name not in raw_providers:
            # New provider added at runtime
            entry = {
                "base_url": prov.base_url,
                "api_key": f"${{{name.upper()}_API_KEY}}",
                "api_format": prov.api_format,
                "models": prov.models,
            }
            if prov.methods:
                entry["methods"] = prov.methods
            raw_providers[name] = entry
        else:
            rp = raw_providers[name]
            rp["base_url"] = prov.base_url
            rp["api_format"] = prov.api_format
            rp["models"] = prov.models
            if prov.methods:
                rp["methods"] = prov.methods
    # Remove providers deleted at runtime
    for name in list(raw_providers.keys()):
        if name not in config.providers:
            del raw_providers[name]
    raw["providers"] = raw_providers

    # Update agent names/models/providers/temps from runtime
    raw_agents = raw.get("agents", [])
    for ra in raw_agents:
        agent = config.get_agent(ra.get("id", ""))
        if agent:
            ra["name"] = agent.name
            ra["model"] = agent.model
            ra["provider"] = agent.provider
            ra["temperature"] = agent.temperature
            ra["max_iterations"] = agent.max_iterations
            ra["fallbacks"] = agent.fallbacks
            if agent.chat_model:
                ra["chat_model"] = agent.chat_model
                ra["chat_provider"] = agent.chat_provider

    path.write_text(yaml.dump(raw, default_flow_style=False, allow_unicode=True, sort_keys=False))
