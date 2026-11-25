"""Owner-scoped keyword recall over the published SQL sources."""

import re

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import match

from app.models.knowledge import KnowledgeDocument, knowledge_search_chunks
from app.models.note import Note
from app.models.note_index import note_index_jobs, note_search_chunks
from app.rag.keyword_publication import analyzer_matches, coverage_gap, index_revision, supported
from app.rag.ranking import FUSION_POLICY
from app.rag.source_spans import note_chunk_id


MAX_QUERY_CHARS = 512
MAX_HITS = FUSION_POLICY.per_source
_ENTITY = re.compile(
    r'(?<![A-Za-z0-9_])(?:[A-Za-z0-9]+(?:[._:/-][A-Za-z0-9]+)+|'
    r'[A-Za-z]+\d[A-Za-z0-9]*|[A-Z][A-Z0-9]{1,})(?![A-Za-z0-9_])'
)


class KeywordHits(list):
    """A source can have valid hits and an incomplete publication simultaneously."""
    def __init__(self, rows, revision, incomplete):
        super().__init__(rows)
        self.revision = revision
        self.incomplete = incomplete


class KeywordSearch:
    def __init__(self, session_factory=None):
        self.session_factory = session_factory

    def _session(self):
        if self.session_factory is not None:
            return self.session_factory()
        from app.db.database import async_session_factory
        return async_session_factory()

    async def _search(self, db, query, base, fields, keys, top_k, *, fulltext_allowed=sa.true()):
        term = query.strip()[:MAX_QUERY_CHARS]
        limit = min(MAX_HITS, top_k)
        if not term or limit < 1:
            return []
        entities = list(dict.fromkeys(_ENTITY.findall(term)))[:4]
        folded = [sa.func.lower(field) for field in fields]
        entity_matches = []
        for entity in entities:
            pattern = r'(^|[^a-z0-9_])' + re.escape(entity.lower()) + r'($|[^a-z0-9_])'
            entity_matches.append(sa.or_(*(field.regexp_match(pattern) for field in folded)))
        if entities == [term]:
            phrase = entity_matches[0]
        else:
            phrase = sa.or_(*(sa.func.instr(field, term.lower()) > 0 for field in folded))
        literal_rank = sa.case((phrase, 2), else_=0) + sum(sa.case((clause, 1), else_=0) for clause in entity_matches)
        orders = [base.selected_columns[key].asc() for key in keys]
        hits = {}
        literal = await db.execute(base.add_columns(literal_rank.label('keyword_score')).where(
            sa.or_(phrase, *entity_matches)).order_by(literal_rank.desc(), *orders).limit(limit))
        for row in literal.mappings():
            hits[tuple(row[key] for key in keys)] = (row, 'sql_literal')
        if len(hits) < limit and db.get_bind().dialect.name == 'mysql':
            relevance = match(*fields, against=term, in_natural_language_mode=True)
            indexed = await db.execute(base.add_columns(relevance.label('keyword_score')).where(
                fulltext_allowed, relevance > 0).order_by(relevance.desc(), *orders).limit(limit))
            for row in indexed.mappings():
                hits.setdefault(tuple(row[key] for key in keys), (row, 'mysql_fulltext_ngram'))
        return list(hits.values())[:limit]

    async def search_documents(self, query, user_id, top_k):
        docs, chunks = KnowledgeDocument.__table__.c, knowledge_search_chunks.c
        base = sa.select(chunks.document_id, chunks.ordinal, chunks.content, chunks.resource_version_id,
                         docs.filename, docs.user_id).select_from(knowledge_search_chunks.join(
            KnowledgeDocument.__table__, docs.id == chunks.document_id,
        )).where(
            docs.user_id == user_id, docs.ready_for_retrieval.is_(True),
            docs.source_manifest['resource_version_id'].as_string() == chunks.resource_version_id,
            chunks.ordinal >= 0, chunks.ordinal < docs.chunk_count, supported(docs.source_manifest),
        )
        async with self._session() as db:
            revision = await index_revision(db)
            rows = await self._search(db, query, base, [chunks.content], ['document_id', 'ordinal'], top_k,
                                      fulltext_allowed=analyzer_matches(docs.source_manifest, revision))
            incomplete = await coverage_gap(db, user_id, 'knowledge', revision)
        return KeywordHits([{
            'content': row['content'], 'chunk_id': f"{row['document_id']}_{row['ordinal']}",
            'algorithm': algorithm, 'keyword_score': float(row['keyword_score']),
            'metadata': {'user_id': row['user_id'], 'document_id': str(row['document_id']),
                         'filename': row['filename'], 'chunk_index': row['ordinal'],
                         'source_sha256': row['resource_version_id']},
        } for row, algorithm in rows], revision, incomplete)

    async def search_notes(self, query, user_id, top_k):
        notes, jobs = Note.__table__.c, note_index_jobs.c
        base = sa.select(notes.id, notes.user_id, notes.title, notes.content, jobs.revision).select_from(
            Note.__table__.outerjoin(note_index_jobs, sa.and_(jobs.note_id == notes.id, jobs.user_id == notes.user_id)),
        ).where(
            notes.user_id == user_id, notes.is_indexed.is_(True), notes.deleted_at.is_(None), jobs.source_manifest.is_(None),
            sa.or_(jobs.note_id.is_(None), sa.and_(jobs.status == 'succeeded', jobs.operation == 'upsert')),
        )
        chunks = note_search_chunks.c
        published = sa.select(notes.id, notes.user_id, notes.title, chunks.ordinal, chunks.content, jobs.revision).select_from(
            note_search_chunks.join(Note.__table__, notes.id == chunks.note_id).join(note_index_jobs, sa.and_(
                jobs.note_id == notes.id, jobs.user_id == notes.user_id, jobs.revision == chunks.revision)),
        ).where(notes.user_id == user_id, notes.is_indexed.is_(True), notes.deleted_at.is_(None),
                jobs.status == 'succeeded', jobs.operation == 'upsert', jobs.source_manifest.is_not(None),
                supported(jobs.source_manifest))
        async with self._session() as db:
            revision = await index_revision(db)
            legacy = await self._search(db, query, base, [notes.title, notes.content], ['id'], top_k)
            current = await self._search(db, query, published, [chunks.title, chunks.content], ['id', 'ordinal'], top_k,
                                         fulltext_allowed=analyzer_matches(jobs.source_manifest, revision))
            incomplete = await coverage_gap(db, user_id, 'notes', revision)
        # Interleave the two publication formats within each matching method.
        ranked = [(algorithm != 'sql_literal', rank, row['id'], row.get('ordinal', 0), row, algorithm)
                  for lane in (current, legacy) for rank, (row, algorithm) in enumerate(lane)]
        rows = sorted(ranked, key=lambda entry: entry[:4])[:min(MAX_HITS, top_k)]
        return KeywordHits([{
            'content': row['content'] or row['title'], 'chunk_id': note_chunk_id(row['id'], ordinal),
            'algorithm': algorithm, 'keyword_score': float(row['keyword_score']),
            'metadata': {'user_id': row['user_id'], 'note_id': row['id'],
                         'title': row['title'], 'chunk_index': ordinal, 'index_revision': row['revision']},
        } for _, _, _, ordinal, row, algorithm in rows], revision, incomplete)
