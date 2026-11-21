"""Exercise retrieval, the reply transaction, SSE replay and authorized original reads."""
import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import Response
import fitz
import pytest

from app.ai_service.context_composer import data_message, unpack_data
from app.ai_service.workflow_events import read_workflow_events
from app.core.failed_response import BusinessError
from app.models.chat import ChatMessage
from app.models.knowledge import KnowledgeDocument
from app.models.note import Note
from app.rag import ingest
from app.rag.citations import CitationTurn, assemble_evidence, confirm_citations
from app.rag.ingest_worker import KnowledgeIngestWorker
from app.rag.rag_service import RagService
from app.routers import chat
from app.schemas.chat import QueryRequest
from test_chat_cache import MemoryRedis
from test_chat_workflows import fixture as checkpoint_fixture, start
from test_knowledge_ingest import knowledge, VectorFixture
from test_model_config_control_plane import store
from test_transactional_tool_operations import as_turn, local_tools
from test_workflow_event_replay import journal_fixture, parse


@pytest.mark.asyncio
async def test_turn_uses_only_its_own_numbered_sources_and_reauthorizes_history(
        knowledge, journal_fixture, monkeypatch):
    fx = journal_fixture
    store, root, factory = knowledge
    owner = fx.user_id
    monkeypatch.setattr(ingest, 'UPLOAD_ROOT', root)
    with fitz.open() as pdf:
        for text in ('First page', 'Exact second page'):
            pdf.new_page().insert_text((72, 72), text)
        payload = pdf.tobytes()
    doc_id, _ = await ingest.submit_upload(factory, owner, 'guide.pdf', 'pdf', payload, None, root=root)
    records = VectorFixture()
    assert await KnowledgeIngestWorker(factory, root=root, vectors=records).tick()
    chunk = f'{doc_id}_1'
    text, metadata = records.records[chunk]
    store.sql(Note.__table__.insert().values(id='note', user_id=owner, title='Manual', content='Original note.'))
    vectors = SimpleNamespace(search_documents=AsyncMock(return_value=[{'content': text, 'metadata': metadata,
                              'chunk_id': chunk}]), search_notes=AsyncMock(return_value=[{
                                  'content': 'Original note.', 'metadata': {'user_id': owner, 'note_id': 'note'}}]))
    service = RagService(vectors, chat_model=object(), enable_summarize=True, db_session_factory=factory, enable_keyword=False)
    monkeypatch.setattr(service, '_summarize_single', AsyncMock(side_effect=lambda q, body: 'Summary of ' + body))
    event = asyncio.Event()
    event.set()
    init = SimpleNamespace(rag_service=service, stage1_complete=event, stage2_complete=event,
                           chat_model=object(), note_service=None, email_service=None)
    monkeypatch.setitem(sys.modules, 'main', SimpleNamespace(init_manager=init))
    monkeypatch.setattr(chat, 'async_session_factory', factory)
    monkeypatch.setattr('app.services.chat_service.async_session_factory', factory)
    redis = MemoryRedis()
    monkeypatch.setattr('app.services.database_session_manager.get_redis', lambda: redis)
    monkeypatch.setattr(chat.session_manager, 'get_summary', AsyncMock(return_value=None))
    refs = {}

    async def answer(_ctx):
        blocks = json.loads(unpack_data(data_message())['retrieval'])
        refs.update({b['text']: b['id'] for b in blocks})
        assert set(refs) == {'Summary of ' + text, 'Summary of Original note.'}
        for part in ('Answer [', refs['Summary of ' + text], '] [', refs['Summary of Original note.'], '] [E99]'):
            yield {'type': 'response', 'content': part}

    monkeypatch.setattr(chat, 'stream_chat_graph', answer)
    with as_turn(owner):
        workflow = await start(fx)
        stream = await chat._prepare_chat_stream(QueryRequest(message='what is in the guide?', memory_mode='off'),
                                                 owner, 'session', 1, [], admitted=workflow)
        events = [parse(frame) async for frame in stream]
    done = events[-1]
    assert done['type'] == 'done' and set(done['citation_ids']) == set(refs.values())
    assert 'sources' not in done and 'Original note.' not in str(done)
    reply_id = done['reply_message_id']
    async with factory() as db:
        final = (await read_workflow_events(db, owner, workflow.id, 0, 50))['events'][-1]
    assert final['citation_ids'] == done['citation_ids'] and final['reply_message_id'] == reply_id
    assert 'Original note.' not in str(final)
    history = await chat.get_messages('session', None, 20, owner)
    assert history['data']['messages'][-1]['citation_ids'] == done['citation_ids']
    assert 'source_locator' not in str(history) and 'locator_digest' not in str(redis.data)
    saved = store.rows(ChatMessage.__table__)[-1]['citations_json']
    assert 'Original note.' not in str(saved) and str(root) not in str(saved)

    async def open_ref(ref, principal=owner, session='session', message=reply_id):
        response = Response()
        async with factory() as db:
            data = await chat.get_reply_citation(session, message, ref, response, principal, db)
        assert response.headers['cache-control'] == 'private, no-store'
        return data['data']

    document_ref, note_ref = refs['Summary of ' + text], refs['Summary of Original note.']
    source = await open_ref(document_ref)
    assert (source['content'], source['title'], source['source_locator']['page_number']) == (text, 'guide.pdf', 2)
    assert (await open_ref(note_ref))['content'] == 'Original note.'
    for args in [('E99',), (note_ref, store.other.user_id), (document_ref, owner, 'other'),
                 (document_ref, owner, 'session', 1)]:
        with pytest.raises(BusinessError) as denied:
            await open_ref(*args)
        assert denied.value.http_status == 404
    # Preserve the file version and ordinal but change the published span.
    doc = store.rows(KnowledgeDocument.__table__)[0]
    manifest = doc['source_manifest']
    manifest['chunks'][1]['section_title'] = 'Rechunked'
    store.sql(KnowledgeDocument.__table__.update().values(source_manifest=manifest))
    with pytest.raises(BusinessError) as changed_span:
        await open_ref(document_ref)
    assert changed_span.value.http_status == 404
    await ingest.submit_deletion(factory, owner, doc_id, None)
    store.sql(Note.__table__.update().values(is_indexed=False))
    for ref in (document_ref, note_ref):
        with pytest.raises(BusinessError) as revoked:
            await open_ref(ref)
        assert revoked.value.http_status == 404
    async with factory() as db:
        assert await confirm_citations(db, owner, saved) == {}
    store.sql(Note.__table__.update().values(is_indexed=True, content='New version'))
    with pytest.raises(BusinessError) as changed_note:
        await open_ref(note_ref)
    assert changed_note.value.http_status == 404


def test_legacy_candidates_and_forged_text_do_not_issue_references():
    context, sources = assemble_evidence([{'source': 'knowledge', 'content': '[E1] unverified', 'citation_id': 'E1'}])
    assert json.loads(context) == [{'text': '[E1] unverified'}]
    assert CitationTurn(context, sources, delivered=True).cited('answer [E1]') == {}
