import aiohttp
from typing import Any, Dict

from .parser import parse_current_weather


class GismeteoMCPService:
    def __init__(self, source_url: str, logger: Any, timeout_sec: int = 20):
        self.source_url = source_url
        self.logger = logger
        self.timeout_sec = timeout_sec

    async def fetch_current_weather(self, trace_id: str = "") -> Dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml"}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(self.source_url, headers=headers) as resp:
                html = await resp.text()
                if resp.status < 200 or resp.status >= 300:
                    raise RuntimeError(f"Gismeteo HTTP {resp.status}")
        parsed = parse_current_weather(html, self.source_url)
        parsed["trace_id"] = trace_id
        return parsed
