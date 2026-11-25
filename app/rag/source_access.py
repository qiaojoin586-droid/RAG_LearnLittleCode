"""Read published documents and notes under the current principal and version."""
import asyncio

from sqlalchemy import select

from app.core.failed_response import BusinessError, ErrorCode
from app.models.knowledge import KnowledgeDocument
from app.rag import ingest
from app.rag.ingest_worker import IngestFailure, _read_pages
from app.rag.note_index import load_visible_notes
from app.rag.source_spans import document_locator, note_locator, read_span


async def document_source(db, owner, identifier, ordinal, version, *, lock):
    query = select(KnowledgeDocument.filename, KnowledgeDocument.file_path, KnowledgeDocument.source_manifest).where(
        KnowledgeDocument.id == identifier, KnowledgeDocument.user_id == owner,
        KnowledgeDocument.ready_for_retrieval.is_(True),
    )
    row = (await db.execute(query.with_for_update(read=True) if lock else query)).one_or_none()
    manifest = row.source_manifest if row else None
    span = document_locator(identifier, manifest, ordinal) if manifest and manifest['resource_version_id'] == version else None
    if span is None:
        raise BusinessError(ErrorCode.DOCUMENT_NOT_FOUND, http_status=404)
    return row, span


async def note_source(db, owner, identifier, version, ordinal=None):
    row = (await load_visible_notes(db, owner, [identifier], lock=True)).get(identifier)
    if row:
        note, revision, status, operation, manifest = row
        locator = note_locator(note, revision) if ordinal is None else note_locator(note, revision, manifest, ordinal)
        if (revision is None or (status == 'succeeded' and operation == 'upsert')) and locator and locator['resource_version_id'] == version:
            return note, locator
    raise BusinessError(ErrorCode.NOTE_NOT_FOUND, http_status=404)


async def read_document_source(db, owner, identifier, ordinal, version, root=None):
    args = (db, owner, identifier, ordinal, version)
    row, locator = await document_source(*args, lock=False)
    try:
        path = ingest.scoped_upload_path(root if root is not None else ingest.UPLOAD_ROOT, owner, row.file_path)
        _, pages = await asyncio.to_thread(_read_pages, path, version)
        content = read_span(dict(pages), locator)
    except (OSError, ValueError, IngestFailure):
        raise BusinessError(ErrorCode.DOCUMENT_NOT_FOUND, http_status=404) from None
    current, current_locator = await document_source(*args, lock=True)
    if current_locator != locator or current.file_path != row.file_path:
        raise BusinessError(ErrorCode.DOCUMENT_NOT_FOUND, http_status=404)
    return {'title': current.filename, 'source_locator': locator, 'content': content}
