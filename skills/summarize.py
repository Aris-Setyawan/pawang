"""Summarize skill — summarize URLs or text."""

import httpx
from .base import Skill, SkillResult


class SummarizeSkill(Skill):
    def __init__(self):
        super().__init__(
            name="summarize",
            description="Summarize a URL or text content",
            usage="/skill summarize <url or text>",
            category="research",
        )

    async def execute(self, args: str, **kwargs) -> SkillResult:
        if not args.strip():
            return SkillResult(success=False, output="Usage: summarize <url or text>")

        text = args.strip()

        # If it's a URL, fetch content
        if text.startswith(("http://", "https://")):
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                    resp = await client.get(
                        text,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; Pawang/1.0)"},
                    )
                    content_type = resp.headers.get("content-type", "")
                    if "text/html" in content_type:
                        # Simple HTML text extraction
                        import re
                        html = resp.text
                        # Remove scripts and styles
                        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
                        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
                        # Remove tags
                        text = re.sub(r'<[^>]+>', ' ', html)
                        # Clean whitespace
                        text = re.sub(r'\s+', ' ', text).strip()
                        # Truncate for summarization
                        text = text[:8000]
                    else:
                        text = resp.text[:8000]
            except Exception as e:
                return SkillResult(success=False, output=f"Failed to fetch URL: {e}")

        if len(text) < 50:
            return SkillResult(success=False, output="Text too short to summarize.")

        # Use AI to summarize
        config = kwargs.get("config")
        if config:
            from core import completion
            from providers.base import Message

            messages = [
                Message(role="system", content=(
                    "Summarize the following content concisely in 3-5 bullet points. "
                    "Match the user's language (Indonesian/English). "
                    "Focus on key points and actionable information."
                )),
                Message(role="user", content=f"Summarize:\n\n{text}"),
            ]

            # Pick fastest model
            for pname, mname in [
                ("deepseek", "deepseek-chat"),
                ("google", "gemini-2.0-flash"),
                ("openai", "gpt-4o-mini"),
            ]:
                prov = config.get_provider(pname)
                if prov and prov.api_key:
                    try:
                        response = await completion.complete(
                            config=config,
                            provider_name=pname,
                            model=mname,
                            messages=messages,
                            temperature=0.3,
                            max_tokens=1000,
                        )
                        return SkillResult(success=True, output=response.text)
                    except Exception:
                        continue

        # Fallback: simple extraction
        sentences = text.split(". ")
        summary = ". ".join(sentences[:5]) + "..."
        return SkillResult(success=True, output=f"Summary (basic):\n{summary}")
