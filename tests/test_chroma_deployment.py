"""Cutover and rollback through an actual Chroma HTTP process and committed SQL state."""

from contextlib import contextmanager
from datetime import datetime
import json
import socket
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from chromadb import PersistentClient
from chromadb.config import Settings

from app.core.failed_response import BusinessError
from app.model_config.index_switch import verify_index_switch
from app.model_config.vector_runtime import bound_collection
from app.models.knowledge import KnowledgeDocument
from app.models.knowledge_ingest import knowledge_ingest_jobs
from app.models.note import Note
from app.rag.chroma_deployment import prepare_deployment, serve_store
from app.rag.chroma_transport import ChromaTransportError, open_chroma, validate_routes
from app.rag.maintenance_lock import VectorStoreBusy, local_vector_owner
from app.rag.reconcile import reconcile
from app.rag.vector_store import VectorStoreService
from test_model_config_control_plane import store  # noqa: F401
from test_transactional_tool_operations import local_tools  # noqa: F401


def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize('value', ['[]', '{"data/chroma":"8009"}', '{"data/chroma":true}',
                                  '{"data/chroma":8009,"./data/chroma":8010}', '{"a":8009,"b":8009}',
                                  '{"a":8009,"a":8010}', '{"a":"http://another-host:8009"}'])
def test_route_config_is_loopback_only_and_unambiguous(monkeypatch, value):
    monkeypatch.setenv('RAG_CHROMA_HTTP_ROUTES', value)
    with pytest.raises(ChromaTransportError, match='Invalid RAG_CHROMA_HTTP_ROUTES'):
        validate_routes()


@pytest.mark.asyncio
async def test_service_cutover_keeps_identity_and_restore_keeps_new_writes(local_tools, tmp_path, monkeypatch):
    for name in ('TEMP', 'TMP', 'SQLITE_TMPDIR'):
        monkeypatch.setenv(name, str(tmp_path))
    fx = local_tools
    owner, other = fx.user_id, fx.store.other.user_id
    mine, theirs, added = (str(uuid4()) for _ in range(3))
    documents = KnowledgeDocument.__table__
    notes = Note.__table__
    for table in (documents, knowledge_ingest_jobs):
        table.create(fx.store.engine)
    fx.store.sql(notes.insert().values([dict(id=mine, user_id=owner, title='mine', content='old note'),
                                       dict(id=theirs, user_id=other, title='other', content='other note')]))
    fx.store.sql(documents.insert().values([
        dict(id=identifier, user_id=user, filename='example.md', file_path='example.md', file_size=12,
             file_type='text/markdown', md5_hash='hash', chunk_count=1, ready_for_retrieval=True)
        for identifier, user in ((1, owner), (2, other))
    ]))
    initial, remote, restored = (tmp_path / name for name in ('initial', 'remote', 'restored'))
    legacy = {'persist_directory': str(initial)}
    port = _free_port()
    with PersistentClient(path=str(initial), settings=Settings(anonymized_telemetry=False)) as client:
        indexes = {}
        for key in ('rag', 'notes'):
            collection = client.create_collection(f'{key}_collection', metadata={'hnsw:space': 'cosine'},
                                                  embedding_function=None)
            indexes[key] = {'id': str(uuid4()), 'collection_name': collection.name, 'dimension': 2,
                            'storage_binding': {'adapter': 'chroma_local', 'evidence_hash': 'a' * 64,
                                                'directory': str(initial), 'tenant': 'default_tenant',
                                                'database': 'default_database', 'collection_id': str(collection.id),
                                                'provenance': 'managed_index_build_v1'}}
            if key == 'rag':
                ids, texts = ['one', 'two'], ['document one', 'document two']
                metadata = [{'document_id': str(item), 'user_id': user, 'chunk_index': 0}
                            for item, user in ((1, owner), (2, other))]
            else:
                ids, texts = [mine, theirs], ['old note', 'other note']
                metadata = [{'note_id': item, 'user_id': user} for item, user in ((mine, owner), (theirs, other))]
            collection.upsert(ids=ids, documents=texts, metadatas=metadata, embeddings=[[.123456789, 0], [.123456789, 0]])

    async def prepare(source, target, *, restore=False):
        return await prepare_deployment(fx.factory, legacy, initial, source, target, port,
                                        restore=restore, workspace=tmp_path)

    with local_vector_owner(tmp_path):
        with pytest.raises(VectorStoreBusy):
            await prepare(initial, remote)
    assert not remote.exists()
    proof = await prepare(initial, remote)
    assert [(item['count'], item['acl_query_cases']) for item in proof['collections']] == [(2, 2), (2, 2)]
    assert not any(text in json.dumps(proof) for text in ('old note', 'other note', owner))
    with pytest.raises(ValueError, match='destination must be absent'):
        await prepare(initial, remote)

    monkeypatch.setenv('RAG_CHROMA_HTTP_ROUTES', json.dumps({str(initial): port}))
    monkeypatch.setattr('app.rag.vector_store.get_chroma_config', lambda: legacy)
    monkeypatch.setattr(VectorStoreService, '_instance', None)
    vectors = VectorStoreService()
    monkeypatch.setattr(vectors, '_generate_embeddings', AsyncMock(return_value=[[.123456789, 0]]))

    @contextmanager
    def service():
        with serve_store(remote, port, workspace=tmp_path):
            yield

    with service():
        with pytest.raises(ChromaTransportError, match='offline restore'):
            open_chroma(remote)
        assert [item['chunk_id'] for item in await vectors.search_notes('probe', owner)] == [mine]
        assert [item['chunk_id'] for item in await vectors.search_documents('probe', other)] == ['two']
        async with fx.factory() as db, db.begin():
            active = await verify_index_switch(db, indexes, workspace=tmp_path)
        assert active['source_watermark'] == proof['source_watermark']
        assert all(item['transport'] == 'chroma_http' for item in active['targets'].values())
        with pytest.raises(VectorStoreBusy):
            await prepare(remote, restored, restore=True)
        await vectors.delete_note_vectors(mine, user_id=owner)
        async with fx.factory() as db, db.begin():
            with pytest.raises(BusinessError) as stale:
                await verify_index_switch(db, indexes, workspace=tmp_path)
        assert stale.value.data['reason'] == 'INDEX_SWITCH_NOT_CAUGHT_UP'
        fx.store.sql(notes.update().where(notes.c.id == mine).values(deleted_at=datetime(2026, 11, 21)))
        fx.store.sql(notes.insert().values(id=added, user_id=owner, title='new', content='new note'))
        await vectors.upsert_document(['new note'], [{'note_id': added, 'user_id': owner}], [added], collection='notes')
        audit = await reconcile(fx.factory, legacy, workspace=tmp_path)
        assert all(item['counts'] == {'valid_vector': 2} for item in audit['collections'])

    with pytest.raises(BusinessError) as unavailable:
        with bound_collection(indexes['notes']):
            pytest.fail('A stopped service must not fall back to the source store')
    assert unavailable.value.data['reason'] == 'INDEX_STORE_UNAVAILABLE'
    with pytest.raises(ChromaTransportError):
        await vectors.search_notes('probe', owner)

    rollback = await prepare(remote, restored, restore=True)
    assert rollback['source_watermark'] != proof['source_watermark']
    assert rollback['collections'][0]['contents_sha256'] != proof['collections'][0]['contents_sha256']
    monkeypatch.delenv('RAG_CHROMA_HTTP_ROUTES')
    backup = initial.with_name('embedded-backup')
    initial.rename(backup)
    restored.rename(initial)
    async with fx.factory() as db, db.begin():
        switched_back = await verify_index_switch(db, indexes, workspace=tmp_path)
    assert switched_back['source_watermark'] == rollback['source_watermark']
    assert [item['chunk_id'] for item in await vectors.search_notes('probe', owner)] == [added]
    if vectors._chroma_client:
        vectors._chroma_client.close()
    for directory, expected in ((backup, mine), (initial, added)):
        with PersistentClient(path=str(directory), settings=Settings(anonymized_telemetry=False)) as client:
            collection = client.get_collection('notes_collection', embedding_function=None)
            assert str(collection.id) == indexes['notes']['storage_binding']['collection_id']
            assert collection.get(where={'user_id': owner}, include=[])['ids'] == [expected]
