"""Weather skill — current weather via wttr.in (free, no API key)."""

import httpx
from .base import Skill, SkillResult


class WeatherSkill(Skill):
    def __init__(self):
        super().__init__(
            name="weather",
            description="Get current weather for a location (free via wttr.in)",
            usage="/skill weather <city>",
            category="utility",
        )

    async def execute(self, args: str, **kwargs) -> SkillResult:
        location = args.strip() or "Jakarta"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"https://wttr.in/{location}",
                    params={"format": "j1"},
                    headers={"User-Agent": "curl/7.0"},
                )
                data = resp.json()

            current = data.get("current_condition", [{}])[0]
            area = data.get("nearest_area", [{}])[0]

            city = area.get("areaName", [{}])[0].get("value", location)
            country = area.get("country", [{}])[0].get("value", "")
            temp_c = current.get("temp_C", "?")
            feels = current.get("FeelsLikeC", "?")
            desc = current.get("weatherDesc", [{}])[0].get("value", "")
            humidity = current.get("humidity", "?")
            wind = current.get("windspeedKmph", "?")
            wind_dir = current.get("winddir16Point", "")

            output = (
                f"Weather: {city}, {country}\n"
                f"Condition: {desc}\n"
                f"Temperature: {temp_c}°C (feels like {feels}°C)\n"
                f"Humidity: {humidity}%\n"
                f"Wind: {wind} km/h {wind_dir}"
            )

            # 3-day forecast
            forecasts = data.get("weather", [])
            if forecasts:
                output += "\n\nForecast:"
                for day in forecasts[:3]:
                    date = day.get("date", "")
                    max_t = day.get("maxtempC", "?")
                    min_t = day.get("mintempC", "?")
                    desc_f = day.get("hourly", [{}])[4].get("weatherDesc", [{}])[0].get("value", "")
                    output += f"\n  {date}: {min_t}-{max_t}°C, {desc_f}"

            return SkillResult(success=True, output=output)

        except Exception as e:
            return SkillResult(success=False, output=f"Weather error: {e}")
