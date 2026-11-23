"""M3-13 deadlines, queued calls, and preservation of completed retrieval evidence."""
import asyncio
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.failed_response import BusinessError
from app.model_config.task_budget import ModelBudgetDenied
from app.rag import rag_service
from app.rag.query_embeddings import query_embedding
from app.rag.rag_service import RagService, RetrievalUnavailable
from app.rag.read_tasks import ReadTasks
from app.routers import chat
from app.schemas.chat import RAGRequest
from test_hybrid_retrieval import OWNER, hybrid, vector_result
from test_query_expansion import QUESTION, REWRITES, reply


def sources(documents, notes):
    return SimpleNamespace(search_documents=documents, search_notes=notes)


@pytest.mark.asyncio
async def test_parent_budget_skips_expansion_before_dispatch_and_keeps_base(hybrid):
    _, factory, seed = hybrid
    seed(1, QUESTION)
    search = AsyncMock(return_value=[])
    generate = AsyncMock(return_value=reply(*REWRITES))
    service = RagService(sources(search, search), enable_keyword=True, db_session_factory=factory,
                         expansion_generator=generate)
    now = asyncio.get_running_loop().time()
    assert now + 4.99 <= rag_service.retrieval_deadline(now + 60) <= now + 5.01
    assert rag_service.RERANK_SECONDS == 1.5 and rag_service.GENERATION_SECONDS == 1.0
    result = await service.query(QUESTION, OWNER, deadline=now + .6)
    assert result['status'] == 'success' and result['sources'][0]['chunk_id'] == '1_0'
    assert result['query_expansion']['reason'] == 'insufficient_budget'
    generate.assert_not_awaited()
    with pytest.raises(RetrievalUnavailable, match='deadline'):
        await service.query(QUESTION, OWNER, deadline=now)
    assert search.await_count == 2


@pytest.mark.asyncio
async def test_short_parent_does_not_dispatch_hyde(hybrid, monkeypatch):
    _, factory, seed = hybrid
    seed(1, QUESTION)
    empty = AsyncMock(return_value=[])
    service = RagService(sources(empty, empty), enable_keyword=True, chat_model=object(), db_session_factory=factory)
    generate = AsyncMock()
    monkeypatch.setattr(service, '_generate_hyde', generate)
    result = await service.query(QUESTION, OWNER, use_hyde=True, deadline=asyncio.get_running_loop().time() + .6)
    generate.assert_not_awaited()
    assert result['degraded_stages'] == ['hyde'] and result['sources'][0]['chunk_id'] == '1_0'


@pytest.mark.asyncio
async def test_authorization_past_parent_deadline_returns_no_evidence_while_database_closes(hybrid, monkeypatch):
    _, factory, seed = hybrid
    seed(1, QUESTION)
    empty = AsyncMock(return_value=[])
    closing, release, finished = (asyncio.Event() for _ in range(3))
    service = RagService(sources(empty, empty), enable_keyword=True, db_session_factory=factory)
    async def recheck(rows, owner, degraded):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            closing.set()
            await release.wait()
            return rows
        finally:
            finished.set()
    monkeypatch.setattr(service, '_recheck', recheck)
    try:
        with pytest.raises(RetrievalUnavailable, match='deadline') as failed:
            await asyncio.wait_for(service.query(QUESTION, OWNER, deadline=asyncio.get_running_loop().time() + .2), 1)
        await asyncio.wait_for(closing.wait(), 1)
        assert not finished.is_set()
        assert 'keyword_knowledge' in failed.value.timings_ms and 'total' in failed.value.timings_ms
    finally:
        release.set()
    await asyncio.wait_for(finished.wait(), 1)


@pytest.mark.asyncio
async def test_queued_expanded_reads_expire_without_starting_and_late_results_are_ignored(monkeypatch, hybrid):
    _, factory, seed = hybrid
    seed(1, 'timely')
    seed(2, 'late')
    monkeypatch.setattr(rag_service, 'RECALL_SECONDS', .24)
    monkeypatch.setattr(rag_service, 'GENERATION_SECONDS', .08)
    entered, rewritten, release, drained = (asyncio.Event() for _ in range(4))
    started = []
    active = peak = 0

    def branch(name):
        async def search(query, owner, top_k):
            nonlocal active, peak
            started.append((name, query))
            active += 1
            peak = max(peak, active)
            if active == 4:
                entered.set()
            try:
                if name == 'keyword_docs':
                    await rewritten.wait()
                    return [dict(vector_result(1, 'timely'), algorithm='sql_literal')]
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                return [vector_result(2, 'late')]
            finally:
                active -= 1
                if not active:
                    drained.set()
        return search

    async def generate(query, *, max_output_tokens):
        await entered.wait()
        rewritten.set()
        return reply(*REWRITES)

    service = RagService(sources(branch('vector_docs'), branch('vector_notes')), enable_keyword=True,
                         keyword_search=sources(branch('keyword_docs'), branch('keyword_notes')),
                         expansion_generator=generate, db_session_factory=factory)
    try:
        result = await asyncio.wait_for(service.query(QUESTION, OWNER), 1)
        assert peak == active == 4
        assert len(started) == 5 and len([q for _, q in started if q == QUESTION]) == 4
        assert result['status'] == 'partial' and [s['chunk_id'] for s in result['sources']] == ['1_0']
        assert len(result['failed_sources']) == 7
        assert result['query_expansion']['reason'] == 'expanded_sources_unavailable'
        assert 'expanded_2_notes' in result['timings_ms'] and 'late' not in result['context']
    finally:
        release.set()
    await asyncio.wait_for(drained.wait(), 1)
    assert len(started) == 5 and 'late' not in result['context']


@pytest.mark.asyncio
async def test_embedding_cleanup_cannot_hold_both_vector_lanes_or_fast_sql(monkeypatch, hybrid):
    _, factory, seed = hybrid
    seed(1, QUESTION)
    monkeypatch.setattr(rag_service, 'RECALL_SECONDS', .08)
    closing, release, finished = (asyncio.Event() for _ in range(3))

    async def embed():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            closing.set()
            await release.wait()
            return [0.1]
        finally:
            finished.set()

    generate, vector_query = AsyncMock(side_effect=embed), AsyncMock(return_value=[])
    async def search(query, owner, top_k):
        return await vector_query(await query_embedding(query, generate))

    service = RagService(sources(search, search), enable_keyword=True, db_session_factory=factory)
    try:
        result = await asyncio.wait_for(service.query(QUESTION, OWNER), 1)
        await asyncio.wait_for(closing.wait(), 1)
        assert not finished.is_set() and result['status'] == 'partial'
        assert result['failed_sources'] == ['knowledge', 'notes'] and result['sources'][0]['chunk_id'] == '1_0'
    finally:
        release.set()
    await asyncio.wait_for(finished.wait(), 1)
    generate.assert_awaited_once()
    vector_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_closing_reads_retain_capacity_until_they_really_finish():
    pool = ReadTasks(1)
    release, closed = asyncio.Event(), asyncio.Event()

    async def slow():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
        finally:
            closed.set()

    loop = asyncio.get_running_loop()
    read = AsyncMock(return_value='ok')
    try:
        with pytest.raises(TimeoutError):
            await pool.until(loop.time() + .06, slow)
        with pytest.raises(BlockingIOError):
            await pool.until(loop.time() + 1, read)
        read.assert_not_awaited()
    finally:
        release.set()
    await asyncio.wait_for(closed.wait(), 1)
    assert await pool.until(loop.time() + 1, read) == 'ok'
    queued = pool.start(read)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert await pool.until(loop.time() + 1, read) == 'ok'
    assert read.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['empty', 'partial_empty', 'partial_hit', 'error', 'expanded_only'])
async def test_direct_endpoint_distinguishes_partial_empty_unavailable_and_surviving_family(monkeypatch, hybrid, outcome):
    _, factory, seed = hybrid
    seed(1, 'current')

    async def documents(query, owner, top_k):
        if outcome == 'partial_hit' or outcome == 'expanded_only' and query in REWRITES:
            return [vector_result(1, 'current')]
        if outcome in ('expanded_only', 'error'):
            raise ConnectionError
        return []

    empty = AsyncMock(return_value=[])
    fail = AsyncMock(side_effect=ConnectionError)
    optional = AsyncMock(return_value=reply(*REWRITES)) if outcome == 'expanded_only' else None
    service = RagService(sources(documents, empty if outcome == 'empty' else fail),
                         keyword_search=sources(fail, fail) if outcome in ('error', 'expanded_only') else sources(empty, empty),
                         enable_keyword=True, expansion_generator=optional, db_session_factory=factory)
    monkeypatch.setitem(sys.modules, 'main', SimpleNamespace(init_manager=SimpleNamespace(rag_service=service)))
    if outcome == 'error':
        with pytest.raises(BusinessError) as failure:
            await chat.rag_query(RAGRequest(query=QUESTION), OWNER)
        assert failure.value.http_status == 503 and failure.value.data['status'] == 'error'
        assert len(failure.value.data['failed_sources']) == 4
        assert 'answer' not in failure.value.data and failure.value.data['timings_ms']['total'] >= 0
        return
    data = (await chat.rag_query(RAGRequest(query=QUESTION), OWNER))['data']
    assert data['status'] == ('empty' if outcome == 'empty' else 'partial')
    assert bool(data['failed_sources']) == (outcome != 'empty')
    assert bool(data['answer']) == (outcome in ('partial_hit', 'expanded_only'))
    if outcome == 'expanded_only':
        assert {p['method'] for p in data['sources'][0]['provenance']} == {'expanded'}


@pytest.mark.asyncio
async def test_rerank_crossing_cutoff_preserves_rrf_order_and_scores(monkeypatch, hybrid):
    _, factory, seed = hybrid
    for i in (1, 2):
        seed(i, str(i))
    documents = AsyncMock(return_value=[vector_result(i, str(i)) for i in (1, 2)])
    service = RagService(sources(documents, AsyncMock(return_value=[])), enable_keyword=False, db_session_factory=factory)
    baseline = await service.query(QUESTION, OWNER)
    # Simulate synchronous score decoding completing across the stage deadline.
    def decode():
        time.sleep(.09)
        return [0., 1.]
    service.rerank_model = SimpleNamespace(predict=lambda _: SimpleNamespace(tolist=decode))
    monkeypatch.setattr(rag_service, 'RERANK_SECONDS', .05)
    result = await service.query(QUESTION, OWNER)
    assert result['status'] == 'degraded' and result['rerank_status'] == 'timeout'
    assert result['sources'] == baseline['sources'] and result['context'] == baseline['context']


@pytest.mark.asyncio
async def test_summary_cannot_hide_parent_model_budget_denial(hybrid):
    _, factory, seed = hybrid
    seed(1, QUESTION)
    empty = AsyncMock(return_value=[])
    model = SimpleNamespace(ainvoke=AsyncMock(side_effect=ModelBudgetDenied('LIMIT')))
    service = RagService(sources(empty, empty), enable_keyword=True, chat_model=model,
                         enable_summarize=True, db_session_factory=factory)
    with pytest.raises(ModelBudgetDenied):
        await service.query(QUESTION, OWNER)
    model.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_generator_cancelled_by_provider_keeps_base_but_caller_cancellation_propagates(hybrid):
    _, factory, seed = hybrid
    seed(1, QUESTION)
    empty = AsyncMock(return_value=[])
    service = RagService(sources(empty, empty), enable_keyword=True, db_session_factory=factory,
                         expansion_generator=AsyncMock(side_effect=asyncio.CancelledError))
    result = await service.query(QUESTION, OWNER)
    assert result['status'] == 'degraded' and result['query_expansion']['reason'] == 'generator_unavailable'
    assert result['sources'][0]['chunk_id'] == '1_0'
    running = asyncio.Event()
    async def generate(query, *, max_output_tokens):
        running.set()
        await asyncio.Event().wait()
    service.expansion_generator = generate
    request = asyncio.create_task(service.query(QUESTION, OWNER))
    await asyncio.wait_for(running.wait(), 1)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
