"""GitHub skill — interact with GitHub via gh CLI or API."""

import asyncio
from .base import Skill, SkillResult


class GitHubSkill(Skill):
    def __init__(self):
        super().__init__(
            name="github",
            description="GitHub operations: issues, PRs, repos, CI status via gh CLI",
            usage="/skill github <command>",
            category="development",
        )

    async def execute(self, args: str, **kwargs) -> SkillResult:
        if not args.strip():
            return SkillResult(
                success=False,
                output=(
                    "Usage: github <command>\n"
                    "Examples:\n"
                    "  github issue list --repo owner/repo\n"
                    "  github pr list --repo owner/repo\n"
                    "  github repo view owner/repo\n"
                    "  github run list --repo owner/repo"
                ),
            )

        cmd = args.strip()
        # Ensure it starts with a valid gh subcommand
        valid_starts = ("issue", "pr", "repo", "run", "release", "api", "search")
        if not any(cmd.startswith(s) for s in valid_starts):
            return SkillResult(
                success=False,
                output=f"Invalid command. Must start with: {', '.join(valid_starts)}",
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", *cmd.split(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode() if stdout else ""
            errors = stderr.decode() if stderr else ""

            if proc.returncode != 0:
                return SkillResult(
                    success=False,
                    output=f"gh error:\n{errors or output}",
                )

            return SkillResult(success=True, output=output[:4000])

        except FileNotFoundError:
            return SkillResult(success=False, output="gh CLI not installed. Run: apt install gh")
        except asyncio.TimeoutError:
            return SkillResult(success=False, output="Command timed out (30s)")
        except Exception as e:
            return SkillResult(success=False, output=f"Error: {e}")
