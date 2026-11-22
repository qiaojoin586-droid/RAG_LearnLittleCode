"""Durable candidate builds and delta fills against real isolated Chroma."""

from contextlib import asynccontextmanager
import copy
from datetime import timedelta
import hashlib
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.orm import Session

from app.core.failed_response import BusinessError
from app.model_config.index_build import IndexBuildService
from app.model_config.provider import ProviderGateway
from app.model_config.service import utcnow
from app.models.category import NoteCategory
from app.models.index_build import index_builds as jobs
from app.models.knowledge import KnowledgeDocument
from app.models.knowledge_ingest import knowledge_ingest_jobs
from app.models.note import Note
from app.models.note_index import note_index_jobs
from app.rag.index_build_worker import IndexBuildWorker
from app.rag.ingest_worker import _read_pages, _split_pages
from test_model_config_control_plane import AsyncFacade, admin, store  # noqa: F401
from test_model_config_publication import publish, selected, snapshot
from test_model_config_validation import execute, validation_service
from test_vector_binding_runtime import probe_response, vectors  # noqa: F401


def test_legacy_pdf_layout_is_preserved_for_rebuilding_committed_sources(tmp_path):
    import fitz

    path = tmp_path / 'legacy.pdf'
    with fitz.open() as pdf:
        for text in ['First original page.', 'Second original page.']:
            pdf.new_page().insert_text((50, 50), text)
        pdf.save(path)
    content = path.read_bytes()
    legacy = _split_pages(*_read_pages(path, None, legacy_md5=hashlib.md5(content, usedforsecurity=False).hexdigest()))
    extension, pages = _read_pages(path, hashlib.sha256(content).hexdigest())
    current = _split_pages(extension, pages)
    assert len(legacy) == 1 and all(text in legacy[0][0] for text in ('First original page.', 'Second original page.'))
    assert [metadata['page_number'] for _, metadata in current] == [1, 2]
    for text, span in current:
        assert dict(pages)[span['page_number']][span['char_start']:span['char_end']] == text


@pytest.mark.asyncio
async def test_managed_build_recovers_lease_and_catches_up_edits_and_deletes(vectors, tmp_path):
    fixture = vectors
    control = fixture.admin
    owner = control.actor.user_id
    notes, documents = Note.__table__, KnowledgeDocument.__table__
    for table in (NoteCategory.__table__, notes, documents, knowledge_ingest_jobs, note_index_jobs, jobs):
        table.create(control.engine)

    @asynccontextmanager
    async def factory():
        with Session(control.engine) as session:
            yield AsyncFacade(session)

    upload = tmp_path / 'uploads' / owner / 'legacy.md'
    upload.parent.mkdir(parents=True)
    file_bytes = b'# Scope\nA committed knowledge source.'
    upload.write_bytes(file_bytes)
    control.sql(documents.insert().values(id=42, user_id=owner, filename='legacy.md', file_path=str(upload),
        file_size=len(file_bytes), file_type='text/markdown', chunk_count=1,
        md5_hash=hashlib.md5(file_bytes, usedforsecurity=False).hexdigest()))
    keep, remove = str(uuid4()), str(uuid4())
    for identifier, body in [(keep, 'original private note'), (remove, 'later deleted note')]:
        control.sql(notes.insert().values(id=identifier, user_id=owner, title='fixture title', content=body))
    control.sql(notes.insert().values(id=str(uuid4()), user_id=owner, title='excluded',
                                      content='must stay out of the build', is_indexed=False))

    draft, _ = await control.call('create_draft', {'base_release_id': fixture.snapshot.release_id,
        'expected_head_version': fixture.published['head_version']}, 'managed-build-draft')
    identity = copy.deepcopy(fixture.plan['record']['semantic_identity'])
    identity['document_prefix'] = 'candidate: '
    body = {'draft_id': draft['id'], 'semantic_identity': identity}
    service = IndexBuildService(control.service, factory)
    async with factory() as db:
        with pytest.raises(BusinessError) as no_proof:
            await service.enqueue(db, control.actor, body, 'managed-build', None, draft['etag'])
    assert no_proof.value.data['reason'] == 'REAUTH_REQUIRED'
    async with factory() as db:
        admitted, status = await service.enqueue(db, control.actor, body, 'managed-build', control.proof(), draft['etag'])
    async with factory() as db:
        replay = await service.enqueue(db, control.actor, body, 'managed-build', control.proof(), draft['etag'])
    assert status == 202 and replay == (admitted, 202) and control.count(jobs) == 1

    sent = []

    def gateway_factory(profile, policy, key, *, permit, probe_mode=True):
        async def transport(request):
            if probe_mode or request.method == 'GET':
                return probe_response(request)
            payload = json.loads(request.content)
            inputs = payload['input']['texts'] if fixture.dashscope else payload['input']
            sent.extend(inputs)
            if fixture.native:
                return httpx.Response(200, json={'embeddings': [[1., 0.]] * len(inputs)})
            if fixture.dashscope:
                return httpx.Response(200, json={'output': {'embeddings': [
                    {'text_index': index, 'embedding': [1., 0.]} for index in range(len(inputs))]}})
            return httpx.Response(200, json={'data': [
                {'index': index, 'embedding': [1., 0.]} for index in range(len(inputs))]})

        return ProviderGateway(profile, policy, key, permit=permit, probe_mode=probe_mode,
                               transport=httpx.MockTransport(transport))

    def worker():
        return IndexBuildWorker(control.service, factory, root=tmp_path / 'candidate-builds',
                                uploads=tmp_path / 'uploads', gateway_factory=gateway_factory)

    first = await worker().claim()
    assert first['id'] == admitted['id'] and await worker().claim() is None
    past = utcnow() - timedelta(minutes=5)
    control.sql(jobs.update().values(lease_expires_at=past))
    assert await worker().tick()
    row = control.rows(jobs)[0]
    assert row['status'] == 'ready_to_switch', row['error_code']
    assert len(sent) == 3 and all(text.startswith('candidate: ') for text in sent)
    assert (await snapshot(control)).release_id == fixture.snapshot.release_id
    indexes = row['index_ids']
    original_watermark = row['source_watermark']

    draft = await control.call('get_draft', draft['id'])
    validator, _ = validation_service(control, probe_response)
    checked, _ = await execute(control, validator, draft, 'embedding', key='managed-build-verify')
    assert checked['status'] == 'passed'
    control.sql(notes.update().where(notes.c.id == keep).values(content='edited private note'))
    control.sql(notes.delete().where(notes.c.id == remove))
    with pytest.raises(BusinessError) as stale:
        await publish(control, draft, 'managed-build-publish')
    assert stale.value.data['reason'] == 'INDEX_SWITCH_NOT_CAUGHT_UP'

    control.sql(jobs.update().values(next_run_at=past))
    assert (await worker().claim())['status'] == 'catching_up'
    with pytest.raises(BusinessError) as syncing:
        await publish(control, draft, 'managed-build-publish')
    assert syncing.value.data['reason'] == 'INDEX_BUILD_NOT_READY'
    control.sql(jobs.update().values(lease_expires_at=past))
    assert await worker().tick()
    row = control.rows(jobs)[0]
    assert row['status'] == 'ready_to_switch' and row['index_ids'] == indexes
    assert row['source_watermark'] != original_watermark and sent[3:] == ['candidate: edited private note']
    released, _ = await publish(control, draft, 'managed-build-publish')
    latest = await snapshot(control)
    assert latest.release_id == released['release_id'] and control.rows(jobs)[0]['status'] == 'switched'
    assert await worker().tick() is False
    with selected(control, latest):
        pinned_notes = await fixture.service.search_notes('candidate snapshot', owner)
    assert [note['content'] for note in pinned_notes] == ['edited private note']
