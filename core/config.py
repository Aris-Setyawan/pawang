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
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
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


@dataclass
class TelegramConfig:
    token: str
    allowed_users: list[int] = field(default_factory=list)
    default_agent: str = "agent1"


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
class PawangConfig:
    gateway: GatewayConfig
    providers: dict[str, ProviderConfig]
    agents: list[AgentConfig]
    telegram: TelegramConfig
    health: HealthConfig
    panel: PanelConfig

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
    raw = yaml.safe_load(path.read_text())
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

    return PawangConfig(
        gateway=gateway,
        providers=providers,
        agents=agents,
        telegram=telegram,
        health=health,
        panel=panel,
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
