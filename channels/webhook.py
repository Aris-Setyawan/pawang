"""Multi-Platform Webhook Adapter — send messages to external platforms.

Supports Discord webhooks, Slack webhooks, and generic HTTP webhooks.
Configured in config.yaml:

  webhooks:
    discord:
      url: https://discord.com/api/webhooks/xxx/yyy
      enabled: true
    slack:
      url: https://hooks.slack.com/services/xxx/yyy/zzz
      enabled: true
    custom:
      url: https://example.com/webhook
      enabled: true
      headers:
        Authorization: "Bearer xxx"
"""

import httpx
from typing import Optional

from core.logger import log


class WebhookAdapter:
    """Send messages to external platforms via webhooks."""

    def __init__(self, config: dict = None):
        self._webhooks = config or {}

    def configure(self, webhooks: dict):
        """Update webhook configuration."""
        self._webhooks = webhooks

    async def send(self, platform: str, text: str, **kwargs) -> bool:
        """Send a message to a configured webhook platform.

        Args:
            platform: "discord", "slack", "custom", etc.
            text: Message text
            **kwargs: Platform-specific options (username, avatar_url, etc.)

        Returns True if sent successfully.
        """
        wh = self._webhooks.get(platform, {})
        if not wh.get("enabled", False) or not wh.get("url", ""):
            log.warning(f"Webhook '{platform}' not configured or disabled")
            return False

        url = wh["url"]
        headers = wh.get("headers", {})

        try:
            if platform == "discord":
                return await self._send_discord(url, text, **kwargs)
            elif platform == "slack":
                return await self._send_slack(url, text, **kwargs)
            else:
                return await self._send_generic(url, text, headers, **kwargs)
        except Exception as e:
            log.error(f"Webhook send error ({platform}): {e}")
            return False

    async def _send_discord(self, url: str, text: str, **kwargs) -> bool:
        """Send message via Discord webhook."""
        # Discord has 2000 char limit
        payload = {
            "content": text[:2000],
            "username": kwargs.get("username", "Pawang"),
        }
        avatar = kwargs.get("avatar_url")
        if avatar:
            payload["avatar_url"] = avatar

        # Handle embeds for longer text
        if len(text) > 2000:
            payload["content"] = ""
            payload["embeds"] = [{
                "description": text[:4096],
                "color": 3447003,  # Blue
            }]

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return True

    async def _send_slack(self, url: str, text: str, **kwargs) -> bool:
        """Send message via Slack webhook."""
        payload = {
            "text": text[:40000],  # Slack limit
        }
        channel = kwargs.get("channel")
        if channel:
            payload["channel"] = channel

        username = kwargs.get("username")
        if username:
            payload["username"] = username

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return True

    async def _send_generic(self, url: str, text: str, headers: dict, **kwargs) -> bool:
        """Send message via generic HTTP webhook."""
        payload = {
            "text": text,
            "source": "pawang",
            **kwargs,
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return True

    async def broadcast(self, text: str, platforms: list[str] = None, **kwargs) -> dict[str, bool]:
        """Send message to multiple platforms.

        Args:
            text: Message to send
            platforms: List of platform names (default: all enabled)

        Returns dict of {platform: success_bool}
        """
        if platforms is None:
            platforms = [name for name, wh in self._webhooks.items() if wh.get("enabled")]

        results = {}
        for platform in platforms:
            results[platform] = await self.send(platform, text, **kwargs)
        return results

    @property
    def enabled_platforms(self) -> list[str]:
        return [name for name, wh in self._webhooks.items() if wh.get("enabled")]


# Global instance
webhook_adapter = WebhookAdapter()
