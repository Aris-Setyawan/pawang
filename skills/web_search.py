"""Web Search skill — free search via DuckDuckGo."""

import httpx
from .base import Skill, SkillResult


class WebSearchSkill(Skill):
    def __init__(self):
        super().__init__(
            name="web_search",
            description="Search the web using DuckDuckGo (free, no API key)",
            usage="/skill web_search <query>",
            category="research",
        )

    async def execute(self, args: str, **kwargs) -> SkillResult:
        if not args.strip():
            return SkillResult(success=False, output="Usage: web_search <query>")

        query = args.strip()
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # DuckDuckGo instant answer API
                resp = await client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
                )
                data = resp.json()

            results = []

            # Abstract (instant answer)
            if data.get("Abstract"):
                results.append(f"Summary: {data['Abstract']}")
                if data.get("AbstractSource"):
                    results.append(f"Source: {data['AbstractSource']} — {data.get('AbstractURL', '')}")

            # Related topics
            for topic in data.get("RelatedTopics", [])[:5]:
                if isinstance(topic, dict) and topic.get("Text"):
                    results.append(f"- {topic['Text'][:200]}")

            # Answer
            if data.get("Answer"):
                results.append(f"Answer: {data['Answer']}")

            if not results:
                # Fallback: use DuckDuckGo HTML search
                resp2 = await client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": query},
                    headers={"User-Agent": "Mozilla/5.0"},
                    follow_redirects=True,
                )
                # Simple extraction
                results.append(f"Search results for '{query}' — use a browser for full results.")

            output = "\n".join(results) if results else f"No results for '{query}'"
            return SkillResult(success=True, output=output)

        except Exception as e:
            return SkillResult(success=False, output=f"Search error: {e}")
