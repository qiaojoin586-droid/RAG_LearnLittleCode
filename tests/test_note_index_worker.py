"""Note Outbox lifecycle, SQL visibility and fenced real Chroma writes."""

from datetime import datetime
from pathlib import Path
import os
from unittest.mock import AsyncMock

from fastapi import Response
import pytest
import sqlalchemy as sa

from app.core.failed_response import BusinessError
from app.models.note import Note
from app.models.note_index import note_index_jobs as jobs
from app.rag.note_index import rebuild_note_index, retry_failed_note_index
from app.rag.note_index_worker import LeaseLost, NoteIndexWorker
from app.rag.vector_store import VectorStoreService
from app.rag.visibility import CandidateVisibilityService
from app.routers.note_router import get_note_index_source
from app.schemas.note import BatchOperation, NoteCreate, NoteUpdate
from test_model_config_control_plane import store  # noqa: F401
from test_transactional_tool_operations import local_tools  # noqa: F401


@pytest.fixture
def note_vectors(tmp_path, monkeypatch):
    import chromadb
    from chromadb.config import Settings

    for name in ('TEMP', 'TMP', 'SQLITE_TMPDIR'):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.setenv('DB_CONFIG_REQUIRED', 'false')
    with chromadb.PersistentClient(path=os.path.relpath(tmp_path, Path.cwd()).replace('\\', '/'),
                                   settings=Settings(anonymized_telemetry=False)) as client:
        vectors = object.__new__(VectorStoreService)
        options = {'embedding_function': None, 'metadata': {'hnsw:space': 'cosine'}}
        vectors._notes_collection = client.create_collection('notes', **options)
        vectors._rag_collection = client.create_collection('rag', **options)
        vectors._generate_embeddings = AsyncMock(return_value=[[1.0, 0.0]])
        yield vectors


async def create(fx, content='original'):
    async with fx.factory() as db, db.begin():
        note = await fx.note_service.create_note(db, fx.user_id, NoteCreate(title='fixture', content=content), after_commit=[])
        return note.id


async def edit(fx, note_id, content):
    async with fx.factory() as db, db.begin():
        await fx.note_service.update_note(db, note_id, fx.user_id, NoteUpdate(content=content))


@pytest.mark.asyncio
async def test_note_index_blocks_old_revision_through_edit_delete_and_restore(local_tools, note_vectors):
    fx = local_tools
    fx.note_service.vector_store = note_vectors
    worker = NoteIndexWorker(fx.factory, vectors=note_vectors)
    visibility = CandidateVisibilityService(fx.factory)
    note_id = await create(fx)

    async def candidates():
        return [{**row, 'source': 'notes'} for row in await note_vectors.search_notes('query', fx.user_id)]

    assert await worker.tick()
    original = await candidates()
    [visible] = await visibility.filter(original, fx.user_id)
    assert visible['source_locator']['char_end'] == len('original')
    await edit(fx, note_id, 'intermediate')
    obsolete = await worker.claim()
    await edit(fx, note_id, 'original')
    assert await visibility.filter(original, fx.user_id) == []
    async with fx.factory() as db:
        assert await fx.note_service.semantic_search(db, fx.user_id, 'query') == []
    assert await worker.tick()
    current = await candidates()
    [current_hit] = await visibility.filter(original + current, fx.user_id)
    assert current_hit['metadata']['index_revision'] == current[0]['metadata']['index_revision']
    assert current_hit['source_locator']['resource_version_id'] != visible['source_locator']['resource_version_id']
    with pytest.raises(LeaseLost):
        async with worker._publish(obsolete):
            pytest.fail('superseded attempt started a vector mutation')
    async with fx.factory() as db:
        assert [note.id for note, _ in await fx.note_service.semantic_search(db, fx.user_id, 'query')] == [note_id]

    # A rolled-back deletion retains visibility. A committed one hides evidence before cleanup.
    async with fx.factory() as db:
        await fx.note_service.delete_note(db, note_id, fx.user_id)
        await db.rollback()
    assert len(await visibility.filter(current, fx.user_id)) == 1
    async with fx.factory() as db, db.begin():
        await fx.note_service.delete_note(db, note_id, fx.user_id)
    pending_delete = await worker.claim()
    assert await visibility.filter(current, fx.user_id) == []
    async with fx.factory() as db, db.begin():
        await fx.note_service.restore_note(db, note_id, fx.user_id)
    assert await visibility.filter(current, fx.user_id) == []
    assert await worker.tick()
    with pytest.raises(LeaseLost):
        await note_vectors.delete_note_vectors(note_id, user_id=fx.user_id, write_guard=lambda: worker._publish(pending_delete))
    restored = await candidates()
    assert len(await visibility.filter(current + restored, fx.user_id)) == len(restored) == 1

    note_vectors._notes_collection.upsert(ids=['foreign'], documents=['foreign'], embeddings=[[1., 0.]],
                                         metadatas=[{'user_id': fx.store.other.user_id, 'note_id': note_id}])
    async with fx.factory() as db, db.begin():
        await fx.note_service.delete_note(db, note_id, fx.user_id)
        await fx.note_service.permanent_delete(db, note_id, fx.user_id)
    assert not fx.store.rows(Note.__table__) and fx.store.rows(jobs)[0]['operation'] == 'delete'
    assert await worker.tick()
    assert note_vectors._notes_collection.get()['ids'] == ['foreign']


@pytest.mark.asyncio
async def test_note_opt_out_hides_stored_vectors_and_preserves_preference_on_restore(local_tools, note_vectors):
    fx = local_tools
    fx.note_service.vector_store = note_vectors
    worker = NoteIndexWorker(fx.factory, vectors=note_vectors)
    async with fx.factory() as db, db.begin():
        draft = await fx.note_service.create_note(db, fx.user_id,
            NoteCreate(title='private', content='private text', is_indexed=False), after_commit=[])
    assert not fx.store.rows(jobs) and not await worker.tick()
    async with fx.factory() as db, db.begin():
        await fx.note_service.update_note(db, draft.id, fx.user_id, NoteUpdate(is_indexed=True))
    assert await worker.tick()
    [hit] = await note_vectors.search_notes('query', fx.user_id)
    candidate = {**hit, 'source': 'notes'}
    visibility = CandidateVisibilityService(fx.factory)
    [visible] = await visibility.filter([candidate], fx.user_id)
    version = visible['source_locator']['resource_version_id']
    async with fx.factory() as db:
        assert (await get_note_index_source(draft.id, Response(), version, fx.user_id, db))['data']['content'] == hit['content']

    async with fx.factory() as db, db.begin():
        await fx.note_service.update_note(db, draft.id, fx.user_id, NoteUpdate(is_indexed=False))
        assert not await rebuild_note_index(db, fx.user_id, draft.id)
    assert note_vectors._notes_collection.count() == 1
    assert not await visibility.filter([candidate], fx.user_id)
    async with fx.factory() as db:
        assert await fx.note_service.semantic_search(db, fx.user_id, 'query') == []
        with pytest.raises(BusinessError) as denied:
            await get_note_index_source(draft.id, Response(), version, fx.user_id, db)
        assert denied.value.http_status == 404
    assert await worker.tick() and note_vectors._notes_collection.count() == 0
    async with fx.factory() as db, db.begin():
        await fx.note_service.delete_note(db, draft.id, fx.user_id)
        restored = await fx.note_service.restore_note(db, draft.id, fx.user_id)
        await fx.note_service.update_note(db, draft.id, fx.user_id, NoteUpdate(content='edited while disabled'))
        assert not restored.is_indexed
    assert await worker.tick() and note_vectors._notes_collection.count() == 0
    async with fx.factory() as db, db.begin():
        await fx.note_service.update_note(db, draft.id, fx.user_id, NoteUpdate(is_indexed=True))
    assert await worker.tick()
    [latest] = await note_vectors.search_notes('query', fx.user_id)
    assert latest['content'] == 'edited while disabled' and not await visibility.filter([candidate], fx.user_id)
    async with fx.factory() as db:
        with pytest.raises(BusinessError):
            await get_note_index_source(draft.id, Response(), version, fx.user_id, db)


@pytest.mark.asyncio
async def test_worker_rechecks_preference_after_embedding(local_tools, note_vectors):
    fx = local_tools
    note_id = await create(fx)

    async def revoke_before_write(texts):
        fx.store.sql(Note.__table__.update().where(Note.id == note_id).values(is_indexed=False))
        return [[1.0, 0.0]]

    note_vectors._generate_embeddings.side_effect = revoke_before_write
    assert await NoteIndexWorker(fx.factory, vectors=note_vectors).tick()
    assert fx.store.rows(jobs)[0]['error_code'] == 'NOTE_INDEX_SOURCE_UNAVAILABLE'
    assert note_vectors._notes_collection.count() == 0


@pytest.mark.asyncio
async def test_note_index_reclaims_lease_and_retries_failed_revision(local_tools, note_vectors):
    fx = local_tools
    note_id = await create(fx)
    worker = NoteIndexWorker(fx.factory, vectors=note_vectors)
    claimed = await worker.claim()
    past = datetime(2000, 1, 1)
    fx.store.sql(jobs.update().values(lease_expires_at=past))
    restarted = NoteIndexWorker(fx.factory, vectors=note_vectors)
    with pytest.raises(LeaseLost):
        async with worker._publish(claimed):
            pytest.fail('expired attempt started a vector mutation')
    note_vectors._generate_embeddings.side_effect = RuntimeError('offline')
    assert await restarted.tick()
    queued = fx.store.rows(jobs)[0]
    assert queued['attempts'] == 2 and queued['status'] == 'queued' and queued['next_attempt_at'] > past
    fx.store.sql(jobs.update().values(next_attempt_at=past))
    assert await restarted.tick()
    failed = fx.store.rows(jobs)[0]
    assert failed['attempts'] == 3 and failed['status'] == 'failed'
    assert failed['error_code'] == 'NOTE_INDEX_UPSERT_FAILED'
    assert note_vectors._notes_collection.count() == 0

    async with fx.factory() as db, db.begin():
        assert not await retry_failed_note_index(db, fx.store.other.user_id, note_id, failed['revision'])
        assert not await retry_failed_note_index(db, fx.user_id, note_id, 'stale')
        assert await retry_failed_note_index(db, fx.user_id, note_id, failed['revision'])
    note_vectors._generate_embeddings.side_effect = None
    assert await restarted.tick()
    assert fx.store.rows(jobs)[0]['status'] == 'succeeded'
    assert note_vectors._notes_collection.count() == 1


@pytest.mark.asyncio
async def test_batch_delete_rolls_back_note_when_its_index_job_cannot_be_written(local_tools, monkeypatch):
    from app.routers import note_router
    from app.services import note_service

    fx = local_tools
    failing, succeeding = await create(fx), await create(fx)
    queue = note_service.queue_note_index

    async def queue_with_one_failure(db, note, operation):
        if note.id == failing:
            raise RuntimeError('index job write failed')
        await queue(db, note, operation)

    monkeypatch.setattr(note_service, 'queue_note_index', queue_with_one_failure)
    monkeypatch.setattr(note_router, '_get_note_service', lambda: fx.note_service)
    async with fx.factory() as db:
        response = await note_router.batch_operation(BatchOperation(note_ids=[failing, succeeding], operation='delete'),
                                                     fx.user_id, db)
    assert response['data']['error_count'] == response['data']['success_count'] == 1
    notes = {row['id']: row for row in fx.store.rows(Note.__table__)}
    jobs_by_note = {row['note_id']: row for row in fx.store.rows(jobs)}
    assert notes[failing]['deleted_at'] is None and jobs_by_note[failing]['operation'] == 'upsert'
    assert notes[succeeding]['deleted_at'] is not None and jobs_by_note[succeeding]['operation'] == 'delete'
