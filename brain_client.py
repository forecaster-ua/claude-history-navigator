import aiohttp
import json
from datetime import datetime

BRAIN_URL = "http://localhost:3100"


async def brain_search(query: str, limit: int = 10) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{BRAIN_URL}/api/search",
            params={"q": query, "limit": limit},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            return {"results": [], "total": 0, "error": f"HTTP {resp.status}"}


async def brain_save(text: str, session_id: str, tags: list[str] = None) -> dict:
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_id = session_id[:8] if session_id else "unknown"
    all_tags = ["claude-history", short_id] + (tags or [])

    payload = {
        "files": [{
            "name": f"{now}_{short_id}_claude_history.md",
            "content": text,
            "tags": all_tags
        }],
        "source": "claude-history"
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{BRAIN_URL}/api/import/files",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status in (200, 201):
                return {"ok": True}
            body = await resp.text()
            return {"ok": False, "error": body}
