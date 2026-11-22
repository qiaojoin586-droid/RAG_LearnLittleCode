"""Read committed sources for an isolated candidate build."""

import asyncio
from dataclasses import dataclass

import sqlalchemy as sa

from app.model_config.contracts import content_hash
from app.model_config.errors import fail
from app.models.note import Note
from app.models.user import User
from app.rag.ingest import DOCUMENTS, scoped_upload_path
from app.rag.ingest_worker import IngestFailure, _read_pages, _split_pages
from app.rag.reconcile import _hash
from app.rag.source_spans import note_chunk_id, read_span


@dataclass(frozen=True)
class Source:
    key: str
    id: str
    state: dict

    @property
    def count(self):
        if self.key == 'rag':
            return self.state['chunk_count']
        manifest = self.state.get('source_manifest')
        return len(manifest['chunks']) if manifest else 1

    @property
    def signature(self):
        if self.key == 'rag':
            material = [self.state['chunk_count'], self.state['md5_hash'], self.state['source_sha256']]
            if self.state['source_manifest'] is not None:
                material.append(content_hash(self.state['source_manifest']))
        else:
            material = [self.state['digest'], self.state['revision']]
            if self.state.get('source_manifest'):
                material.append(content_hash(self.state['source_manifest']))
        return content_hash([self.key, self.id, self.state['user_id'], *material])

    def vector_id(self, position):
        return f'{self.id}_{position}' if self.key == 'rag' else note_chunk_id(self.id, position)


def eligible_sources(state):
    return {
        'rag': [Source('rag', identifier, row) for identifier, row in state['rag'].items()
                if row['ready_for_retrieval'] and row['status'] in (None, 'succeeded')],
        'notes': [Source('notes', identifier, row) for identifier, row in state['notes'].items()
                  if row['deleted_at'] is None and row['is_indexed'] and row['status'] in (None, 'succeeded')
                  and row['operation'] in (None, 'upsert')],
    }


async def read_chunks(factory, source, upload_root):
    table = DOCUMENTS if source.key == 'rag' else Note.__table__
    identifier = int(source.id) if source.key == 'rag' else source.id
    async with factory() as db:
        result = await db.execute(sa.select(table, User.status.label('account_status')).join(User,
            User.uuid == table.c.user_id).where(table.c.id == identifier, table.c.user_id == source.state['user_id']))
        row = result.mappings().first()
    if row is None:
        fail(40923, 'INDEX_BUILD_SOURCE_CHANGED')
    if row['account_status'] != 'active':
        fail(42220, 'INDEX_BUILD_SOURCE_OWNER_INACTIVE', 422, index_key=source.key)

    owner = row['user_id']
    if source.key == 'notes':
        text = row['content'] or row['title']
        if row['deleted_at'] is not None or not row['is_indexed'] or _hash(text) != source.state['digest']:
            fail(40923, 'INDEX_BUILD_SOURCE_CHANGED')
        metadata = {'user_id': owner, 'note_id': source.id, 'title': row['title'], 'category': row['category'] or ''}
        if source.state['revision']:
            metadata['index_revision'] = source.state['revision']
        manifest = source.state.get('source_manifest')
        if manifest:
            if manifest['resource_version_id'] != source.state['digest']:
                fail(40923, 'INDEX_BUILD_SOURCE_CHANGED')
            return [(read_span({None: text}, span), {**metadata, **span, 'chunk_index': i})
                    for i, span in enumerate(manifest['chunks'])]
        return [(text, metadata)]

    if (not row['ready_for_retrieval'] or row['md5_hash'] != source.state['md5_hash']
            or row['chunk_count'] != source.count or row['source_manifest'] != source.state['source_manifest']):
        fail(40923, 'INDEX_BUILD_SOURCE_CHANGED')
    try:
        path = scoped_upload_path(upload_root, owner, row['file_path'])
        manifest = row['source_manifest']
        digest = manifest['resource_version_id'] if manifest else source.state['source_sha256']
        extension, pages = await asyncio.to_thread(_read_pages, path, digest, legacy_md5=row['md5_hash'])
        if manifest:
            original_pages = dict(pages)
            chunks = [(read_span(original_pages, span), span) for span in manifest['chunks']]
        else:
            chunks = await asyncio.to_thread(_split_pages, extension, pages, legacy=True)
    except (ValueError, OSError, IngestFailure):
        fail(42220, 'INDEX_BUILD_DOCUMENT_SOURCE_INVALID', 422)
    if len(chunks) != source.count:
        fail(42220, 'INDEX_BUILD_CHUNK_COUNT_CHANGED', 422)
    metadata = {'user_id': owner, 'document_id': source.id, 'filename': row['filename']}
    if digest:
        metadata['source_sha256'] = digest
    return [(text, {**item, **metadata, 'chunk_index': position}) for position, (text, item) in enumerate(chunks)]
