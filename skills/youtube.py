"""YouTube skill — fetch video info and transcript."""

import re
import httpx
from .base import Skill, SkillResult


class YouTubeSkill(Skill):
    def __init__(self):
        super().__init__(
            name="youtube",
            description="Get YouTube video info, transcript, or summary",
            usage="/skill youtube <url>",
            category="research",
        )

    def _extract_video_id(self, url: str) -> str:
        """Extract video ID from various YouTube URL formats."""
        patterns = [
            r'(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})',
            r'(?:embed/)([a-zA-Z0-9_-]{11})',
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return url.strip()  # assume it's already an ID

    async def execute(self, args: str, **kwargs) -> SkillResult:
        if not args.strip():
            return SkillResult(success=False, output="Usage: youtube <url or video_id>")

        video_id = self._extract_video_id(args.strip())

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Fetch video page for basic info
                resp = await client.get(
                    f"https://www.youtube.com/watch?v={video_id}",
                    headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"},
                )
                html = resp.text

            # Extract title
            title_match = re.search(r'<title>(.*?)</title>', html)
            title = title_match.group(1).replace(" - YouTube", "") if title_match else "Unknown"

            # Extract description
            desc_match = re.search(r'"shortDescription":"(.*?)"', html)
            description = ""
            if desc_match:
                description = desc_match.group(1)[:500].encode().decode('unicode_escape', errors='replace')

            # Extract duration
            dur_match = re.search(r'"lengthSeconds":"(\d+)"', html)
            duration = ""
            if dur_match:
                secs = int(dur_match.group(1))
                mins, s = divmod(secs, 60)
                hours, m = divmod(mins, 60)
                duration = f"{hours}:{m:02d}:{s:02d}" if hours else f"{m}:{s:02d}"

            # Extract view count
            views_match = re.search(r'"viewCount":"(\d+)"', html)
            views = views_match.group(1) if views_match else "?"

            output = (
                f"Title: {title}\n"
                f"Duration: {duration}\n"
                f"Views: {int(views):,}\n" if views != "?" else f"Views: ?\n"
            )
            if description:
                output += f"\nDescription:\n{description}"

            output += f"\n\nURL: https://youtu.be/{video_id}"

            return SkillResult(success=True, output=output)

        except Exception as e:
            return SkillResult(success=False, output=f"YouTube error: {e}")
