"""Base skill interface."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SkillResult:
    """Result from a skill execution."""
    success: bool
    output: str
    file_path: Optional[str] = None  # if skill produced a file
    metadata: dict = field(default_factory=dict)


@dataclass
class Skill:
    """A skill that agents can use."""
    name: str
    description: str
    usage: str  # usage example
    category: str = "general"
    requires_api_key: Optional[str] = None  # env var name

    async def execute(self, args: str, **kwargs) -> SkillResult:
        """Execute the skill. Override in subclasses."""
        return SkillResult(success=False, output="Not implemented")
