"""Database visibility checks before retrieved text reaches any downstream model."""
from __future__ import annotations

from sqlalchemy import select

from app.model_config.errors import fail
from app.model_config.runtime import verify_inference_actor
from app.model_config.snapshots import current_snapshot, inference_actor
from app.models.knowledge import KnowledgeDocument
from app.rag.note_index import load_visible_notes, candidate_note_locator
from app.rag.source_spans import document_locator, hash_text


class CandidateVisibilityService:
    def __init__(self, session_factory=None):
        self.session_factory = session_factory

    def session(self):
        if self.session_factory is not None:
            return self.session_factory()
        from app.db.database import async_session_factory
        return async_session_factory()

    async def authorize(self, user_id, db=None):
        actor = inference_actor.get()
        if not user_id or (actor is not None and actor.get('user_id') != user_id):
            fail(40314, 'RETRIEVAL_OWNER_MISMATCH', 403)
        if actor is None and current_snapshot() is None:
            return  # trusted internal caller using the explicit owner in local mode
        if db is None:
            async with self.session() as session:
                await verify_inference_actor(session, actor)
        else:
            await verify_inference_actor(db, actor)

    async def filter(self, candidates: list[dict], user_id: str) -> list[dict]:
        if not candidates:
            await self.authorize(user_id)
            return []
        document_ids, note_ids = set(), set()
        for candidate in candidates:
            metadata = candidate.get("metadata") or {}
            if metadata.get("user_id") != user_id:
                continue
            if candidate.get("source") == "knowledge":
                identifier = str(metadata.get("document_id", ""))
                if identifier.isascii() and identifier.isdigit() and len(identifier) <= 10:
                    document_ids.add(int(identifier))
            elif candidate.get("source") == "notes" and metadata.get("note_id"):
                note_ids.add(str(metadata["note_id"]))
        async with self.session() as db:
            await self.authorize(user_id, db)
            documents = {}
            notes = {}
            if document_ids:
                rows = await db.execute(select(KnowledgeDocument.id, KnowledgeDocument.source_manifest).where(
                    KnowledgeDocument.id.in_(document_ids), KnowledgeDocument.user_id == user_id,
                    KnowledgeDocument.ready_for_retrieval.is_(True),
                ))
                documents = {str(row.id): row.source_manifest for row in rows}
            if note_ids:
                notes = await load_visible_notes(db, user_id, note_ids)
            visible = []
            for candidate in candidates:
                metadata = candidate.get("metadata") or {}
                if metadata.get("user_id") != user_id:
                    continue
                locator = None
                if candidate.get("source") == "knowledge":
                    identifier = str(metadata.get("document_id"))
                    if identifier not in documents:
                        continue
                    manifest = documents[identifier]
                    if not manifest or metadata.get('source_sha256') != manifest['resource_version_id']:
                        continue
                    locator = document_locator(identifier, manifest, metadata.get('chunk_index'))
                    text = candidate.get('content')
                    if (locator is None or candidate.get('chunk_id') != locator['chunk_id']
                            or not isinstance(text, str) or hash_text(text) != locator['text_sha256']):
                        continue
                elif candidate.get("source") == "notes":
                    row = notes.get(str(metadata.get("note_id")))
                    locator = candidate_note_locator(candidate, row)
                    if locator is None:
                        continue
                else:
                    continue
                item = {**candidate}
                item.pop('source_locator', None)
                if locator is not None:
                    item['source_locator'] = locator
                visible.append(item)
            return visible
