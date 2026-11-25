"""Build and refresh candidate collections in the process that owns local Chroma."""

import asyncio
from contextlib import ExitStack, suppress
from datetime import timedelta
from pathlib import Path

import sqlalchemy as sa

from app.core.failed_response import BusinessError
from app.core.logger_handler import logger
from app.model_config.contracts import content_hash
from app.model_config.errors import fail
from app.model_config.index_build import IndexBuildService
from app.model_config.index_restore import restore_target
from app.model_config.legacy_import import _CLIActor
from app.model_config.probes import run_suite
from app.model_config.provider import ProviderFailure, ProviderGateway
from app.model_config.security import SCOPE, authorize
from app.model_config.service import etag, json_view, uid, utcnow
from app.model_config.vector_runtime import bound_collection, parse_vectors
from app.models.index_build import index_builds as jobs
from app.rag.index_build_sources import eligible_sources, read_chunks
from app.rag.blocking_io import finish_before_cancelling
from app.rag.ingest import UPLOAD_ROOT
from app.rag.maintenance_lock import WORKSPACE
from app.rag.reconcile import _hash, _read_sources


BUILD_ROOT = WORKSPACE / 'data' / 'index-builds'
LEASE = timedelta(seconds=180)
SYNC_INTERVAL = timedelta(seconds=15)
ACTIVE = ('queued', 'building', 'catching_up', 'ready_to_switch')
CHUNK_BATCH = 10
MAX_BATCHES_PER_PASS = 16
INVENTORY_BATCH = 200


async def _chroma_call(operation, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    return await finish_before_cancelling(task)


class IndexBuildWorker:
    def __init__(self, control, factory, *, root=BUILD_ROOT, uploads=UPLOAD_ROOT, gateway_factory=ProviderGateway):
        self.control = control
        self.factory = factory
        self.service = IndexBuildService(control, factory)
        self.root = Path(root).resolve()
        self.uploads = Path(uploads)
        self.gateway_factory = gateway_factory
        self.task = None

    async def claim(self):
        async with self.factory() as db, db.begin():
            now = await self.control._now(db)
            row = (await db.execute(sa.select(jobs).where(jobs.c.status.in_(ACTIVE), jobs.c.next_run_at <= now,
                sa.or_(jobs.c.lease_token.is_(None), jobs.c.lease_expires_at <= now)).order_by(
                    jobs.c.next_run_at, jobs.c.id).limit(1).with_for_update(skip_locked=True))).mappings().first()
            if row is None:
                return None
            values = {'status': 'catching_up' if row['collections'] else 'building',
                      'lease_token': uid(), 'lease_expires_at': now + LEASE, 'error_code': None}
            await db.execute(jobs.update().where(jobs.c.id == row['id']).values(**values))
            return {**row, **values}

    @staticmethod
    def _lease(job):
        return (jobs.c.id == job['id'], jobs.c.lease_token == job['lease_token'],
                jobs.c.status.in_(('building', 'catching_up')))

    async def _context(self, db, job, *, lock=False):
        now = await self.control._now(db)
        actor = _CLIActor(job['actor_id'], utcnow() + timedelta(seconds=30))
        historical = job['restore_release_id']
        permission = 'config.restore' if historical else 'config.publish'
        _, _, policy_version = await authorize(db, actor, permission, lock=lock)
        if policy_version != job['policy_version']:
            fail(40312, 'INDEX_BUILD_AUTHORIZATION_CHANGED', 403)
        head = await self.control._head(db, lock=lock)
        if historical:
            await self._require_lease(db, job, now, lock=lock)
            _, indexes, profile, policy, credential, fingerprint = await restore_target(
                self.control, db, historical, head['active_release_id'])
            if fingerprint != job['target_hash']:
                fail(40921, 'INDEX_BUILD_TARGET_CHANGED')
            return None, profile, policy, credential, indexes
        draft = await self.control._draft(db, job['draft_id'])
        if lock:
            draft = await self.control._draft(db, job['draft_id'], edit=True, match=etag(draft))
        if (draft['status'] not in ('editing', 'validating', 'ready') or draft['base_head_version'] != head['head_version']
                or (job['index_ids'] is None and draft['draft_version'] != job['draft_version'])):
            fail(40921, 'INDEX_BUILD_DRAFT_CHANGED')
        if job['index_ids'] is not None:
            current = {key: binding['index_version_id'] for key, binding in draft['payload']['index_bindings'].items()}
            if current != job['index_ids']:
                fail(40921, 'INDEX_BUILD_DRAFT_CHANGED')
        await self._require_lease(db, job, now, lock=lock)
        target, profile, policy, credential = await self.service.validation._target(db, draft, 'embedding')
        if content_hash(target) != job['target_hash']:
            fail(40921, 'INDEX_BUILD_TARGET_CHANGED')
        return draft, profile, policy, credential, None

    async def _require_lease(self, db, job, now, *, lock):
        query = sa.select(jobs.c.id).where(*self._lease(job), jobs.c.lease_expires_at > now)
        if (await db.execute(query.with_for_update() if lock else query)).first() is None:
            fail(40923, 'INDEX_BUILD_LEASE_LOST')

    async def _update(self, job, **values):
        async with self.factory() as db, db.begin():
            await db.execute(jobs.update().where(*self._lease(job)).values(**values))

    def _directory(self, job):
        directory = (self.root / job['id']).resolve()
        if directory.parent != self.root:
            fail(42220, 'INDEX_BUILD_STORAGE_INVALID', 422)
        return directory

    @staticmethod
    def _inventory(collection, historical):
        entries = {}
        include = ['metadatas', 'documents'] if historical else ['metadatas']
        for offset in range(0, collection.count(), INVENTORY_BATCH):
            batch = collection.get(limit=INVENTORY_BATCH, offset=offset, include=include)
            if historical:
                for identifier, metadata, document in zip(batch['ids'], batch['metadatas'], batch['documents'], strict=True):
                    entries[identifier] = {**(metadata or {}), '_document_digest': _hash(document) if isinstance(document, str) else None}
            else:
                entries.update(zip(batch['ids'], batch['metadatas'], strict=True))
        return entries

    @staticmethod
    def _current_vector(source, position, metadata, signature, historical):
        if not historical:
            return metadata.get('build_source') == signature
        if metadata.get('user_id') != source.state['user_id']:
            return False
        if source.key == 'notes':
            manifest = source.state.get('source_manifest')
            digest = source.state['digest']
            if manifest:
                if metadata.get('chunk_index') != position or manifest['resource_version_id'] != digest:
                    return False
                digest = manifest['chunks'][position]['text_sha256']
            return (metadata.get('note_id') == source.id and metadata.get('index_revision') == source.state['revision']
                    and metadata.get('_document_digest') == digest)
        manifest = source.state['source_manifest']
        if manifest is not None:
            return (str(metadata.get('document_id')) == source.id and metadata.get('chunk_index') == position
                    and metadata.get('source_sha256') == manifest['resource_version_id']
                    and metadata.get('_document_digest') == manifest['chunks'][position]['text_sha256'])
        return (str(metadata.get('document_id')) == source.id and metadata.get('chunk_index') == position
                and (source.state['source_sha256'] is None or metadata.get('source_sha256') == source.state['source_sha256'])
                and metadata.get('build_source', signature) == signature and metadata.get('_document_digest') is not None)

    async def _sync(self, collections, sources, profile, policy, api_key, permit, *, historical=False):
        batches = 0
        loop = asyncio.get_running_loop()
        yield_at = loop.time() + 60
        for key, source_list in sources.items():
            collection = collections[key]
            existing = await _chroma_call(self._inventory, collection, historical)
            expected = {source.vector_id(position) for source in source_list for position in range(source.count)}
            obsolete = sorted(existing.keys() - expected)
            for offset in range(0, len(obsolete), INVENTORY_BATCH):
                await permit()
                await _chroma_call(collection.delete, ids=obsolete[offset:offset + INVENTORY_BATCH])
            for source in source_list:
                signature = source.signature
                current = {position: existing.get(source.vector_id(position)) or {} for position in range(source.count)}
                missing = {position for position, metadata in current.items()
                           if not self._current_vector(source, position, metadata, signature, historical)}
                chunks = None
                if historical and key == 'rag' and source.state['source_sha256'] is None:
                    legacy = [position for position, metadata in current.items()
                              if position not in missing and 'build_source' not in metadata]
                    if legacy:
                        chunks = await read_chunks(self.factory, source, self.uploads)
                        missing.update(position for position in legacy
                                       if current[position]['_document_digest'] != _hash(chunks[position][0]))
                if not missing:
                    continue
                if chunks is None:
                    chunks = await read_chunks(self.factory, source, self.uploads)
                missing = sorted(missing)
                for offset in range(0, len(missing), CHUNK_BATCH):
                    positions = missing[offset:offset + CHUNK_BATCH]
                    texts = [chunks[position][0] for position in positions]
                    prefix = profile['semantic_identity']['document_prefix']
                    async with self.gateway_factory(profile, policy, api_key, permit=permit, probe_mode=False) as gateway:
                        await gateway.verify_embedding_revision()
                        response = await gateway.embed([prefix + text for text in texts], purpose='document')
                        await gateway.verify_embedding_revision()
                    embeddings = parse_vectors(response, profile['api_protocol'], len(texts), profile['embedding_dimension'],
                                               normalization=profile['semantic_identity']['normalization'])
                    await permit()
                    ids = [source.vector_id(pos) for pos in positions]
                    if historical:
                        # Replacements must clear metadata inherited from the old revision.
                        outdated = [identifier for identifier in ids if identifier in existing]
                        if outdated:
                            await _chroma_call(collection.delete, ids=outdated)
                    await _chroma_call(collection.upsert, ids=ids,
                        documents=texts, embeddings=embeddings,
                        metadatas=[{**chunks[pos][1], 'build_source': signature} for pos in positions])
                    batches += 1
                    if batches >= MAX_BATCHES_PER_PASS or loop.time() >= yield_at:
                        return False
        return True

    async def _finish(self, job, directory, state, watermark):
        from app.model_config.index_switch import _verify_collections
        from app.model_config import tables
        from app.model_config.security import scoped

        async with self.factory() as db, db.begin():
            draft, profile, _, _, indexes = await self._context(db, job, lock=True)
            current = await _read_sources(db)
            if content_hash(json_view(current)) != watermark:
                await db.execute(jobs.update().where(*self._lease(job)).values(
                    status='catching_up', lease_token=None, lease_expires_at=None, attempts=0))
                return
            eligible = eligible_sources(state)
            live = len(state['rag']) + sum(row['deleted_at'] is None and row['is_indexed'] for row in state['notes'].values())
            if sum(map(len, eligible.values())) != live:
                await db.execute(jobs.update().where(*self._lease(job)).values(status='catching_up',
                    next_run_at=await self.control._now(db) + SYNC_INTERVAL, lease_token=None, lease_expires_at=None,
                    error_code='INDEX_BUILD_WAITING_FOR_SOURCE_INDEX', attempts=0))
                return
            ids = job['index_ids'] or await self.service.attach(db, draft, job, profile, directory, job['collections'], watermark)
            if indexes is None:
                table = tables.rag_index_versions
                rows = (await db.execute(sa.select(table).where(scoped(table), table.c.id.in_(ids.values())))).mappings().all()
                indexes = {row['index_key']: row for row in rows}
            await asyncio.to_thread(_verify_collections, indexes, state, WORKSPACE)
            await db.execute(jobs.update().where(*self._lease(job)).values(status='ready_to_switch',
                index_ids=ids, draft_version=draft['draft_version'] if draft else job['draft_version'], source_watermark=watermark, attempts=0,
                next_run_at=await self.control._now(db) + SYNC_INTERVAL, lease_token=None, lease_expires_at=None))

    async def _execute(self, job):
        from app.rag.chroma_transport import opened_chroma, route_port

        async def permit():
            async with self.factory() as db:
                return await self._context(db, job)

        _, profile, policy, credential, indexes = await permit()
        async with self.factory() as db:
            state = await _read_sources(db)
        watermark = content_hash(json_view(state))
        if job['index_ids'] and job['source_watermark'] == watermark:
            await self._update(job, status='ready_to_switch', next_run_at=utcnow() + SYNC_INTERVAL,
                               lease_token=None, lease_expires_at=None, attempts=0)
            return
        await self._update(job, source_watermark=None)
        api_key = self.control.keyring.decrypt(credential['ciphertext'], credential['key_version'],
            {**SCOPE, 'id': credential['id'], 'origin': credential['allowed_origin'],
             'version': credential['credential_version']}) if credential else None
        if job['collections'] is None:
            async with self.gateway_factory(profile, policy, api_key, permit=permit) as gateway:
                check = await run_suite(gateway, 'embedding')
            if check['status'] != 'passed':
                fail(42222, 'INDEX_BUILD_EMBEDDING_PROBE_FAILED', 422)

        directory = None if indexes else self._directory(job)
        stack = ExitStack()
        def open_collections():
            if indexes:
                return {key: stack.enter_context(bound_collection(index)) for key, index in indexes.items()}
            if not job['collections'] and route_port(directory) is not None:
                fail(42220, 'INDEX_BUILD_REQUIRES_NEW_LOCAL_STORE', 422)
            client = stack.enter_context(opened_chroma(directory))
            open_collection = client.get_collection if job['collections'] else client.get_or_create_collection
            options = {} if job['collections'] else {'metadata': {'hnsw:space': 'cosine'}}
            return {key: open_collection(key + '_collection', embedding_function=None, **options) for key in ('rag', 'notes')}
        try:
            collections = await _chroma_call(open_collections)
            identities = {key: str(collection.id) for key, collection in collections.items()}
            if job['collections'] is not None and job['collections'] != identities:
                fail(42220, 'INDEX_BUILD_COLLECTION_REPLACED', 422)
            if job['collections'] is None:
                await self._update(job, collections=identities)
                job['collections'] = identities
            complete = await self._sync(collections, eligible_sources(state), profile, policy, api_key, permit,
                                        historical=indexes is not None)
        finally:
            await _chroma_call(stack.close)
        if not complete:
            await self._update(job, status='catching_up', next_run_at=utcnow(), lease_token=None,
                               lease_expires_at=None, attempts=0)
            return
        await self._finish(job, directory, state, watermark)

    async def tick(self):
        job = await self.claim()
        if job is None:
            return False
        try:
            async with asyncio.timeout(150):
                await self._execute(job)
        except (BusinessError, ProviderFailure, TimeoutError, OSError) as error:
            reason = 'INDEX_BUILD_FAILED'
            if isinstance(error, BusinessError):
                reason = (error.data or {}).get('reason', reason)
            elif isinstance(error, ProviderFailure):
                reason = error.reason
            transient = reason == 'INDEX_BUILD_SOURCE_CHANGED' or not isinstance(error, BusinessError)
            attempts = job['attempts'] + 1
            status = 'catching_up' if transient and attempts < 3 else 'failed'
            await self._update(job, status=status, attempts=attempts, error_code=reason,
                next_run_at=utcnow() + timedelta(seconds=2 ** attempts), lease_token=None, lease_expires_at=None)
        except Exception as error:
            logger.warning(f'候选索引构建失败，异常类型 {type(error).__name__}')
            await self._update(job, status='failed', error_code='INDEX_BUILD_FAILED', lease_token=None, lease_expires_at=None)
        return True

    async def run(self):
        while True:
            try:
                if await self.tick():
                    continue
            except sa.exc.SQLAlchemyError as error:
                logger.warning(f'候选索引任务轮询失败，异常类型 {type(error).__name__}')
            await asyncio.sleep(1)

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name='index_build_worker')

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
