"""YAML Skill Loader — load procedure skills from YAML files.

Skills are YAML files with frontmatter metadata and markdown body.
Placed in skills/procedures/ directory.

Example skills/procedures/deploy-check.yaml:
---
name: deploy-check
description: Check deployment readiness
category: devops
usage: /skill deploy-check
steps:
  - run: "git status"
  - run: "docker ps"
  - run: "systemctl status pawang"
---
Check if all services are healthy and ready for deployment.
"""

import asyncio
import os
from pathlib import Path
from typing import Optional

import yaml

from skills.base import Skill, SkillResult
from core.logger import log

PROCEDURES_DIR = Path(__file__).parent / "procedures"


class YAMLSkill(Skill):
    """A skill loaded from a YAML procedure file."""

    def __init__(self, meta: dict, body: str, file_path: str):
        super().__init__(
            name=meta.get("name", "unnamed"),
            description=meta.get("description", ""),
            usage=meta.get("usage", ""),
            category=meta.get("category", "procedure"),
        )
        self.steps = meta.get("steps", [])
        self.body = body
        self.file_path = file_path
        self.timeout = meta.get("timeout", 60)

    async def execute(self, args: str, **kwargs) -> SkillResult:
        """Execute the YAML skill steps."""
        if not self.steps:
            return SkillResult(success=True, output=self.body or "(No steps defined)")

        outputs = []
        for i, step in enumerate(self.steps, 1):
            if isinstance(step, dict) and "run" in step:
                cmd = step["run"]
                # Substitute {args} placeholder
                cmd = cmd.replace("{args}", args or "")
                label = step.get("label", cmd[:60])
                outputs.append(f"[Step {i}] {label}")

                try:
                    proc = await asyncio.create_subprocess_shell(
                        cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=step.get("cwd", "/root/pawang"),
                    )
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=self.timeout,
                    )
                    out = stdout.decode(errors="replace")
                    if stderr:
                        out += "\n" + stderr.decode(errors="replace")
                    outputs.append(out[:2000] if out else "(no output)")

                    # Stop on failure if step requires it
                    if step.get("required", False) and proc.returncode != 0:
                        outputs.append(f"Step {i} failed (exit {proc.returncode}), stopping.")
                        return SkillResult(success=False, output="\n".join(outputs))

                except asyncio.TimeoutError:
                    outputs.append(f"Step {i} timed out after {self.timeout}s")
                    return SkillResult(success=False, output="\n".join(outputs))

            elif isinstance(step, dict) and "echo" in step:
                outputs.append(step["echo"])

        return SkillResult(success=True, output="\n".join(outputs))


def load_yaml_skills() -> list[YAMLSkill]:
    """Load all YAML skill files from the procedures directory."""
    if not PROCEDURES_DIR.exists():
        PROCEDURES_DIR.mkdir(parents=True, exist_ok=True)
        return []

    skills = []
    for path in PROCEDURES_DIR.glob("*.yaml"):
        try:
            skill = _parse_yaml_skill(path)
            if skill:
                skills.append(skill)
        except Exception as e:
            log.warning(f"Failed to load YAML skill {path.name}: {e}")

    for path in PROCEDURES_DIR.glob("*.yml"):
        try:
            skill = _parse_yaml_skill(path)
            if skill:
                skills.append(skill)
        except Exception as e:
            log.warning(f"Failed to load YAML skill {path.name}: {e}")

    if skills:
        log.info(f"Loaded {len(skills)} YAML procedure skills")
    return skills


def _parse_yaml_skill(path: Path) -> Optional[YAMLSkill]:
    """Parse a single YAML skill file with frontmatter."""
    content = path.read_text(encoding="utf-8")

    # Split frontmatter from body
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            meta = yaml.safe_load(parts[1]) or {}
            body = parts[2].strip()
        else:
            meta = yaml.safe_load(content) or {}
            body = ""
    else:
        meta = yaml.safe_load(content) or {}
        body = ""

    if not isinstance(meta, dict):
        return None

    # Default name from filename
    if "name" not in meta:
        meta["name"] = path.stem

    return YAMLSkill(meta=meta, body=body, file_path=str(path))
