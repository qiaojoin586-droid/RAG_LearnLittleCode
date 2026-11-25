"""M3-12 publication, scoped recall and dispatch authorization regressions."""
from datetime import datetime
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import sqlalchemy as sa
from langchain_core.messages import HumanMessage
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from app.ai_service.chat_graph import LegacyEvidenceGuard
from app.ai_service.agent import guard_tool_call
from app.core.failed_response import BusinessError
from app.model_config import runtime, tables
from app.model_config.snapshots import current_snapshot, inference_actor
from app.models.knowledge import KnowledgeDocument, knowledge_search_chunks
from app.models.knowledge_ingest import knowledge_ingest_jobs
from app.models.note import Note
from app.models.note_index import note_index_jobs, note_search_chunks
from app.models.user import User
from app.rag.citations import CitationTurn, current_citations
from app.rag.keyword_publication import verify_keyword_publication
from app.rag.keyword_search import KeywordHits
from app.rag.rag_service import RagService
from app.rag.source_spans import hash_text, make_document_manifest
from test_hybrid_retrieval import OWNER, hybrid, vector_result
from test_model_config_control_plane import admin, store
from test_model_config_publication import database_factory, mock_runtime, selected, snapshot
from test_model_fallback import verified
from test_query_expansion import QUESTION, REWRITES, reply
from test_vector_binding_runtime import vectors


def backend(rows=()):
    return SimpleNamespace(search_documents=AsyncMock(return_value=list(rows)), search_notes=AsyncMock(return_value=[]))


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['superseded', 'missing', 'wrong_body', 'unknown_analyzer'])
async def test_sql_lag_reports_partial_and_preserves_current_vector(hybrid, mutation):
    engine, factory, seed = hybrid
    text = 'check AX-900'
    seed(1, text)
    with engine.begin() as db:
        chunks = knowledge_search_chunks
        if mutation == 'superseded':
            db.execute(chunks.update().values(resource_version_id='previous', content='private old value'))
        elif mutation == 'missing':
            db.execute(chunks.delete())
        elif mutation == 'wrong_body':
            db.execute(chunks.update().values(content='AX-900 private old value'))
        else:
            docs = KnowledgeDocument.__table__
            manifest = db.scalar(sa.select(docs.c.source_manifest))
            db.execute(docs.update().values(source_manifest={**manifest, 'keyword_projection_revision': 'unknown'}))
    reranker = Mock(return_value=[1])
    service = RagService(backend([vector_result(1, text)]), enable_keyword=True,
                         db_session_factory=factory, rerank_model=SimpleNamespace(predict=reranker))
    result = await service.query('AX-900', OWNER)
    assert result['status'] == 'partial' and result['failed_sources'] == ['keyword_knowledge']
    assert 'keyword_index_lag' in result['degraded_stages']
    assert json.loads(result['context']) == [{'id': 'E1', 'text': text}]
    assert [p['method'] for p in result['sources'][0]['provenance']] == ['vector']
    reranker.assert_called_once_with([('AX-900', text)])


@pytest.mark.asyncio
async def test_note_projection_coverage_detects_missing_ordinals_before_matching(hybrid):
    engine, factory, _ = hybrid
    body = 'manual'
    manifest = make_document_manifest('md', hash_text(body), [(body, {'char_start': 0, 'char_end': len(body)})])
    note = {'id': 'note', 'title': 'title', 'user_id': OWNER, 'content': body}
    with engine.begin() as conn:
        conn.execute(Note.__table__.insert().values(note))
        conn.execute(note_index_jobs.insert().values(note_id='note', user_id=OWNER, revision='new', status='succeeded',
                     operation='upsert', attempts=0, next_attempt_at=datetime.now(), source_manifest=manifest))
        conn.execute(note_search_chunks.insert().values(note_id='note', ordinal=0, revision='old', title='', content='old'))
    service = RagService(backend(), db_session_factory=factory, enable_keyword=True)
    result = await service.query('nothing matches', OWNER)
    assert result['status'] == 'partial' and result['failed_sources'] == ['keyword_notes'] and not result['sources']
    state = {'rag': {}, 'notes': {'note': {'source_manifest': manifest, 'revision': 'new', 'digest': hash_text(body),
              'is_indexed': True, 'deleted_at': None, 'status': 'succeeded', 'operation': 'upsert'}}}
    async with factory() as db:
        with pytest.raises(BusinessError) as error:
            await verify_keyword_publication(db, state)
        assert error.value.data['reason'] == 'KEYWORD_PROJECTION_INCOMPLETE'
    with engine.begin() as conn:
        conn.execute(note_search_chunks.update().values(revision='new', content=body))
    async with factory() as db:
        proof = await verify_keyword_publication(db, state)
    assert proof['notes'] == 1
    restored = await service.query('manual', OWNER)
    assert restored['status'] == 'success'
    assert restored['sources'][0]['provenance'][0]['index_version_id'] == proof['index_revision']


@pytest.mark.asyncio
async def test_incompatible_sql_revisions_do_not_vote(hybrid):
    _, factory, seed = hybrid
    seed(1, 'current')
    row = vector_result(1, 'current')
    keyword = SimpleNamespace(search_documents=AsyncMock(return_value=KeywordHits(
        [{**row, 'algorithm': 'sql_literal'}], 'old', False)),
        search_notes=AsyncMock(return_value=KeywordHits([], 'new', False)))
    result = await RagService(backend([row]), enable_keyword=True, keyword_search=keyword,
                              db_session_factory=factory).query('current', OWNER)
    assert result['status'] == 'partial' and result['failed_sources'] == ['keyword_knowledge', 'keyword_notes']
    assert [p['method'] for p in result['sources'][0]['provenance']] == ['vector']


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['reorder', 'summarize', 'while_summarizing', 'final_without_summary'])
async def test_publication_rechecked_after_waits(hybrid, stage):
    engine, factory, seed = hybrid
    seed(1, 'private evidence')
    def revoke():
        with engine.begin() as conn:
            conn.execute(KnowledgeDocument.__table__.update().values(ready_for_retrieval=False))
    async def progress(event):
        if event['event'] == stage or stage == 'final_without_summary' and event['event'] == 'summarize':
            revoke()
    async def summary(*args):
        if stage == 'while_summarizing':
            revoke()
        return 'summary of private evidence'
    predict = Mock(return_value=[1])
    service = RagService(backend([vector_result(1, 'private evidence')]), chat_model=object(),
        enable_summarize=stage != 'final_without_summary', rerank_model=SimpleNamespace(predict=predict),
        enable_keyword=False, db_session_factory=factory)
    service._summarize_single = AsyncMock(side_effect=summary)
    result = await service.query('lookup', OWNER, thinking_callback=progress)
    assert result['status'] == 'partial' and 'evidence_changed' in result['degraded_stages']
    assert not result['context'] and not result['sources']
    if stage == 'reorder':
        predict.assert_not_called()
    if stage != 'while_summarizing':
        service._summarize_single.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['wrong_owner', 'suspended'])
async def test_actor_validation_precedes_all_three_lanes_and_generation(store, reason):
    owner = store.actor.user_id
    if reason == 'suspended':
        store.sql(User.__table__.update().where(User.uuid == owner).values(status='banned'))
    actor = inference_actor.set({'user_id': owner})
    vectors, keyword, expand = backend(), backend(), AsyncMock()
    try:
        service = RagService(vectors, keyword_search=keyword, enable_keyword=True,
            db_session_factory=database_factory(store), expansion_generator=expand)
        with pytest.raises(BusinessError) as denied:
            await service.query(QUESTION, store.other.user_id if reason == 'wrong_owner' else owner)
        assert denied.value.http_status == 403
        for method in (vectors.search_documents, vectors.search_notes, keyword.search_documents, keyword.search_notes, expand):
            method.assert_not_awaited()
    finally:
        inference_actor.reset(actor)


@pytest.mark.asyncio
async def test_release_switch_during_expansion_preserves_plan_and_actual_index_calls(vectors, monkeypatch):
    v, owner = vectors, vectors.admin.actor.user_id
    factory = database_factory(v.admin)
    for table in (KnowledgeDocument.__table__, knowledge_search_chunks, Note.__table__, note_index_jobs, note_search_chunks):
        table.create(v.admin.engine, checkfirst=True)
    traces = []
    from app.model_config import vector_runtime
    bound = vector_runtime.query
    async def observed(text, principal, key, top_k):
        traces.append((text, principal, key, current_snapshot().release_id))
        return await bound(text, principal, key, top_k)
    monkeypatch.setattr(vector_runtime, 'query', observed)
    async def switch(question, *, max_output_tokens):
        # Simulate another publisher moving the head after this request was admitted.
        v.admin.sql(tables.ai_config_heads.update().values(active_release_id=v.text_published['release_id']))
        return reply(*REWRITES)
    service = RagService(v.service, enable_keyword=True, db_session_factory=factory, expansion_generator=switch)
    with selected(v.admin, v.snapshot):
        result = await service.query(QUESTION, owner)
    assert result['status'] == 'empty'
    assert {(q, k) for q, u, k, release in traces if u == owner and release == v.snapshot.release_id} == {
        (q, k) for q in [QUESTION, *REWRITES] for k in ('rag', 'notes')}
    assert len(traces) == 6 and (await snapshot(v.admin)).release_id != v.snapshot.release_id
    plan = result['index_snapshot']
    assert plan['config_release_id'] == v.snapshot.release_id
    assert set(plan['vector_index_versions'].values()) == set(v.snapshot.indexes)
    assert set(plan['keyword_index_versions']) == {'knowledge', 'notes'}


@pytest.mark.asyncio
@pytest.mark.parametrize('native', [False, True], ids=['openai', 'ollama'])
async def test_cached_evidence_reauthorized_at_live_transport_and_legacy_model(admin, native, monkeypatch):
    pinned, owner = await verified(admin, local=native), admin.actor.user_id
    factory = database_factory(admin)
    for table in (KnowledgeDocument.__table__, knowledge_search_chunks):
        table.create(admin.engine, checkfirst=True)
    text = 'private original'
    row = vector_result(1, text, user_id=owner)
    manifest = make_document_manifest('txt', hash_text(text), [(text, {'char_start': 0, 'char_end': len(text)})])
    admin.sql(KnowledgeDocument.__table__.insert().values(id=1,user_id=owner,filename='f',file_path='f',
        md5_hash='fixture',file_size=len(text),file_type='txt',chunk_count=1,source_manifest=manifest))
    cached = await RagService(backend([row]), enable_keyword=False, db_session_factory=factory).query('q', owner)
    turn = CitationTurn(cached['context'], cached['sources'], owner=owner, session_factory=factory)
    token = current_citations.set(turn)
    try:
        await turn.authorize()
        admin.sql(KnowledgeDocument.__table__.delete().where(KnowledgeDocument.id == 1))
        requests = mock_runtime(admin, monkeypatch)
        with selected(admin, pinned):
            with pytest.raises(BusinessError) as denied:
                await runtime.model_for('chat').ainvoke([HumanMessage(content=text)])
            assert denied.value.http_status == 409 and not requests
        legacy = FakeListChatModel(responses=['should not dispatch'])
        with pytest.raises(BusinessError):
            await legacy.ainvoke([HumanMessage(content=text)], config={'callbacks': [LegacyEvidenceGuard()]})
        handler = AsyncMock()
        with pytest.raises(BusinessError):
            await guard_tool_call.awrap_tool_call(SimpleNamespace(tool=object()), handler)
        handler.assert_not_awaited()
    finally:
        current_citations.reset(token)


@pytest.mark.asyncio
async def test_switch_checks_body_hash_before_accepting_source_watermark(hybrid, tmp_path):
    from app.model_config.index_switch import verify_index_switch
    engine, factory, seed = hybrid
    knowledge_ingest_jobs.create(engine)
    seed(1, 'published')
    with engine.begin() as db:
        db.execute(knowledge_search_chunks.update().values(content='obsolete'))
    async with factory() as db:
        with pytest.raises(BusinessError) as error:
            await verify_index_switch(db, {}, workspace=tmp_path)
    assert error.value.data['reason'] == 'KEYWORD_PROJECTION_INCOMPLETE'
    with engine.begin() as db:
        db.execute(knowledge_search_chunks.update().values(content='published'))
    async with factory() as db:
        proof = await verify_index_switch(db, {}, workspace=tmp_path)
    assert len(proof['source_watermark']) == 64 and proof['keyword']['rag'] == 1


@pytest.mark.asyncio
async def test_warm_embedding_cache_cannot_restore_revoked_document(vectors):
    v, owner = vectors, vectors.admin.actor.user_id
    docs = KnowledgeDocument.__table__
    for table in (docs, Note.__table__, note_index_jobs):
        table.create(v.admin.engine, checkfirst=True)
    text = 'published cache fixture'
    manifest = make_document_manifest('txt', hash_text(text), [(text, {'char_start': 0, 'char_end': len(text)})])
    v.admin.sql(docs.insert().values(id=1,user_id=owner,filename='f',file_path='unused',
        md5_hash='fixture',file_size=len(text),file_type='txt',chunk_count=1,source_manifest=manifest))
    svc = RagService(v.service, enable_keyword=False, db_session_factory=database_factory(v.admin))
    with selected(v.admin, v.snapshot):
        await v.service.upsert_document([text], [vector_result(1, text, user_id=owner)['metadata']], ['1_0'])
        first = await svc.query('repeatable query', owner)
        assert first['sources'][0]['citation_id'] == 'E1'
        calls = len([r for r in v.requests if r.method == 'POST'])
        v.admin.sql(docs.update().where(docs.c.id == 1).values(user_id=v.admin.other.user_id))
        second = await svc.query('repeatable query', owner)
        assert len([r for r in v.requests if r.method == 'POST']) == calls
        assert second['sources'] == [] and second['context'] == ''


@pytest.mark.asyncio
async def test_recheck_rejects_replaced_span_even_when_file_sha_and_text_match(hybrid):
    engine, factory, seed = hybrid
    seed(1, 'body')
    async def replace_span(event):
        if event['event'] == 'summarize':
            docs = KnowledgeDocument.__table__
            with engine.begin() as conn:
                manifest = conn.scalar(sa.select(docs.c.source_manifest))
                manifest['chunks'][0]['section_title'] = 'different heading'
                conn.execute(docs.update().values(source_manifest=manifest))
    result = await RagService(backend([vector_result(1, 'body')]), enable_keyword=False,
                              db_session_factory=factory).query('q', OWNER, thinking_callback=replace_span)
    assert result['sources'] == [] and result['context'] == ''


@pytest.mark.asyncio
async def test_hyde_cache_partition_and_live_authorization(admin, monkeypatch):
    from app.rag import rag_service
    pinned, owner = await verified(admin), admin.actor.user_id
    service = RagService(enable_keyword=False, db_session_factory=database_factory(admin))
    assert service._hyde_cache_key('lookup', owner) is None
    with selected(admin, pinned):
        key = service._hyde_cache_key('lookup', owner)
        assert key and owner not in key and 'lookup' not in key
        assert service._hyde_cache_key('最近三天的指标', owner) is None
        assert service._hyde_cache_key('lookup', admin.other.user_id) is None
        with selected(admin, replace(pinned, release_id='next-release')):
            assert service._hyde_cache_key('lookup', owner) != key
        model = runtime.model_for('chat')
        monkeypatch.setattr(rag_service, 'model_for', lambda *args: model)
        service._get_hyde_cache = AsyncMock(return_value='cached search expression')
        assert await service._generate_hyde('lookup', owner) == 'cached search expression'
        admin.sql(User.__table__.update().where(User.uuid == owner).values(status='banned'))
        with pytest.raises(BusinessError) as denied:
            await service._generate_hyde('lookup', owner)
        assert denied.value.http_status == 403


@pytest.mark.asyncio
async def test_switch_validates_last_keyword_page_for_large_documents(hybrid):
    engine, factory, seed = hybrid
    seed(1, 'initial')
    spans = [(str(i), {'char_start': 0, 'char_end': len(str(i))}) for i in range(257)]
    manifest = make_document_manifest('txt', hash_text('fixture version'), spans)
    sources = {'rag': {'1': {'ready_for_retrieval': True, 'source_manifest': manifest, 'chunk_count': len(spans)}},
               'notes': {}}
    with engine.begin() as db:
        db.execute(knowledge_search_chunks.delete())
        db.execute(knowledge_search_chunks.insert(), [
            {'document_id': 1, 'ordinal': i, 'resource_version_id': manifest['resource_version_id'], 'content': text}
            for i, (text, _) in enumerate(spans)])
    async with factory() as db:
        assert (await verify_keyword_publication(db, sources))['rag'] == len(spans)
    with engine.begin() as db:
        db.execute(knowledge_search_chunks.delete().where(knowledge_search_chunks.c.ordinal == 256))
    async with factory() as db:
        with pytest.raises(BusinessError) as error:
            await verify_keyword_publication(db, sources)
        assert error.value.data['reason'] == 'KEYWORD_PROJECTION_INCOMPLETE'
