"""聊天缓存回填必须保持最近窗口与时间顺序。"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.services import database_session_manager as sessions


class MemoryRedis:
    def __init__(self):
        self.data = {}

    async def delete(self, key):
        self.data.pop(key, None)

    async def lpush(self, key, item):
        self.data.setdefault(key, []).insert(0, item)

    async def ltrim(self, key, start, end):
        self.data[key] = self.data.get(key, [])[start:end + 1]

    async def lrange(self, key, start, end):
        values = self.data.get(key, [])
        return values[start:] if end == -1 else values[start:end + 1]


@pytest.mark.parametrize("message_count", [0, 3, 10, 12])
def test_cache_rebuild_keeps_latest_messages_in_chronological_order(monkeypatch, message_count):
    redis = MemoryRedis()
    monkeypatch.setattr(sessions, "get_redis", lambda: redis)
    manager = sessions.DatabaseSessionManager(chat_buffer_size=10)
    manager.get_session = AsyncMock()
    messages = [{
        "id": i, "content": str(i), "role": "user",
        "created_at": f"2026-10-25T00:00:{i:02d}+00:00",
    } for i in range(1, message_count + 1)]

    async def run():
        await manager._rebuild_redis_cache("session", messages)
        if not messages:
            assert await redis.lrange("chat:msgs:session", 0, -1) == []
            return
        recent = await manager.get_recent_messages("session")
        history, _ = await manager.get_messages(None, "session", "user")
        expected = list(range(max(1, message_count - 9), message_count + 1))
        assert [msg["id"] for msg in recent] == expected
        assert [msg.id for msg in history] == expected

    asyncio.run(run())
