"""SQL projection identity and coverage for the published document/note spans."""
import sqlalchemy as sa

from app.models.knowledge import KnowledgeDocument, knowledge_search_chunks
from app.models.note import Note
from app.models.note_index import note_index_jobs, note_search_chunks
from app.rag.source_spans import KEYWORD_REVISION, hash_text


async def index_revision(db):
    if db.get_bind().dialect.name == 'mysql':
        size = (await db.execute(sa.text('SELECT @@ngram_token_size'))).scalar_one()
        return f'{KEYWORD_REVISION}/mysql-ngram-{int(size)}-natural'
    return f'{KEYWORD_REVISION}/sqlite-literal'


def supported(manifest):
    revision = manifest['keyword_projection_revision'].as_string()
    return sa.or_(revision.is_(None), revision == KEYWORD_REVISION)


def analyzer_matches(manifest, revision):
    stamp = manifest['keyword_index_version_id'].as_string()
    # SQLite's literal lookup has no separately built tokenizer.
    if revision.endswith('/sqlite-literal'):
        return sa.or_(stamp.is_(None), stamp == revision)
    return sa.func.coalesce(stamp, '') == revision


async def publication_manifest(db, manifest):
    return {**manifest, 'keyword_projection_revision': KEYWORD_REVISION,
            'keyword_index_version_id': await index_revision(db)}


async def coverage_gap(db, owner, source, revision):
    """Detect absent or superseded projection rows even if no text matched."""
    if source == 'knowledge':
        docs, chunks = KnowledgeDocument.__table__.c, knowledge_search_chunks.c
        count = sa.select(sa.func.count()).where(
            chunks.document_id == docs.id, chunks.resource_version_id == docs.source_manifest['resource_version_id'].as_string(),
            chunks.ordinal >= 0, chunks.ordinal < docs.chunk_count).correlate(KnowledgeDocument.__table__).scalar_subquery()
        gap = sa.select(docs.id).where(docs.user_id == owner, docs.ready_for_retrieval.is_(True),
            sa.or_(docs.source_manifest.is_(None), ~supported(docs.source_manifest),
                   ~analyzer_matches(docs.source_manifest, revision), count != docs.chunk_count))
    else:
        notes, jobs, chunks = Note.__table__.c, note_index_jobs.c, note_search_chunks.c
        spans = jobs.source_manifest['chunks']
        size = sa.func.json_length(spans) if db.get_bind().dialect.name == 'mysql' else sa.func.json_array_length(spans)
        count = sa.select(sa.func.count()).where(chunks.note_id == notes.id, chunks.revision == jobs.revision,
            chunks.ordinal >= 0, chunks.ordinal < size).correlate(Note.__table__, note_index_jobs).scalar_subquery()
        gap = sa.select(notes.id).join(note_index_jobs, sa.and_(jobs.note_id == notes.id, jobs.user_id == notes.user_id)).where(
            notes.user_id == owner, notes.deleted_at.is_(None), notes.is_indexed.is_(True),
            jobs.status == 'succeeded', jobs.operation == 'upsert', jobs.source_manifest.is_not(None),
            sa.or_(~supported(jobs.source_manifest), ~analyzer_matches(jobs.source_manifest, revision), count != size))
    return (await db.execute(gap.limit(1))).first() is not None


async def verify_keyword_publication(db, sources):
    """Under the switch/restore locks, verify every searchable SQL span against the same source watermark."""
    from app.model_config.errors import fail

    report = {'index_revision': await index_revision(db), 'rag': 0, 'notes': 0, 'legacy_documents_unindexed': 0}
    for source, key in (('rag', 'document_id'), ('notes', 'note_id')):
        table = knowledge_search_chunks if source == 'rag' else note_search_chunks
        expected = {}
        for identifier, state in sources[source].items():
            manifest = state.get('source_manifest')
            if source == 'rag':
                if not state['ready_for_retrieval']:
                    continue
                if manifest is None:
                    report['legacy_documents_unindexed'] += 1
                    continue
                version = manifest['resource_version_id']
                if state['chunk_count'] != len(manifest['chunks']):
                    fail(42220, 'KEYWORD_PROJECTION_INCOMPLETE', 422, index_key=source)
            else:
                if (state['deleted_at'] or not state['is_indexed'] or
                        state['revision'] is not None and (state['status'] != 'succeeded' or state['operation'] != 'upsert')):
                    continue
                if manifest is None:
                    report['notes'] += 1  # legacy notes search the authorized SQL source row directly
                    continue
                version = state['revision']
                if manifest['resource_version_id'] != state['digest']:
                    fail(42220, 'KEYWORD_PROJECTION_INCOMPLETE', 422, index_key=source)
            if manifest.get('keyword_projection_revision', KEYWORD_REVISION) != KEYWORD_REVISION:
                fail(42220, 'KEYWORD_PROJECTION_INCOMPLETE', 422, index_key=source)
            recorded = manifest.get('keyword_index_version_id')
            if recorded != report['index_revision'] and (recorded is not None or db.get_bind().dialect.name == 'mysql'):
                fail(42220, 'KEYWORD_ANALYZER_UNVERIFIED', 422, index_key=source)
            expected[identifier] = (version, manifest['chunks'])
        ids = list(expected)
        version_col = table.c.resource_version_id if source == 'rag' else table.c.revision
        for offset in range(0, len(ids), 64):
            batch = ids[offset:offset + 64]
            seen = 0
            stmt = sa.select(table.c[key], table.c.ordinal, version_col, table.c.content).where(
                table.c[key].in_(batch)).order_by(table.c[key], table.c.ordinal).limit(256).with_for_update(read=True)
            page = stmt
            while True:
                rows = (await db.execute(page)).all()
                for ident, ordinal, version, text in rows:
                    active, spans = expected[str(ident)]
                    if (version != active or not 0 <= ordinal < len(spans)
                            or hash_text(text) != spans[ordinal]['text_sha256']):
                        fail(42220, 'KEYWORD_PROJECTION_INCOMPLETE', 422, index_key=source)
                seen += len(rows)
                if len(rows) < 256:
                    break
                last_id, last_ordinal = rows[-1][:2]
                page = stmt.where(sa.tuple_(table.c[key], table.c.ordinal) > (last_id, last_ordinal))
            if seen != sum(len(expected[i][1]) for i in batch):
                fail(42220, 'KEYWORD_PROJECTION_INCOMPLETE', 422, index_key=source)
            report[source] += seen
    return report
