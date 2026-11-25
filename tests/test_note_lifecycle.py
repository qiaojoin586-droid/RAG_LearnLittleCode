"""回收站的存储时间、保留期与展示均使用 UTC。"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import category_service, note_service
from app.utils import time_utils


def test_utc_now_is_independent_of_server_timezone(monkeypatch):
    fixed_utc = datetime(2026, 10, 25)

    class ShanghaiClock(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_utc + timedelta(hours=8)
            return fixed_utc.replace(tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(time_utils, "datetime", ShanghaiClock)
    assert time_utils.utc_now() == fixed_utc
    assert time_utils.utc_now().tzinfo is None


@pytest.mark.parametrize("kind", ["note", "category"])
@pytest.mark.parametrize("age,should_delete", [
    (timedelta(days=13, hours=20), False),
    (timedelta(days=14), False),
    (timedelta(days=14, seconds=1), True),
])
def test_cleanup_respects_full_utc_retention_period(monkeypatch, kind, age, should_delete):
    fixed_utc = datetime(2026, 10, 25)
    module = note_service if kind == "note" else category_service
    monkeypatch.setattr(module, "utc_now", lambda: fixed_utc)
    item = SimpleNamespace(id="recoverable", deleted_at=fixed_utc - age)
    result = MagicMock()

    async def execute(statement):
        cutoff = next(value for value in statement.compile().params.values()
                      if isinstance(value, datetime))
        result.scalars.return_value.all.return_value = [item] if item.deleted_at < cutoff else []
        return result

    db = SimpleNamespace(execute=execute, delete=AsyncMock(), flush=AsyncMock())
    if kind == "note":
        monkeypatch.setattr(note_service, 'queue_note_index', AsyncMock())
        cleanup = note_service.NoteService().cleanup_expired_notes
    else:
        service = category_service.CategoryService()
        service._promote_active_children = AsyncMock()
        cleanup = service.cleanup_expired_categories
    assert asyncio.run(cleanup(db, days=14)) == int(should_delete)
    assert db.delete.await_count == int(should_delete)
