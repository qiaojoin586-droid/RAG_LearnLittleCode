import asyncio
from contextvars import ContextVar
from hashlib import sha256
import json
import math
import sys
from threading import Event
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fitz
import pytest

from app.core.failed_response import BusinessError
from app.models.note import Note
from app.rag import chroma_transport, ingest, ingest_worker, rag_service
from app.rag.blocking_io import BlockingPool
from app.rag.rag_service import RagService, RetrievalUnavailable
from app.routers import chat
from app.schemas.chat import RAGRequest
from test_hybrid_retrieval import BASELINE, OWNER, hybrid, vector_result
from test_knowledge_ingest import knowledge, VectorFixture
from test_model_config_control_plane import store
from test_note_index_worker import note_vectors


async def visible(rows, user):
    return rows


def backends(documents, notes):
    return SimpleNamespace(search_documents=documents, search_notes=notes)


@pytest.mark.asyncio
async def test_slow_sources_keep_fast_keyword_and_cancel_unfinished_calls(monkeypatch):
    monkeypatch.setattr(rag_service, 'RECALL_SECONDS', 0.07)
    cancelled = []
    async def stall(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    hit = dict(vector_result(1, 'evidence'), algorithm='sql_literal')
    service = RagService(backends(stall, stall), keyword_search=backends(AsyncMock(return_value=[hit]), stall),
                         enable_keyword=True, visibility_checker=visible)
    result = await asyncio.wait_for(service.query('q', OWNER, use_hyde=False), 1)
    assert result['status'] == 'partial' and result['failed_sources'] == ['knowledge', 'notes', 'keyword_notes']
    assert json.loads(result['context']) == [{'text': 'evidence'}] and len(cancelled) == 3


@pytest.mark.asyncio
async def test_optional_stages_preserve_completed_evidence_and_stop_by_parent(monkeypatch):
    for name in ('HYDE_SECONDS', 'RERANK_SECONDS', 'SUMMARY_SECONDS'):
        monkeypatch.setattr(rag_service, name, 0.05)
    release = Event()
    async def never(*args):
        await asyncio.Event().wait()
    def predict(pairs):
        release.wait(2)
        return [1] * len(pairs)
    async def summary(query, body):
        if body == 'first':
            return 'ready summary'
        await never()
    vectors = backends(AsyncMock(return_value=[vector_result(1, 'first'), vector_result(2, 'second')]),
                       AsyncMock(return_value=[]))
    service = RagService(vectors, chat_model=object(), rerank_model=SimpleNamespace(predict=predict),
                         enable_keyword=False, enable_summarize=True, visibility_checker=visible)
    monkeypatch.setattr(service, '_generate_hyde', never)
    monkeypatch.setattr(service, '_summarize_single', summary)
    try:
        result = await asyncio.wait_for(service.query('original', OWNER, use_hyde=True), 1)
        assert result['status'] == 'degraded' and result['rerank_status'] == 'timeout'
        assert result['degraded_stages'] == ['hyde', 'rerank', 'summarize']
        assert [b['text'] for b in json.loads(result['context'])] == ['ready summary', 'second']
        vectors.search_documents.assert_awaited_once_with('original', OWNER, top_k=6)
        # No stale authorization may substitute for an unfinished final read.
        checked = 0
        async def recheck(rows, owner):
            nonlocal checked
            checked += 1
            if checked > 1:
                await never()
            return rows
        service.visibility_checker = recheck
        service.rerank_model = None
        service.enable_summarize = False
        with pytest.raises(RetrievalUnavailable, match='deadline'):
            await service.query('q', OWNER, use_hyde=False, deadline=asyncio.get_running_loop().time()+0.08)
        assert checked == 2
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize('http', [True, False])
async def test_vector_write_guard_outlives_cancellation_and_keeps_loop_responsive(monkeypatch, http):
    monkeypatch.setattr(chroma_transport, 'route_port', lambda directory: 8001 if http else None)
    loop = asyncio.get_running_loop()
    started, finish, written = asyncio.Event(), Event(), Event()
    principal = ContextVar('test_principal', default=None)
    principal.set(OWNER)
    def write():
        assert principal.get() == OWNER
        loop.call_soon_threadsafe(started.set)
        assert finish.wait(2)
        written.set()
    task = asyncio.create_task(chroma_transport.vector_io('.', write, write=True))
    try:
        await asyncio.wait_for(started.wait(), 1)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done() and not written.is_set()
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert written.is_set()


@pytest.mark.asyncio
async def test_cancelled_read_retains_physical_worker_capacity_and_discards_queued_call():
    pool = BlockingPool('test-retrieval', workers=1, queued=1)
    entered, finish = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    def blocking():
        loop.call_soon_threadsafe(entered.set)
        assert finish.wait(2)
    first = asyncio.create_task(pool.run(blocking))
    called = Event()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(pool.run(called.set))
        await asyncio.sleep(0)
        first.cancel()
        second.cancel()
        for task in (first, second):
            with pytest.raises(asyncio.CancelledError):
                await task
        with pytest.raises(BlockingIOError):
            await pool.run(called.set)
    finally:
        finish.set()
        await asyncio.to_thread(pool.executor.shutdown)
    assert not called.is_set()


@pytest.mark.asyncio
async def test_ingest_pdf_parser_allows_concurrent_loop_progress(knowledge, monkeypatch):
    db_store, root, factory = knowledge
    real = ingest_worker._read_pages
    entered, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    def parse(*args):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return real(*args)
    monkeypatch.setattr(ingest_worker, '_read_pages', parse)
    with fitz.open() as pdf:
        pdf.new_page().insert_text((72, 72), 'Retrieval budget.')
        data = pdf.tobytes()
    owner = db_store.actor.user_id
    await ingest.submit_upload(factory, owner, 'latency.pdf', 'pdf', data, None, root=root)
    vectors = VectorFixture()
    task = asyncio.create_task(ingest_worker.KnowledgeIngestWorker(factory, root=root, vectors=vectors).tick())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not task.done()
    finally:
        release.set()
    assert await task and list(vectors.records.values())[0][0] == 'Retrieval budget.'


@pytest.mark.asyncio
async def test_direct_endpoint_reports_unavailable_and_chat_ack_precedes_preparation(monkeypatch):
    offline = AsyncMock(side_effect=RuntimeError('offline'))
    service = RagService(backends(offline, offline), enable_keyword=False, visibility_checker=visible)
    monkeypatch.setitem(sys.modules, 'main', SimpleNamespace(init_manager=SimpleNamespace(rag_service=service)))
    with pytest.raises(BusinessError) as failed:
        await chat.rag_query(RAGRequest(query='q', use_hyde=False), OWNER)
    assert failed.value.http_status == 503 and failed.value.data['status'] == 'error'
    assert failed.value.data['failed_sources'] == ['knowledge','notes']
    assert {'vector_knowledge', 'vector_notes', 'total'} <= failed.value.data['timings_ms'].keys()
    prepare = AsyncMock(side_effect=RuntimeError('local failure'))
    monkeypatch.setattr(chat, '_prepare_chat_stream', prepare)
    async with chat.aclosing(chat._accepted_chat_frames(None, OWNER, 'session', 1, [])) as frames:
        assert '"accepted"' in await anext(frames)
        prepare.assert_not_awaited()
        assert '"error"' in await anext(frames)


@pytest.mark.asyncio
async def test_cancelling_request_stops_all_lanes_and_does_not_produce_context():
    started = 0
    stopped = 0
    ready = asyncio.Event()
    async def pending(*args, **kwargs):
        nonlocal started, stopped
        started += 1
        if started == 4:
            ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped += 1
    backend = backends(pending, pending)
    check = AsyncMock()
    service = RagService(backend, keyword_search=backend, enable_keyword=True, visibility_checker=check)
    task = asyncio.create_task(service.query('q', OWNER, use_hyde=False))
    await asyncio.wait_for(ready.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped == 4 and not check.await_count


@pytest.mark.asyncio
async def test_frozen_local_hybrid_load(hybrid, note_vectors, request):
    config = json.loads((BASELINE.parent / 'rag_m3_09_load.json').read_text(encoding='utf-8'))
    fixture = json.loads((BASELINE.parent / config['corpus_and_queries']).read_text(encoding='utf-8'))
    canonical = json.dumps(fixture, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    assert sha256(canonical.encode()).hexdigest() == config['corpus_sha256']
    engine, factory, seed = hybrid
    corpus, questions = fixture['corpus'], fixture['questions']
    def embedding(order):
        return [float(len(order)-order.index(i)) if i in order else 0.0 for i in corpus]

    for identifier, text in corpus.items():
        seed(identifier, text)
        row = vector_result(identifier, text)
        note_vectors._rag_collection.upsert(ids=[row['chunk_id']], documents=[text],
                                             embeddings=[embedding([identifier])], metadatas=[row['metadata']])
    with engine.begin() as conn:
        for i in ('11','12'):
            ident, body = 'n'+i, corpus[i]
            conn.execute(Note.__table__.insert().values(id=ident, user_id=OWNER, title=ident, content=body))
            note_vectors._notes_collection.upsert(ids=['note_'+ident], documents=[body], embeddings=[embedding([i])],
                                                  metadatas=[{'note_id':ident, 'user_id':OWNER}])
    query_vectors = {q['query']: embedding(q['vector_ranking']) for q in questions}
    note_vectors._generate_embeddings.side_effect = lambda inputs: [query_vectors[q] for q in inputs]
    service = RagService(note_vectors, enable_keyword=True, db_session_factory=factory)
    async def one(i):
        question = questions[i % len(questions)]['query']
        start = perf_counter()
        result = await service.query(question, OWNER, use_hyde=False, top_k=config['top_k'])
        ms = 1000*(perf_counter()-start)
        ok = (result['status']=='success' and bool(result['context']) and result['recall_methods']==['vector','keyword']
              and all(hit['source_locator'] and hit['metadata']['user_id']==OWNER for hit in result['sources']))
        return ms, ok
    async def measure(count):
        samples = []
        for i in range(0, count, config['concurrency']):
            samples.extend(await asyncio.gather(*(one(j) for j in range(i,min(count,i+config['concurrency']))),
                                                return_exceptions=True))
        return samples
    warm = await measure(config['warmup_requests'])
    assert all(isinstance(s, tuple) and s[1] for s in warm)
    samples = await measure(config['measured_requests'])
    errors = sum(not isinstance(s, tuple) or not s[1] for s in samples)
    latencies = sorted(s[0] for s in samples if isinstance(s, tuple))
    assert len(samples) == config['measured_requests'] and latencies
    p95 = latencies[math.ceil(len(latencies)*0.95)-1]
    request.node.user_properties.append(('frozen_load', json.dumps(dict(requests=len(samples), errors=errors,
        p95_ms=round(p95,3), p50_ms=round(latencies[math.ceil(len(latencies)/2)-1],3),
        latency_ms=[round(ms,3) for ms in latencies]))))
    assert errors <= config['allowed_errors'] and p95 <= config['max_p95_ms']
    assert note_vectors._generate_embeddings.await_count == config['warmup_requests'] + len(samples)
