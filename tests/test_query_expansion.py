"""Limited query expansion at the original retrieval entry points."""
import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.model_config.task_budget import ModelBudgetDenied
from app.rag import rag_service
from app.rag.query_expansion import InvalidExpansion, skip_reason, validated_queries
from app.rag.rag_service import RagService, RetrievalUnavailable
from app.routers import chat
from app.schemas.chat import RAGRequest
from test_hybrid_retrieval import BASELINE, OWNER, hybrid, vector_result
from test_note_index_worker import note_vectors


FIXTURE = json.loads((BASELINE.parent / 'rag_m3_10_queries.json').read_text(encoding='utf-8'))
QUESTION, REWRITES = FIXTURE['question'], FIXTURE['rewrites']


def reply(*queries):
    return json.dumps({'queries': queries}, ensure_ascii=False)


def backend(search):
    return SimpleNamespace(search_documents=search, search_notes=search)


@pytest.mark.parametrize('name,question,candidate,accepted', FIXTURE['constraints'],
                         ids=[row[0] for row in FIXTURE['constraints']])
def test_frozen_constraints(name, question, candidate, accepted):
    assert skip_reason(question) is None
    if accepted:
        assert validated_queries(question, reply(candidate)) == [candidate]
    else:
        with pytest.raises(InvalidExpansion, match='^constraint_changed$'):
            validated_queries(question, reply(candidate))


@pytest.mark.parametrize('output,reason', [
    ('not json', 'invalid_output'), ('{"queries":[],"queries":[]}', 'invalid_output'),
    ('{"queries":[],"filter":{}}', 'invalid_output'), ('{"queries":{}}', 'invalid_output'),
    (reply({'query': QUESTION}), 'invalid_output'), (reply('a' * 513), 'invalid_output'),
    (reply('https://example.invalid/'), 'invalid_output'), (reply('owner_id=any'), 'invalid_output'),
    (reply('<tool>action</tool>'), 'invalid_output'), (reply(REWRITES[0], '捏造的场景'), 'constraint_changed'),
    (reply(*REWRITES, REWRITES[0]), 'too_many_queries'), (reply(' ', QUESTION), 'no_distinct_query'),
    (reply(), 'no_distinct_query'), ('[]', 'invalid_output'),
])
def test_invalid_generation_is_rejected_as_a_unit(output, reason):
    with pytest.raises(InvalidExpansion, match=f'^{reason}$'):
        validated_queries(QUESTION, output)


def test_deduplicate_and_drop_blanks_before_search():
    assert validated_queries(QUESTION, reply(REWRITES[0], ' ' + REWRITES[0] + '? ')) == REWRITES[:1]
    assert validated_queries(QUESTION, reply('', REWRITES[1])) == REWRITES[1:]
    assert validated_queries(QUESTION, reply(QUESTION, REWRITES[0])) == REWRITES[:1]


@pytest.mark.asyncio
@pytest.mark.parametrize('question,reason', [
    ('QX-AND-42', 'no_adaptive_trigger'), ('查 QX-AND-42', 'no_adaptive_trigger'),
    ('查“优点和缺点”', 'no_adaptive_trigger'), ('冷却泵', 'no_adaptive_trigger'),
    (QUESTION + '\n以下是用户引用的笔记卡片：\n正文', 'input_ineligible'),
    (QUESTION + ' <referenced_notes>private</referenced_notes>', 'input_ineligible'),
    (QUESTION * 55, 'input_ineligible'),
])
async def test_ineligible_input_never_reaches_generator(question, reason):
    generator = AsyncMock()
    search = AsyncMock(return_value=[])
    service = RagService(backend(search), keyword_search=backend(search), enable_keyword=True,
                         expansion_generator=generator)
    result = await service.query(question, OWNER)
    generator.assert_not_awaited()
    assert search.await_count == 4
    assert result['query_expansion']['reason'] == reason and result['status'] == 'empty'


@pytest.mark.asyncio
async def test_independent_original_lanes_and_bounded_expansion(monkeypatch, hybrid):
    _, factory, seed = hybrid
    seed(1, 'original')
    calls, active, peak = [], 0, 0
    originals_started, generated, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    def source(method):
        async def search(text, owner, top_k):
            nonlocal active, peak
            assert owner == OWNER and top_k == 20
            calls.append((method, text))
            active += 1
            peak = max(peak, active)
            try:
                if len(calls) == 4:
                    originals_started.set()
                await release.wait()
                return [dict(vector_result(1, 'original'), algorithm='sql_literal')] if method.endswith('docs') else []
            finally:
                active -= 1
        return search

    async def generate(text, *, max_output_tokens):
        await originals_started.wait()
        generated.set()
        return reply(*REWRITES)

    generator = AsyncMock(side_effect=generate)
    vector = SimpleNamespace(search_documents=source('vector_docs'), search_notes=source('vector_notes'))
    keyword = SimpleNamespace(search_documents=source('keyword_docs'), search_notes=source('keyword_notes'))
    service = RagService(vector, keyword_search=keyword, enable_keyword=True, chat_model=object(),
                         db_session_factory=factory, expansion_generator=generator)
    hyde = AsyncMock()
    monkeypatch.setattr(service, '_generate_hyde', hyde)
    task = asyncio.create_task(service.query(QUESTION, OWNER, use_hyde=True))
    try:
        await asyncio.wait_for(generated.wait(), 1)
        assert len(calls) == 4 and all(text == QUESTION for _, text in calls)
    finally:
        release.set()
    result = await task
    assert peak == 4 and len(calls) == 8
    for text in REWRITES:
        assert sorted(method for method, q in calls if q == text) == ['vector_docs', 'vector_notes']
    generator.assert_awaited_once_with(QUESTION, max_output_tokens=384)
    hyde.assert_not_awaited()
    assert result['query_expansion']['query_count'] == 2
    assert result['recall_methods'] == ['vector', 'keyword', 'expanded']
    hit, = result['sources']
    assert hit['fusion_score'] == pytest.approx(3 / 61)
    assert [(p['method'], p.get('subquery_id')) for p in hit['provenance']] == [
        ('vector', None), ('keyword', None), ('expanded', 1), ('expanded', 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['no_distinct_query', 'constraint_changed', 'invalid_output',
                                    'generator_unavailable', 'timeout', 'late_reply'])
async def test_generation_faults_preserve_original_evidence(reason, monkeypatch, hybrid):
    _, factory, seed = hybrid
    seed(1, 'evidence')
    async def generate(*args, **kwargs):
        if reason == 'generator_unavailable':
            raise InvalidExpansion('sensitive provider error')
        if reason in ('timeout', 'late_reply'):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if reason == 'late_reply':
                    return reply(*REWRITES)
                raise
        return {'no_distinct_query': reply(QUESTION), 'constraint_changed': reply('changed'),
                'invalid_output': 'malformed'}[reason]
    monkeypatch.setattr(rag_service, 'GENERATION_SECONDS', .04)
    generator = AsyncMock(side_effect=generate)
    documents = AsyncMock(return_value=[vector_result(1, 'evidence')])
    service = RagService(SimpleNamespace(search_documents=documents, search_notes=AsyncMock(return_value=[])),
                         db_session_factory=factory, enable_keyword=True, expansion_generator=generator)
    result = await service.query(QUESTION, OWNER)
    assert result['status'] == 'degraded' and result['degraded_stages'] == ['query_expansion']
    assert result['query_expansion']['reason'] == ('timeout' if reason == 'late_reply' else reason)
    assert result['query_expansion']['query_count'] == 0
    assert json.loads(result['context']) == [{'id': 'E1', 'text': 'evidence'}]
    documents.assert_awaited_once_with(QUESTION, OWNER, top_k=20)
    generator.assert_awaited_once_with(QUESTION, max_output_tokens=384)


@pytest.mark.asyncio
async def test_expanded_source_failure_and_budget(hybrid):
    _, factory, seed = hybrid
    seed(1, 'original evidence')
    async def docs(query, owner, top_k):
        if query == QUESTION:
            return [vector_result(1, 'original evidence')]
        raise ConnectionError
    offline = AsyncMock(side_effect=ConnectionError)
    service = RagService(SimpleNamespace(search_documents=docs, search_notes=offline),
                         db_session_factory=factory, enable_keyword=True,
                         expansion_generator=AsyncMock(return_value=reply(REWRITES[0])))
    result = await service.query(QUESTION, OWNER)
    assert result['status'] == 'partial' and result['sources'][0]['chunk_id'] == '1_0'
    assert result['failed_sources'] == ['notes', 'expanded_1_knowledge', 'expanded_1_notes']
    assert result['query_expansion']['reason'] == 'expanded_sources_unavailable'
    service.vector_store = service.keyword_search = backend(offline)
    with pytest.raises(RetrievalUnavailable):
        await service.query(QUESTION, OWNER)
    service.expansion_generator = AsyncMock(side_effect=ModelBudgetDenied('LIMIT'))
    with pytest.raises(ModelBudgetDenied):
        await service.query(QUESTION, OWNER)


@pytest.mark.asyncio
async def test_slow_provider_shutdown_does_not_hold_base_response(monkeypatch, hybrid):
    _, factory, seed = hybrid
    seed(1, 'evidence')
    closing, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def generate(text, *, max_output_tokens):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            closing.set()
            await release.wait()
            finished.set()
            return reply(*REWRITES)
    documents = AsyncMock(return_value=[vector_result(1, 'evidence')])
    service = RagService(SimpleNamespace(search_documents=documents, search_notes=AsyncMock(return_value=[])),
                         enable_keyword=True, db_session_factory=factory, expansion_generator=generate)
    monkeypatch.setattr(rag_service, 'GENERATION_SECONDS', .04)
    try:
        result = await asyncio.wait_for(service.query(QUESTION, OWNER), 1)
        assert result['query_expansion']['reason'] == 'timeout' and result['sources'][0]['chunk_id'] == '1_0'
        await asyncio.wait_for(closing.wait(), 1)
        assert not finished.is_set()
    finally:
        release.set()
    await asyncio.wait_for(finished.wait(), 1)
    documents.assert_awaited_once_with(QUESTION, OWNER, top_k=20)


@pytest.mark.asyncio
async def test_default_direct_request_cannot_enable_expansion(monkeypatch):
    clean = AsyncMock(return_value=[])
    service = RagService(backend(clean), chat_model=object(), keyword_search=backend(clean), enable_keyword=True)
    monkeypatch.setitem(sys.modules, 'main', SimpleNamespace(init_manager=SimpleNamespace(rag_service=service)))
    data = RAGRequest.model_validate({'query': QUESTION, 'query_expansion_mode': 'adaptive'})
    assert data.use_hyde is False
    result = (await chat.rag_query(data, OWNER))['data']
    assert result['query_expansion']['status'] == 'off' and clean.await_count == 4


@pytest.mark.asyncio
async def test_real_vector_miss_recovered_and_reauthorized(hybrid, note_vectors, request):
    _, factory, seed = hybrid
    # 21 near neighbours keep the target outside the original vector top 20.
    # Two stale Chroma entries still claim the requesting owner.
    target, foreign, retired = 'maintenance evidence', 'foreign evidence', 'retired evidence'
    rows = [(1, target, [0., 1.]), (2, foreign, [0., 1.]), (3, retired, [0., 1.])]
    rows += [(n, f'original source {n}', [1., 0.]) for n in range(20, 41)]
    for identifier, text, vector in rows:
        seed(identifier, text, owner='other' if identifier == 2 else OWNER, ready=identifier != 3)
    candidates = [vector_result(i, t) for i, t, _ in rows]
    note_vectors._rag_collection.upsert(ids=[r['chunk_id'] for r in candidates],
                                       documents=[r['content'] for r in candidates],
                                       metadatas=[r['metadata'] for r in candidates],
                                       embeddings=[v for _, _, v in rows])
    note_vectors._generate_embeddings.side_effect = lambda texts: [
        [0., 1.] if text == REWRITES[1] else [1., 0.] for text in texts]
    def predict(pairs):
        assert all(q == QUESTION and t not in (foreign, retired) for q, t in pairs)
        return [float(t == target) for _, t in pairs]
    service = RagService(note_vectors, rerank_model=SimpleNamespace(predict=predict),
                         enable_keyword=True, db_session_factory=factory)
    before = await service.query(QUESTION, OWNER)
    assert before['rerank_status'] == 'success' and all(r['content'] != target for r in before['sources'])
    service.expansion_generator = AsyncMock(return_value=reply(*REWRITES))
    after = await service.query(QUESTION, OWNER)
    assert after['status'] == 'success' and after['rerank_status'] == 'success'
    hit = after['sources'][0]
    assert hit['content'] == target and json.loads(after['context'])[0] == {'id': 'E1', 'text': target}
    assert hit['source_locator']['resource_version_id'] == hit['metadata']['source_sha256']
    origin, = hit['provenance']
    assert (origin['method'], origin['algorithm'], origin['rank'], origin['subquery_id']) == (
        'expanded', 'chroma_cosine', 1, 2)
    assert note_vectors._generate_embeddings.await_count == 4  # one baseline + original and two rewrites
    request.node.user_properties.append(('expansion_recall', json.dumps({
        'fixture_documents': len(rows), 'original_top20_found_target': False,
        'expanded_final_rank': hit['retrieval_rank'], 'expanded_query_id': 2,
        'unauthorized_texts_in_reranker': 0, 'source_version_verified': True})))
