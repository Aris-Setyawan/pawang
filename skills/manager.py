"""Skill Manager — loads and executes skills."""

from typing import Optional
from .base import Skill, SkillResult
from .web_search import WebSearchSkill
from .weather import WeatherSkill
from .summarize import SummarizeSkill
from .youtube import YouTubeSkill
from .github_skill import GitHubSkill


class SkillManager:
    """Registry and executor for all available skills."""

    def __init__(self):
        self._skills: dict[str, Skill] = {}
        self._load_builtin()

    def _load_builtin(self):
        """Load all built-in skills."""
        for skill in [
            WebSearchSkill(),
            WeatherSkill(),
            SummarizeSkill(),
            YouTubeSkill(),
            GitHubSkill(),
        ]:
            self._skills[skill.name] = skill

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def list_skills(self) -> list[Skill]:
        return list(self._skills.values())

    async def execute(self, name: str, args: str, **kwargs) -> SkillResult:
        skill = self.get(name)
        if not skill:
            available = ", ".join(self._skills.keys())
            return SkillResult(
                success=False,
                output=f"Skill '{name}' not found. Available: {available}",
            )
        return await skill.execute(args, **kwargs)
