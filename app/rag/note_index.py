"""Transactional admission and read-side version checks for note vectors."""

from uuid import uuid4

from sqlalchemy import and_, func, select

from app.models.note import Note
from app.models.note_index import note_index_jobs as jobs
from app.rag.source_spans import hash_text, note_locator


async def queue_note_index(db, note: Note, operation: str) -> None:
    """Called while the transaction owns the source note's write lock."""
    values = dict(revision=str(uuid4()), operation=operation, status='queued', attempts=0,
                  next_attempt_at=func.current_timestamp(), lease_token=None, lease_expires_at=None, error_code=None)
    updated = await db.execute(jobs.update().where(
        jobs.c.note_id == note.id, jobs.c.user_id == note.user_id,
    ).values(**values))
    if updated.rowcount == 0:
        await db.execute(jobs.insert().values(note_id=note.id, user_id=note.user_id, **values))


async def retry_failed_note_index(db, user_id: str, note_id: str, revision: str) -> bool:
    """Requeue exactly the failed revision inspected by an operator."""
    result = await db.execute(jobs.update().where(
        jobs.c.user_id == user_id, jobs.c.note_id == note_id, jobs.c.revision == revision,
        jobs.c.status == 'failed',
    ).values(status='queued', attempts=0, next_attempt_at=func.current_timestamp(), error_code=None))
    return result.rowcount == 1


async def rebuild_note_index(db, user_id: str, note_id: str) -> bool:
    """Enqueue a new current revision for an owned note with no unfinished job."""
    note = await db.scalar(select(Note).where(
        Note.id == note_id, Note.user_id == user_id, Note.deleted_at.is_(None), Note.is_indexed.is_(True),
    ).with_for_update())
    if note is None:
        return False
    status = await db.scalar(select(jobs.c.status).where(
        jobs.c.note_id == note_id, jobs.c.user_id == user_id,
    ).with_for_update())
    if status not in (None, 'succeeded'):
        return False
    await queue_note_index(db, note, 'upsert')
    return True


async def load_visible_notes(db, user_id: str, note_ids, *, lock=False):
    query = select(Note, jobs.c.revision, jobs.c.status, jobs.c.operation, jobs.c.source_manifest).outerjoin(
        jobs, and_(jobs.c.note_id == Note.id, jobs.c.user_id == Note.user_id),
    ).where(Note.id.in_(note_ids), Note.user_id == user_id, Note.deleted_at.is_(None), Note.is_indexed.is_(True))
    rows = await db.execute(query.with_for_update(read=True).execution_options(populate_existing=True) if lock else query)
    return {row[0].id: row for row in rows}


def candidate_note_locator(candidate, row):
    if row is None:
        return None
    note, revision, status, operation, manifest = row
    metadata = candidate.get('metadata') or {}
    if (metadata.get('user_id') != note.user_id or metadata.get('index_revision') != revision
            or revision is not None and (status != 'succeeded' or operation != 'upsert')):
        return None
    locator = note_locator(note, revision, manifest, metadata.get('chunk_index', 0))
    if locator is None or manifest is not None and candidate.get('chunk_id') != locator['chunk_id']:
        return None
    text = candidate.get('content')
    if not isinstance(text, str) or hash_text(text) != locator['text_sha256']:
        return None
    return locator
