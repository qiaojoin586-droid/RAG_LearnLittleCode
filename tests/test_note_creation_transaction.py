"""A full background queue must not lose the note's durable review record."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core import task_runner
from app.models.note import Note
from app.models.note_index import note_index_jobs as jobs
from app.models.review import ReviewRecord
from app.schemas.note import NoteCreate
from app.services.note_service import NoteService


@pytest.mark.asyncio
@pytest.mark.parametrize("commit", [True, False])
async def test_note_and_review_share_transaction_even_when_tag_queue_is_full(monkeypatch, commit):
    monkeypatch.setattr(task_runner, "MAX_BACKGROUND_TASKS", 0)
    engine = sa.create_engine("sqlite://")
    Note.__table__.create(engine)
    ReviewRecord.__table__.create(engine)
    jobs.create(engine)
    with Session(engine) as session:
        async def flush():
            session.flush()

        async def refresh(entity):
            session.refresh(entity)

        async def execute(statement):
            return session.execute(statement)

        db = SimpleNamespace(add=session.add, flush=flush, refresh=refresh, execute=execute)
        note = await NoteService().create_note(db, "u", NoteCreate(title="t", content="c"))
        review = session.scalar(sa.select(ReviewRecord))
        assert review.note_id == note.id and review.user_id == "u"
        job = session.execute(sa.select(jobs)).mappings().one()
        assert job['note_id'] == note.id and job['status'] == 'queued'
        session.commit() if commit else session.rollback()
    with Session(engine) as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Note)) == int(commit)
        assert session.scalar(sa.select(sa.func.count()).select_from(ReviewRecord)) == int(commit)
        assert session.scalar(sa.select(sa.func.count()).select_from(jobs)) == int(commit)
    engine.dispose()


@pytest.mark.asyncio
async def test_offline_vector_store_does_not_block_admission(monkeypatch):
    monkeypatch.setattr(task_runner, "MAX_BACKGROUND_TASKS", 0)
    engine = sa.create_engine("sqlite://")
    Note.__table__.create(engine)
    ReviewRecord.__table__.create(engine)
    jobs.create(engine)
    with Session(engine) as session:
        async def flush():
            session.flush()

        async def refresh(entity):
            session.refresh(entity)

        async def execute(statement):
            return session.execute(statement)

        vector = SimpleNamespace(upsert_document=AsyncMock(side_effect=RuntimeError("offline")))
        service = NoteService(vector_store=vector)
        await service.create_note(SimpleNamespace(add=session.add, flush=flush, refresh=refresh, execute=execute),
                                  "u", NoteCreate(title="t", content="c"))
        session.commit()
        vector.upsert_document.assert_not_awaited()
        assert session.scalar(sa.select(jobs.c.status)) == 'queued'
    engine.dispose()
