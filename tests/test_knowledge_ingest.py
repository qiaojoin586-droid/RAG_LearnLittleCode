"""Exercise the upload endpoint and durable worker with isolated storage."""

import asyncio
from contextlib import aclosing, asynccontextmanager, nullcontext
from datetime import datetime
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Response, UploadFile
import httpx
import pytest
from sqlalchemy.orm import Session

from app.core.failed_response import BusinessError
from app.model_config.snapshots import SnapshotMiddleware
from app.models.knowledge import KnowledgeDocument, knowledge_search_chunks
from app.models.knowledge_ingest import knowledge_ingest_jobs as jobs
from app.rag import ingest
from app.rag.ingest_worker import KnowledgeIngestWorker, LeaseLost
from app.rag.index_build_sources import Source, read_chunks
from app.rag.keyword_index import backfill_keyword_index
from app.rag.keyword_search import KeywordSearch
from app.rag.visibility import CandidateVisibilityService
from app.routers import knowledge_router
from test_model_config_control_plane import admin, store  # noqa: F401
from test_model_config_publication import selected
from test_transactional_tool_operations import ToolSession
from test_vector_binding_runtime import vectors  # noqa: F401


class VectorFixture:
    def __init__(self):
        self.records = {}

    async def upsert_document(self, documents, metadatas, ids, *, collection, write_guard=None):
        assert collection == 'rag'
        async with write_guard() if write_guard else nullcontext():
            self.records.update(zip(ids, zip(documents, metadatas, strict=True), strict=True))

    async def delete_document_vectors(self, document_id, *, user_id, write_guard=None):
        async with write_guard() if write_guard else nullcontext():
            self.records = {key: value for key, value in self.records.items()
                            if (value[1]['document_id'], value[1]['user_id']) != (document_id, user_id)}


@pytest.fixture
def knowledge(store, tmp_path, monkeypatch):
    KnowledgeDocument.__table__.create(store.engine)
    knowledge_search_chunks.create(store.engine)
    jobs.create(store.engine)

    @asynccontextmanager
    async def factory():
        with Session(store.engine, expire_on_commit=False) as session:
            yield ToolSession(session)

    async def submit(*args, **kwargs):
        return await ingest.submit_upload(*args, **kwargs, root=tmp_path)

    monkeypatch.setattr(knowledge_router, 'async_session_factory', factory)
    monkeypatch.setattr(knowledge_router, 'submit_upload', submit)
    monkeypatch.setattr(knowledge_router, 'UPLOAD_ROOT', tmp_path)
    return store, tmp_path, factory


def as_candidate(document_id, user_id):
    return {'source': 'knowledge', 'metadata': {'document_id': str(document_id), 'user_id': user_id},
            'content': 'fixture document'}


@pytest.mark.asyncio
async def test_http_acceptance_survives_disconnected_sse_and_duplicate_upload(knowledge):
    store, root, factory = knowledge
    owner = store.actor.user_id
    upload = await knowledge_router.upload_document(UploadFile(BytesIO(b'fixture document'), filename='source.txt'), owner)
    job_id = upload.headers['X-Knowledge-Job-Id']
    async with aclosing(upload.body_iterator) as events:
        assert 'queued' in await anext(events)
    document, = store.rows(KnowledgeDocument.__table__)
    candidate = as_candidate(document['id'], owner)
    visibility = CandidateVisibilityService(factory)
    assert await visibility.filter([candidate], owner) == []
    duplicate = await knowledge_router.upload_document(UploadFile(BytesIO(b'fixture document'), filename='same.txt'), owner)
    assert duplicate.headers['X-Knowledge-Job-Id'] == job_id
    await duplicate.body_iterator.aclose()
    assert len(list((root / owner).iterdir())) == 1
    with pytest.raises(BusinessError) as denied:
        await ingest.read_upload_job(factory, store.other.user_id, job_id)
    assert denied.value.http_status == 404

    vectors = VectorFixture()
    worker = KnowledgeIngestWorker(factory, root=root, vectors=vectors)
    assert await worker.tick()
    identifier, (text, metadata) = next(iter(vectors.records.items()))
    [visible] = await visibility.filter([{
        'source': 'knowledge', 'chunk_id': identifier, 'content': text, 'metadata': metadata,
    }], owner)
    assert visible['source_locator']['char_end'] == len(text)
    assert await visibility.filter([candidate], owner) == []
    view = await knowledge_router.get_upload_job(job_id, Response(), owner)
    assert view['data']['status'] == 'succeeded' and 'file_path' not in view['data']


@pytest.mark.asyncio
async def test_expired_attempt_cannot_publish_and_reprocessed_vectors_keep_their_ids(knowledge):
    store, root, factory = knowledge
    owner = store.actor.user_id
    document_id, job_id = await ingest.submit_upload(factory, owner, 'source.txt', 'txt', b'text', None, root=root)
    vectors = VectorFixture()
    first = KnowledgeIngestWorker(factory, root=root, vectors=vectors)
    job = await first.claim()
    await vectors.upsert_document(['text'], [{'document_id': str(document_id), 'user_id': owner}], [f'{document_id}_0'],
                                  collection='rag')
    store.sql(jobs.update().values(lease_expires_at=datetime(2000, 1, 1)))
    with pytest.raises(LeaseLost):
        await first._publish(job, {'chunks': [{}]}, ['text'])
    second = KnowledgeIngestWorker(factory, root=root, vectors=vectors)
    assert await second.tick()
    assert list(vectors.records) == [f'{document_id}_0']
    assert (await ingest.read_upload_job(factory, owner, job_id))['status'] == 'succeeded'
    assert store.rows(jobs)[0]['attempts'] == 2


@pytest.mark.asyncio
async def test_versioned_excerpt_uses_authoritative_span_and_denies_stale_or_foreign_requests(knowledge):
    store, root, factory = knowledge
    owner = store.actor.user_id
    source = '  中文前言 😀\r\n\r\n## 章节\r\n正文\r\n'
    doc_id, _ = await ingest.submit_upload(factory, owner, 'source.md', 'md', source.encode('utf-8-sig'), None, root=root)
    vectors = VectorFixture()
    assert await KnowledgeIngestWorker(factory, root=root, vectors=vectors).tick()
    doc, = store.rows(KnowledgeDocument.__table__)
    version = doc['source_manifest']['resource_version_id']
    identifier = f'{doc_id}_1'
    text, metadata = vectors.records[identifier]
    candidate = {'source': 'knowledge', 'chunk_id': identifier, 'content': text, 'metadata': metadata,
                 'source_locator': {'char_start': -1}}
    checker = CandidateVisibilityService(factory)
    [visible] = await checker.filter([candidate], owner)
    locator = visible['source_locator']
    assert locator['section_title'] == '## 章节'
    assert source[locator['char_start']:locator['char_end']] == text
    assert await checker.filter([{**candidate, 'content': 'changed'}, {**candidate, 'chunk_id': 'forged'},
                                 {**candidate, 'metadata': {**metadata, 'source_sha256': '0' * 64}}], owner) == []
    assert await checker.filter([candidate], store.other.user_id) == []

    async def excerpt(principal=owner, digest=version, ordinal=1):
        response = Response()
        async with factory() as db:
            data = await knowledge_router.get_document_chunk(doc_id, ordinal, response, digest, principal, db)
        assert response.headers['Cache-Control'] == 'private, no-store'
        return data['data']

    assert await excerpt() == {'source_locator': locator, 'content': text}
    rebuilt = await read_chunks(factory, Source('rag', str(doc_id), {**doc, 'source_sha256': version}), root)
    assert rebuilt[1][0] == text and rebuilt[1][1]['char_start'] == locator['char_start']
    for options in ({'principal': store.other.user_id}, {'digest': 'f' * 64}, {'ordinal': 5}):
        with pytest.raises(BusinessError) as denied:
            await excerpt(**options)
        assert denied.value.http_status == 404
    Path(doc['file_path']).write_text('changed after upload', encoding='utf-8')
    with pytest.raises(BusinessError) as denied:
        await excerpt()
    assert denied.value.http_status == 404
    await ingest.submit_deletion(factory, owner, doc_id, None)
    assert await checker.filter([candidate], owner) == []


@pytest.mark.asyncio
async def test_legacy_rebuild_checks_original_bytes_and_reuses_document_id(knowledge):
    store, root, factory = knowledge
    owner = store.actor.user_id
    doc_id, job_id = await ingest.submit_upload(factory, owner, 'old.txt', 'txt', b'legacy body', None, root=root)
    documents = KnowledgeDocument.__table__
    store.sql(jobs.delete().where(jobs.c.id == job_id))
    store.sql(documents.update().where(documents.c.id == doc_id).values(ready_for_retrieval=True, chunk_count=1))
    doc, = store.rows(documents)
    path = Path(doc['file_path'])
    path.write_bytes(b'wrong source')
    with pytest.raises(ValueError):
        await ingest.queue_source_manifest(factory, owner, doc_id, root=root)
    assert store.rows(documents)[0]['ready_for_retrieval'] and not store.rows(jobs)
    path.write_bytes(b'legacy body')
    rebuilt = await ingest.queue_source_manifest(factory, owner, doc_id, root=root)
    assert rebuilt and not store.rows(documents)[0]['ready_for_retrieval']
    assert await ingest.queue_source_manifest(factory, owner, doc_id, root=root) is None
    assert await KnowledgeIngestWorker(factory, root=root, vectors=VectorFixture()).tick()
    final, = store.rows(documents)
    assert final['id'] == doc_id and final['ready_for_retrieval'] and final['source_manifest']['chunks']
    [hit] = await KeywordSearch(factory).search_documents('legacy body', owner, top_k=5)
    assert hit['chunk_id'] == f'{doc_id}_0'


@pytest.mark.asyncio
async def test_backfill_keyword_chunks_uses_published_spans_and_preserves_vectors(knowledge):
    store, root, factory = knowledge
    owner = store.actor.user_id
    doc_id, _ = await ingest.submit_upload(factory, owner, 'existing.txt', 'txt', b'ZX-42 source', None, root=root)
    vectors = VectorFixture()
    assert await KnowledgeIngestWorker(factory, root=root, vectors=vectors).tick()
    original_vectors = vectors.records.copy()
    store.sql(knowledge_search_chunks.delete())
    search = KeywordSearch(factory)
    assert not await search.search_documents('ZX-42', owner, top_k=5)
    with pytest.raises(BusinessError):
        await backfill_keyword_index(factory, store.other.user_id, doc_id, root=root)
    assert await backfill_keyword_index(factory, owner, doc_id, root=root) == 1
    assert await backfill_keyword_index(factory, owner, doc_id, root=root) == 1
    assert vectors.records == original_vectors
    [hit] = await search.search_documents('ZX-42', owner, top_k=5)
    assert hit['content'] == 'ZX-42 source'


@pytest.mark.asyncio
async def test_keyword_projection_failure_rolls_back_document_publication(knowledge, monkeypatch):
    from app.rag import ingest_worker

    store, root, factory = knowledge
    owner = store.actor.user_id
    doc_id, job_id = await ingest.submit_upload(factory, owner, 'source.txt', 'txt', b'publish only once', None, root=root)
    replace = ingest_worker.replace_chunks

    async def fail_after_write(*args):
        await replace(*args)
        raise RuntimeError('failed after keyword staging')

    monkeypatch.setattr(ingest_worker, 'replace_chunks', fail_after_write)
    vectors = VectorFixture()
    worker = KnowledgeIngestWorker(factory, root=root, vectors=vectors)
    assert await worker.tick() and vectors.records
    assert (await ingest.read_upload_job(factory, owner, job_id))['status'] == 'queued'
    assert not store.rows(knowledge_search_chunks)
    assert not store.rows(KnowledgeDocument.__table__)[0]['ready_for_retrieval']
    monkeypatch.setattr(ingest_worker, 'replace_chunks', replace)
    store.sql(jobs.update().values(next_attempt_at=datetime(2000, 1, 1)))
    assert await worker.tick()
    assert (await ingest.read_upload_job(factory, owner, job_id))['status'] == 'succeeded'
    [chunk] = store.rows(knowledge_search_chunks)
    assert chunk['document_id'] == doc_id and chunk['content'] == 'publish only once'


@pytest.mark.asyncio
async def test_index_failure_retries_without_publishing_partial_batches(knowledge):
    store, root, factory = knowledge
    owner = store.actor.user_id
    document_id, job_id = await ingest.submit_upload(factory, owner, 'source.txt', 'txt', b'text', None, root=root)

    class InterruptedVectors(VectorFixture):
        calls = 0

        async def upsert_document(self, *args, **kwargs):
            await super().upsert_document(*args, **kwargs)
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError('fixture response lost after vector upsert')

    vectors = InterruptedVectors()
    worker = KnowledgeIngestWorker(factory, root=root, vectors=vectors)
    assert await worker.tick() and vectors.records
    assert (await ingest.read_upload_job(factory, owner, job_id))['status'] == 'queued'
    assert await CandidateVisibilityService(factory).filter([as_candidate(document_id, owner)], owner) == []
    assert not await worker.tick()  # The retry waits for the persisted backoff.
    store.sql(jobs.update().values(next_attempt_at=datetime(2000, 1, 1)))
    assert await worker.tick()
    assert (await ingest.read_upload_job(factory, owner, job_id))['status'] == 'succeeded'
    assert list(vectors.records) == [f'{document_id}_0']


@pytest.mark.asyncio
async def test_owner_can_read_job_progress_without_an_active_model_release(knowledge, monkeypatch):
    store, root, factory = knowledge
    owner = store.actor.user_id
    _, job_id = await ingest.submit_upload(factory, owner, 'source.txt', 'txt', b'text', None, root=root)
    monkeypatch.setenv('DB_CONFIG_REQUIRED', 'true')
    app = FastAPI()
    app.include_router(knowledge_router.router, prefix='/api/v1')
    app.dependency_overrides[knowledge_router.get_current_user_id] = lambda: owner
    app.add_middleware(SnapshotMiddleware, session_factory=factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        response = await client.get(f'/api/v1/knowledge/upload-jobs/{job_id}')
        listing = await client.get('/api/v1/knowledge/upload-jobs')
    assert response.status_code == 200 and response.json()['data']['status'] == 'queued'
    assert response.headers['cache-control'] == 'private, no-store'
    assert listing.status_code == 200 and listing.headers['cache-control'] == 'private, no-store'
    assert [row['job_id'] for row in listing.json()['data']['jobs']] == [job_id]


@pytest.mark.asyncio
async def test_list_resumes_own_active_jobs_and_reports_recent_failures(knowledge):
    store, root, factory = knowledge
    owner = store.actor.user_id
    _, first_id = await ingest.submit_upload(factory, owner, 'active.txt', 'txt', b'live', None, root=root)
    denied, rejected = await ingest.submit_upload(factory, owner, 'bad.pdf', 'pdf', b'broken', None, root=root)
    _, other = await ingest.submit_upload(factory, store.other.user_id, 'foreign.txt', 'txt', b'live', None, root=root)
    _, next_id = await ingest.submit_upload(factory, owner, 'next.txt', 'txt', b'next', None, root=root)
    _, done = await ingest.submit_upload(factory, owner, 'done.txt', 'txt', b'done', None, root=root)
    store.sql(jobs.update().where(jobs.c.id == rejected).values(status='failed', error_code='PDF_PARSE_FAILED', stage='parsing'))
    store.sql(jobs.update().where(jobs.c.id == done).values(status='succeeded', stage='complete'))
    first = await ingest.list_upload_jobs(factory, owner, limit=1)
    assert first['next_cursor'] and len(first['jobs']) == 2
    second = await ingest.list_upload_jobs(factory, owner, limit=1, after=first['next_cursor'])
    assert second['next_cursor'] is None and len(second['jobs']) == 1
    seen = {job['job_id']: job for job in first['jobs'] + second['jobs']}
    assert set(seen) == {first_id, next_id, rejected}
    failed = seen[rejected]
    assert failed['document_id'] == denied and failed['stage'] == 'parsing' and failed['message'].startswith('PDF')
    assert all('file_path' not in job and 'lease_token' not in job for job in seen.values())
    assert [job['job_id'] for job in (await ingest.list_upload_jobs(factory, store.other.user_id))['jobs']] == [other]


@pytest.mark.asyncio
async def test_delete_hides_document_even_if_vector_write_finishes_late(knowledge):
    store, root, factory = knowledge
    owner = store.actor.user_id
    document_id, job_id = await ingest.submit_upload(factory, owner, 'source.txt', 'txt', b'text', None, root=root)
    started, release = asyncio.Event(), asyncio.Event()

    class DelayedVectors(VectorFixture):
        async def upsert_document(self, *args, **kwargs):
            started.set()
            await release.wait()
            await super().upsert_document(*args, **kwargs)

    vectors = DelayedVectors()
    worker = KnowledgeIngestWorker(factory, root=root, vectors=vectors)
    write = asyncio.create_task(worker.tick())
    try:
        await asyncio.wait_for(started.wait(), 3)
        await ingest.submit_deletion(factory, owner, document_id, None)
        assert (await ingest.read_upload_job(factory, owner, job_id))['status'] == 'cancelled'
        release.set()
        await asyncio.wait_for(write, 3)
    finally:
        if not write.done():
            write.cancel()
            await asyncio.gather(write, return_exceptions=True)
    assert not vectors.records
    assert await CandidateVisibilityService(factory).filter([as_candidate(document_id, owner)], owner) == []
    source = Path(store.rows(jobs)[0]['file_path'])
    assert await worker.tick() and not vectors.records and not source.exists()
    new_id, _ = await ingest.submit_upload(factory, owner, 'source.txt', 'txt', b'text', None, root=root)
    assert new_id != document_id


@pytest.mark.asyncio
async def test_reclaimed_upload_prevents_old_attempt_from_writing_vectors(knowledge):
    store, root, factory = knowledge
    owner = store.actor.user_id
    _, job_id = await ingest.submit_upload(factory, owner, 'source.txt', 'txt', b'kept across retry', None, root=root)
    started, resume = asyncio.Event(), asyncio.Event()

    class DelayedFirstAttempt(VectorFixture):
        pause = True

        async def upsert_document(self, documents, *args, **kwargs):
            if self.pause:
                self.pause = False
                started.set()
                await resume.wait()
                documents = ['obsolete write']
            await super().upsert_document(documents, *args, **kwargs)

    vectors = DelayedFirstAttempt()
    first = KnowledgeIngestWorker(factory, root=root, vectors=vectors)
    attempt = await first.claim()
    pending = asyncio.create_task(first._execute(attempt))
    try:
        await asyncio.wait_for(started.wait(), 3)
        store.sql(jobs.update().where(jobs.c.id == job_id).values(lease_expires_at=datetime(2000, 1, 1)))
        assert await KnowledgeIngestWorker(factory, root=root, vectors=vectors).tick()
        resume.set()
        with pytest.raises(LeaseLost):
            await pending
        assert [row[0] for row in vectors.records.values()] == ['kept across retry']
        assert (await ingest.read_upload_job(factory, owner, job_id))['attempts'] == 2
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('text_pages', [True, False])
async def test_pdf_uses_real_page_numbers_and_rejects_pages_without_text(knowledge, text_pages):
    import fitz

    store, root, factory = knowledge
    owner = store.actor.user_id
    with fitz.open() as pdf:
        for number in (1, 2):
            page = pdf.new_page()
            if text_pages:
                page.insert_text((72, 72), f'Page {number}')
        payload = pdf.tobytes()
    _, job_id = await ingest.submit_upload(factory, owner, 'source.pdf', 'pdf', payload, None, root=root)
    vectors = VectorFixture()
    assert await KnowledgeIngestWorker(factory, root=root, vectors=vectors).tick()
    status = await ingest.read_upload_job(factory, owner, job_id)
    if text_pages:
        assert status['status'] == 'succeeded'
        assert [metadata['page_number'] for _, metadata in vectors.records.values()] == [1, 2]
    else:
        assert status['status'] == 'failed' and status['error_code'] == 'PDF_TEXT_REQUIRED' and not vectors.records


@pytest.mark.asyncio
async def test_worker_publishes_and_cleans_real_chroma_under_a_pinned_release(knowledge, vectors):
    store, root, factory = knowledge
    owner = store.actor.user_id
    release_id = vectors.snapshot.release_id
    document_id, job_id = await ingest.submit_upload(factory, owner, 'source.txt', 'txt', b'worker fixture document',
                                                     release_id, root=root)
    worker = KnowledgeIngestWorker(factory, root=root, vectors=vectors.service)
    assert await worker.tick()
    assert (await ingest.read_upload_job(factory, owner, job_id))['status'] == 'succeeded'
    with selected(vectors.admin, vectors.snapshot):
        matches = await vectors.service.search_documents('fixture', owner)
    assert any(row['chunk_id'] == f'{document_id}_0' and row['content'] == 'worker fixture document' for row in matches)

    await ingest.submit_deletion(factory, owner, document_id, release_id)
    assert await worker.tick()
    with selected(vectors.admin, vectors.snapshot):
        matches = await vectors.service.search_documents('fixture', owner)
    assert all(row['chunk_id'] != f'{document_id}_0' for row in matches)
