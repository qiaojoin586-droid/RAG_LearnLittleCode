"""Maintain the searchable projection of committed document chunks."""

import asyncio

import sqlalchemy as sa

from app.core.failed_response import BusinessError, ErrorCode
from app.models.knowledge import knowledge_search_chunks as chunks
from app.rag.ingest import DOCUMENTS, UPLOAD_ROOT, lock_owner, scoped_upload_path
from app.rag.keyword_publication import publication_manifest
from app.rag.source_spans import read_span


async def replace_chunks(db, document_id, version, excerpts):
    await db.execute(chunks.delete().where(chunks.c.document_id == document_id))
    for offset in range(0, len(excerpts), 32):
        rows = [dict(document_id=document_id, ordinal=ordinal, resource_version_id=version, content=text)
                for ordinal, text in enumerate(excerpts[offset:offset + 32], start=offset)]
        await db.execute(chunks.insert().values(rows))


async def backfill_keyword_index(factory, user_id, document_id, *, root=UPLOAD_ROOT):
    from app.rag.ingest_worker import _read_pages

    document = DOCUMENTS.c
    query = sa.select(document.file_path, document.source_manifest, document.chunk_count).where(
        document.id == document_id, document.user_id == user_id, document.ready_for_retrieval.is_(True),
    )
    async with factory() as db:
        initial = (await db.execute(query)).one_or_none()
    if initial is None:
        raise BusinessError(ErrorCode.DOCUMENT_NOT_FOUND, http_status=404)
    manifest = initial.source_manifest
    if not manifest or not initial.chunk_count or len(manifest['chunks']) != initial.chunk_count:
        raise ValueError('The document requires a published source manifest')
    version = manifest['resource_version_id']
    path = scoped_upload_path(root, user_id, initial.file_path)

    def read_excerpts():
        _, pages = _read_pages(path, version)
        original = dict(pages)
        return [read_span(original, span) for span in manifest['chunks']]

    excerpts = await asyncio.to_thread(read_excerpts)
    async with factory() as db, db.begin():
        await lock_owner(db, user_id)
        if (await db.execute(query.with_for_update())).one_or_none() != initial:
            raise ValueError('The published document changed before keyword indexing')
        await replace_chunks(db, document_id, version, excerpts)
        await db.execute(DOCUMENTS.update().where(document.id == document_id, document.user_id == user_id)
                         .values(source_manifest=await publication_manifest(db, manifest)))
    return len(excerpts)
