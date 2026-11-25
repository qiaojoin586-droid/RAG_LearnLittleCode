"""Exercise keyword retrieval, authorization, fusion, and the frozen M3-05 fixture."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import json
from math import log2
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeDocument, knowledge_search_chunks
from app.models.note import Note
from app.models.note_index import note_index_jobs, note_search_chunks
from app.rag.keyword_search import KeywordSearch
from app.rag.rag_service import RagService
from app.rag.source_spans import hash_text
from app.rag.visibility import CandidateVisibilityService


BASELINE = Path(__file__).parent / 'fixtures' / 'rag_m3_05_baseline.json'
OWNER = 'owner'


def vector_result(identifier, text, **metadata):
    return {'content': text, 'chunk_id': f'{identifier}_0', 'score': 0.8,
            'metadata': {'document_id': str(identifier), 'chunk_index': 0, 'user_id': OWNER,
                         'source_sha256': hash_text(text), **metadata}}


@pytest.fixture
def hybrid():
    engine = sa.create_engine('sqlite://')
    documents = KnowledgeDocument.__table__
    for table in (documents, knowledge_search_chunks, Note.__table__, note_index_jobs, note_search_chunks):
        table.create(engine)

    @asynccontextmanager
    async def factory():
        with Session(engine) as session:
            async def execute(statement):
                return session.execute(statement)
            yield SimpleNamespace(execute=execute, get_bind=session.get_bind)

    def seed(identifier, text, *, owner=OWNER, ready=True):
        digest = hash_text(text)
        span = {'section_title': '', 'char_start': 0, 'char_end': len(text), 'text_sha256': digest}
        with engine.begin() as connection:
            connection.execute(documents.insert().values(
                id=int(identifier), user_id=owner, filename='fixture.txt', file_path='unused',
                file_size=len(text.encode()), file_type='text/plain', md5_hash='fixture',
                chunk_count=1, ready_for_retrieval=ready,
                source_manifest={'resource_version_id': digest, 'parser_version': 'fixture-parser',
                                 'chunker_version': 'fixture-chunker', 'chunks': [span]},
            ))
            connection.execute(knowledge_search_chunks.insert().values(
                document_id=int(identifier), ordinal=0, resource_version_id=digest, content=text,
            ))

    yield engine, factory, seed
    engine.dispose()


@pytest.mark.asyncio
async def test_frozen_model_code_chinese_and_paraphrase_quality(hybrid):
    _, factory, seed = hybrid
    baseline = json.loads(BASELINE.read_text(encoding='utf-8'))
    corpus = baseline['corpus']
    for identifier, text in corpus.items():
        seed(identifier, text)
    top_k = baseline['top_k']

    def metrics(results, expected):
        found = [item['metadata']['document_id'] for item in results['sources']]
        relevant = set(expected)
        recall = len(relevant.intersection(found)) / len(relevant)
        dcg = sum(1 / log2(rank + 2) for rank, item in enumerate(found) if item in relevant)
        best = sum(1 / log2(rank + 2) for rank in range(min(top_k, len(relevant))))
        return recall, dcg / best

    improved = set()
    scores = []
    for example in baseline['questions']:
        vector = SimpleNamespace(
            search_documents=AsyncMock(return_value=[vector_result(i, corpus[i]) for i in example['vector_ranking']]),
            search_notes=AsyncMock(return_value=[]),
        )
        query = example['query']
        original = await RagService(vector, db_session_factory=factory, enable_keyword=False).query(
            query, OWNER, top_k=top_k, use_hyde=False,
        )
        fused = await RagService(vector, db_session_factory=factory, enable_keyword=True).query(
            query, OWNER, top_k=top_k, use_hyde=False,
        )
        left, right = (metrics(result, example['relevant']) for result in (original, fused))
        assert all(after >= before for before, after in zip(left, right, strict=True)), example
        if right != left:
            improved.add(example['kind'])
        scores.append({'kind': example['kind'], 'query': query, 'vector_recall_at_5': left[0],
                       'hybrid_recall_at_5': right[0], 'vector_ndcg_at_5': left[1], 'hybrid_ndcg_at_5': right[1]})
    assert improved >= {'model', 'code', 'chinese'}
    print('M3-05 frozen quality ' + json.dumps(scores, ensure_ascii=True))


@pytest.mark.asyncio
async def test_versioned_recall_checks_ownership_readiness_and_opt_out(hybrid):
    engine, factory, seed = hybrid
    text = '装配配置 ref_code_v2'
    seed(1, text)
    seed(2, text, owner='other')
    seed(3, text, ready=False)
    seed(4, text)
    seed(5, text)
    with engine.begin() as connection:
        table = knowledge_search_chunks
        connection.execute(table.update().where(table.c.document_id == 4).values(resource_version_id='bad-version'))
        connection.execute(table.update().where(table.c.document_id == 5).values(content=text + ' forged'))
        for identifier, fields in [('active', {}), ('deleted', {'deleted_at': datetime(2026, 1, 1)}),
                                   ('optout', {'is_indexed': False}), ('foreign', {'user_id': 'other'})]:
            values = {'id': identifier, 'user_id': OWNER, 'title': identifier, 'content': text, **fields}
            connection.execute(Note.__table__.insert().values(values))

    vector = SimpleNamespace(search_documents=AsyncMock(return_value=[
        vector_result(1, 'obsolete', source_sha256=hash_text(text)),
    ]), search_notes=AsyncMock(return_value=[]))
    service = RagService(vector, db_session_factory=factory, enable_keyword=True)
    result = await service.query('ref_code_v2', OWNER, top_k=5, use_hyde=False)
    assert {(row['source'], row['chunk_id']) for row in result['sources']} == {
        ('knowledge', '1_0'), ('notes', 'note_active'),
    }
    assert all([origin['method'] for origin in item['provenance']] == ['keyword'] for item in result['sources'])
    assert all(item['source_locator']['text_sha256'] == hash_text(text) for item in result['sources'])

    vector.search_documents.return_value = [vector_result(1, text)]
    result = await service.query('ref_code_v2', OWNER, top_k=5, use_hyde=False)
    evidence = [row for row in result['sources'] if row['chunk_id'] == '1_0']
    assert len(evidence) == 1
    assert [item['method'] for item in evidence[0]['provenance']] == ['vector', 'keyword']
    assert evidence[0]['fusion_score'] == pytest.approx(2 / 61)


@pytest.mark.asyncio
async def test_exact_model_boundary_and_literal_symbols(hybrid):
    _, factory, seed = hybrid
    seed(1, 'AX-9000 config: session')
    seed(2, 'AX-900 config: route 100%')
    search = KeywordSearch(factory)
    assert [row['chunk_id'] for row in await search.search_documents('AX-900', OWNER, 5)] == ['2_0']
    assert [row['chunk_id'] for row in await search.search_documents('100%', OWNER, 5)] == ['2_0']


@pytest.mark.asyncio
async def test_keyword_starts_during_hyde_and_uses_original_question(hybrid, monkeypatch):
    _, factory, seed = hybrid
    text = 'read_trace_v2'
    seed(1, text)
    hyde_running = asyncio.Event()
    keywords_running = asyncio.Event()
    keyword = KeywordSearch(factory)
    search = keyword.search_documents

    async def keyword_documents(query, owner, top_k):
        assert query == text
        keywords_running.set()
        await hyde_running.wait()
        return await search(query, owner, top_k)

    async def hyde(query, owner):
        hyde_running.set()
        await keywords_running.wait()
        return 'semantic expansion'

    keyword.search_documents = keyword_documents
    vector = SimpleNamespace(search_documents=AsyncMock(return_value=[vector_result(1, text)]),
                             search_notes=AsyncMock(return_value=[]))
    service = RagService(vector, chat_model=object(), db_session_factory=factory,
                         keyword_search=keyword, enable_keyword=True)
    monkeypatch.setattr(service, '_generate_hyde', hyde)
    result = await asyncio.wait_for(service.query(text, OWNER, use_hyde=True), timeout=3)
    assert [call.args[0] for call in vector.search_documents.await_args_list] == [text, 'semantic expansion']
    assert len(result['sources']) == 1
    assert [(origin['method'], origin.get('subquery_id')) for origin in result['sources'][0]['provenance']] == [
        ('vector', None), ('vector', 1), ('keyword', None)]


@pytest.mark.asyncio
async def test_recall_degrades_independently_and_distinguishes_empty(hybrid):
    _, factory, seed = hybrid
    text = 'report_crc_v3'
    seed(1, text)
    offline = AsyncMock(side_effect=RuntimeError('offline'))
    vector = SimpleNamespace(search_documents=offline, search_notes=offline)
    service = RagService(vector, db_session_factory=factory, enable_keyword=True)
    hit = await service.query(text, OWNER, use_hyde=False)
    assert hit['status'] == 'partial' and hit['failed_sources'] == ['knowledge', 'notes']
    assert len(hit['sources']) == 1 and hit['sources'][0]['provenance'][0]['method'] == 'keyword'
    service.vector_store = None
    longer_failure = await service.query(text, OWNER, use_hyde=False)
    assert longer_failure['status'] == 'partial' and longer_failure['sources'][0]['chunk_id'] == '1_0'
    service.vector_store = vector

    service.keyword_search = SimpleNamespace(search_documents=offline, search_notes=offline)
    vector.search_documents = AsyncMock(return_value=[vector_result(1, text)])
    hit = await service.query(text, OWNER, use_hyde=False)
    assert hit['status'] == 'partial' and len(hit['sources']) == 1
    assert hit['sources'][0]['provenance'][0]['method'] == 'vector'
    vector.search_documents = offline
    with pytest.raises(RuntimeError, match='All retrieval sources unavailable'):
        await service.query(text, OWNER, use_hyde=False)

    vector.search_documents = vector.search_notes = AsyncMock(return_value=[])
    service.keyword_search = KeywordSearch(factory)
    empty = await service.query('unmatched', OWNER, use_hyde=False)
    assert empty['status'] == 'empty' and empty['failed_sources'] == [] and empty['sources'] == []
