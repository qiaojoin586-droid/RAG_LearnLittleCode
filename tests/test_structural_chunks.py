"""Structure, original offsets and publication through real note retrieval."""
from datetime import datetime
from hashlib import sha256
import json

import fitz
import pytest

from app.core.failed_response import BusinessError
from app.models.chat import ChatMessage
from app.models.knowledge import KnowledgeDocument, knowledge_search_chunks
from app.models.knowledge_ingest import knowledge_ingest_jobs
from app.models.note_index import note_index_jobs as jobs, note_search_chunks
from app.rag.citations import CitationTurn, confirm_citations, read_citation
from app.rag.index_build_sources import eligible_sources, read_chunks
from app.rag.ingest_worker import _read_pages, _split_pages, IngestFailure
from app.rag.keyword_search import KeywordSearch
from app.rag.note_index_worker import NoteIndexWorker
from app.rag.rag_service import RagService
from app.rag.reconcile import Binding, Location, _inspect_service, _read_sources
from app.rag.source_spans import hash_text, make_document_manifest, read_span
from app.rag.text_spliter import TextSplitter
from app.rag.visibility import CandidateVisibilityService
from test_model_config_control_plane import store
from test_note_index_worker import create, edit, note_vectors
from test_transactional_tool_operations import local_tools


def test_markdown_blocks_use_original_offsets_and_true_headings():
    fence = '~~~python\r\n# inside\r\nprint("汉")\r\n~~~\r\n'
    table = '| A | B |\r\n|---|---|\r\n| 1 | 2 |\r\n'
    source = '引言🙂\r\n\r\n###### 真标题\r\n段落\r\n\r\n' + fence + '\r\n' + table + '\r\n末段\r\n\r\n附录\r\n---\r\n正文'
    splitter = TextSplitter()
    chunks = splitter.split_with_positions(source, 'md')
    assert all(source[s['char_start']:s['char_end']] == text for text, s in chunks)
    assert [(s['block_kind'], text) for text, s in chunks if 'block_start' in s] == [('code', fence), ('table', table)]
    for text, span in chunks:
        if text in (fence, table, '末段'):
            assert span['section_title'] == '###### 真标题'
    assert chunks[-1][1]['section_title'] == '## 附录'
    texts, metadata = splitter.split_with_sections(source, 'md')
    assert texts == splitter.split_text(source, 'md') == [text for text, _ in chunks]
    assert [s['chunk_index'] for s in metadata] == list(range(len(chunks)))


@pytest.mark.parametrize(('block', 'kind'), [
    ('~~~\n' + 'line of code\n' * 12 + 'x' * 93 + '\n~~~\n', 'code'),
    ('| A | B |\n|---|---|\n' + '| data | repeated |\n' * 10 + '| ' + '行' * 110 + ' | 2 |\n', 'table'),
])
def test_oversize_blocks_preserve_every_original_character(block, kind):
    splitter = TextSplitter()
    splitter.chunk_size, splitter.chunk_overlap = 40, 8
    text = '# 示例\n\n' + block + '\n' + '无空格段' * 25
    chunks = splitter.split_with_positions(text, 'md')
    pieces = [(body, s) for body, s in chunks if s['block_kind'] == kind]
    assert len(pieces) > 2 and ''.join(body for body, _ in pieces) == block
    assert {(s['block_start'], s['block_end']) for _, s in pieces} == {(text.index(block), text.index(block) + len(block))}
    assert all(len(body) <= 40 and text[s['char_start']:s['char_end']] == body for body, s in chunks)
    assert pieces[0][0].endswith('\n')
    prose = [s for _, s in chunks if s['char_start'] > text.index(block) + len(block)]
    assert all(a['char_end'] > b['char_start'] for a, b in zip(prose, prose[1:]))
    assert prose[-1]['char_end'] == len(text)


def test_image_only_pdf_and_mixed_pdf_physical_page(tmp_path):
    path = tmp_path / 'scan.pdf'
    with fitz.open() as pdf:
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8))
        pix.clear_with(150)
        pdf.new_page().insert_image(fitz.Rect(10, 10, 50, 50), pixmap=pix)
        for add_text in (False, True):
            if add_text:
                pdf.new_page().insert_text((40, 40), 'Visible on page two.')
            payload = pdf.tobytes()
            path.write_bytes(payload)
            version = sha256(payload).hexdigest()
            extension, pages = _read_pages(path, version)
            if not add_text:
                with pytest.raises(IngestFailure, match='PDF_TEXT_REQUIRED'):
                    _split_pages(extension, pages)
                continue
            chunks = _split_pages(extension, pages)
            [span] = make_document_manifest(extension, version, chunks)['chunks']
            assert span['page_number'] == 2 and read_span(dict(pages), span) == 'Visible on page two.'
        encrypted = pdf.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='reader')
        path.write_bytes(encrypted)
        with pytest.raises(IngestFailure, match='PDF_PARSE_FAILED'):
            _read_pages(path, sha256(encrypted).hexdigest())


@pytest.mark.asyncio
async def test_long_note_tail_retrieval_citation_rebuild_and_shortening(local_tools, note_vectors, tmp_path):
    fx = local_tools
    for table in (KnowledgeDocument.__table__, knowledge_search_chunks, knowledge_ingest_jobs):
        table.create(fx.store.engine)
    marker = 'TAIL-947'
    content = '前文。' * 1500 + '\n\n## 尾部\n' + '末' * 880 + marker
    identifier = await create(fx, content)
    note_vectors._generate_embeddings.side_effect = lambda texts: [[1., 0.] if marker in s else [0., 1.] for s in texts]
    worker = NoteIndexWorker(fx.factory, vectors=note_vectors)
    assert await worker.tick()
    service = RagService(note_vectors, db_session_factory=fx.factory, enable_keyword=True, enable_summarize=False)
    result = await service.query(marker, fx.user_id, top_k=1, use_hyde=False)
    [hit] = result['sources']
    [block] = json.loads(result['context'])
    assert {p['method'] for p in hit['provenance']} == {'keyword', 'vector'}
    span = hit['source_locator']
    expected = content[span['char_start']:span['char_end']]
    assert span['ordinal'] > 0 and span['section_title'] == '## 尾部' and span['text_sha256'] == hash_text(expected)
    assert block == {'id': 'E1', 'text': expected} and expected.index(marker) > 800
    cited = CitationTurn(result['context'], result['sources'], delivered=True).cited('结论 [E1]')
    async with fx.factory() as db, db.begin():
        confirmed = await confirm_citations(db, fx.user_id, cited)
        assert confirmed == cited and 'E1' in confirmed
        await db.execute(ChatMessage.__table__.insert().values(id=9, session_id='session', role='assistant',
                                                               content='结论 [E1]', citations_json=confirmed))
        state = await _read_sources(db)
        [source] = eligible_sources(state)['notes']
    built = await read_chunks(fx.factory, source, tmp_path)
    vectors = note_vectors._notes_collection.get(include=['documents'])
    assert len(built) == len(vectors['ids']) == source.count
    assert dict(zip(vectors['ids'], vectors['documents'], strict=True)) == {
        source.vector_id(i): body for i, (body, _) in enumerate(built)}
    audit, repairs = _inspect_service(note_vectors._notes_collection,
        Location(tmp_path, 'default_tenant', 'default_database', 'notes'), Binding(keys={'notes'}),
        state['notes'], fx.user_id, collect_repairs=True)
    assert audit['counts'] == {'valid_vector': source.count} and repairs == []
    async with fx.factory() as db:
        assert (await read_citation(db, fx.user_id, 'session', 9, 'E1'))['content'] == expected
        with pytest.raises(BusinessError) as denied:
            await read_citation(db, fx.store.other.user_id, 'session', 9, 'E1')
        assert denied.value.http_status == 404

    second = await create(fx, 'another ' + marker)
    assert await worker.tick()
    fx.note_service.vector_store = note_vectors
    async with fx.factory() as db:
        found = await fx.note_service.semantic_search(db, fx.user_id, '前文', top_k=2)
        assert {note.id for note, _ in found} == {identifier, second}
    await edit(fx, identifier, 'short')
    async with fx.factory() as db:
        with pytest.raises(BusinessError) as old:
            await read_citation(db, fx.user_id, 'session', 9, 'E1')
        assert old.value.http_status == 404
    assert await worker.tick()
    assert note_vectors._notes_collection.get(where={'note_id': identifier})['documents'] == ['short']
    rows = await KeywordSearch(fx.factory).search_notes(marker, fx.user_id, 5)
    assert [r['metadata']['note_id'] for r in rows] == [second]


@pytest.mark.asyncio
async def test_partial_note_vector_batch_hidden_and_retry_replaces_it(local_tools, note_vectors, monkeypatch):
    fx = local_tools
    monkeypatch.setattr('app.rag.note_index_worker.BATCH_SIZE', 1)
    await create(fx, 'repeat ' * 600 + ' marker')
    writes = 0

    async def interrupt(texts):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError('simulated interrupted embedding')
        return [[1., 0.] for _ in texts]

    note_vectors._generate_embeddings.side_effect = interrupt
    worker = NoteIndexWorker(fx.factory, vectors=note_vectors)
    assert await worker.tick() and note_vectors._notes_collection.count() == 1
    [job] = fx.store.rows(jobs)
    assert job['status'] == 'queued' and not job['source_manifest'] and not fx.store.rows(note_search_chunks)
    partial = [dict(row, source='notes') for row in await note_vectors.search_notes('repeat', fx.user_id)]
    assert partial and not await CandidateVisibilityService(fx.factory).filter(partial, fx.user_id)
    keyword = KeywordSearch(fx.factory)
    assert not await keyword.search_notes('repeat', fx.user_id, 3)
    fx.store.sql(jobs.update().values(next_attempt_at=datetime(2000, 1, 1)))
    assert await worker.tick()
    [retried] = fx.store.rows(jobs)
    assert retried['revision'] == job['revision'] and retried['status'] == 'succeeded'
    count = len(retried['source_manifest']['chunks'])
    assert count == note_vectors._notes_collection.count() == len(fx.store.rows(note_search_chunks)) and count > 1
    assert await keyword.search_notes('marker', fx.user_id, 1)
