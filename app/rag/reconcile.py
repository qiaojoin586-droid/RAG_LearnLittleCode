"""Compare committed SQL sources with local Chroma collections and remove confirmed orphans."""

from collections import Counter, defaultdict
import asyncio
from dataclasses import dataclass, field
from hashlib import sha256
from itertools import groupby
from pathlib import Path
import sqlite3
from uuid import UUID

import sqlalchemy as sa

from app.core.failed_response import BusinessError
from app.model_config import tables as config
from app.model_config.index_binding import check_storage
from app.model_config.index_inventory import open_catalog_snapshot, storage_identity
from app.model_config.security import scoped
from app.model_config.snapshots import load_snapshot
from app.models.knowledge_ingest import knowledge_ingest_jobs
from app.models.note import Note
from app.models.note_index import note_index_jobs
from app.rag.chroma_transport import ChromaTransportError, http_chroma, opened_chroma, route_port, vector_io
from app.rag.ingest import DOCUMENTS
from app.rag.maintenance_lock import WORKSPACE, local_vector_owner
from app.rag.source_spans import hash_text, note_chunk_id


INDEXES = {'rag': ('rag_collection', 'document_id'), 'notes': ('notes_collection', 'note_id')}
SOURCE_TABLES = {'rag': DOCUMENTS, 'notes': Note.__table__}
REMOVABLE = {'orphan_vector', 'deleted_note_vector', 'disabled_note_vector'}
BATCH_SIZE = 200
SAMPLE_LIMIT = 20


@dataclass(frozen=True, order=True)
class Location:
    directory: Path
    tenant: str
    database: str
    name: str


@dataclass
class Binding:
    keys: set = field(default_factory=set)
    versions: dict = field(default_factory=dict)
    legacy: bool = False
    active: bool = False


@dataclass(frozen=True)
class VectorRow:
    identifier: str
    owner: str
    source_id: str


def _hash(text):
    return sha256(text.encode('utf-8')).hexdigest()


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def _source_id(key, value):
    if key == 'notes':
        return value if _uuid(value) else None
    value = str(value)
    if value.isascii() and value.isdigit() and len(value) <= 10 and 0 < int(value) <= 2147483647:
        return str(int(value))
    return None


async def _bindings(db, legacy):
    bindings = defaultdict(Binding)
    legacy_directory = Path(legacy.get('persist_directory', 'data/chroma')).resolve()
    legacy_locations = {
        key: Location(legacy_directory, 'default_tenant', 'default_database', legacy.get('collections', {}).get(key, name))
        for key, (name, _) in INDEXES.items()
    }
    snapshot = await load_snapshot(db)
    active_ids = set() if snapshot is None else {
        item['index_version_id'] for item in snapshot.manifest['index_bindings'].values()
    }
    for key, location in legacy_locations.items():
        item = bindings[location]
        item.keys.add(key)
        item.legacy = True
        item.active = snapshot is None

    table = config.rag_index_versions
    rows = await db.execute(sa.select(table).where(scoped(table), table.c.index_key.in_(INDEXES)).order_by(table.c.id))
    unresolved = []
    for row in rows.mappings():
        key, version = row['index_key'], row['id']
        try:
            storage = check_storage(row)
        except BusinessError:
            # Older inventory-only records can be associated with the configured legacy store.
            location = legacy_locations[key]
            identity = row['semantic_identity']
            observed = identity.get('observation') if isinstance(identity, dict) else None
            if (row['storage_binding'] is not None or not isinstance(observed, dict)
                    or identity.get('kind') != 'catalog_observation_v1'
                    or identity.get('store_id') != storage_identity(location.directory, location.tenant, location.database)
                    or observed.get('collection_name') != location.name or not observed.get('collection_id')):
                unresolved.append({'index_version_id': version, 'index_key': key, 'reason': 'storage_unresolved'})
                continue
            collection_id = observed['collection_id']
        else:
            location = Location(Path(storage['directory']).resolve(), storage['tenant'], storage['database'],
                                row['collection_name'])
            collection_id = storage['collection_id']
        item = bindings[location]
        item.keys.add(key)
        item.versions[version] = collection_id
        item.active |= version in active_ids
    return bindings, unresolved


async def _read_sources(db, *, lock=False):
    docs, notes = DOCUMENTS.c, Note.__table__.c
    ingest, index = knowledge_ingest_jobs.c, note_index_jobs.c
    doc_query = sa.select(docs.id, docs.user_id, docs.ready_for_retrieval, docs.chunk_count, docs.md5_hash,
        docs.source_manifest, ingest.source_sha256, ingest.status, ingest.operation).outerjoin(knowledge_ingest_jobs, sa.and_(
            docs.id == ingest.document_id, docs.user_id == ingest.user_id))
    note_query = sa.select(notes.id, notes.user_id, notes.deleted_at, notes.is_indexed, notes.title, notes.content,
        index.revision, index.status, index.operation, index.source_manifest).outerjoin(note_index_jobs, sa.and_(
            notes.id == index.note_id, notes.user_id == index.user_id)).order_by(notes.id).limit(BATCH_SIZE)
    if lock:
        doc_query = doc_query.order_by(docs.id).with_for_update()
        note_query = note_query.with_for_update()
    doc_rows = await db.execute(doc_query)
    note_states, last = {}, None
    while True:
        query = note_query if last is None else note_query.where(notes.id > last)
        batch = (await db.execute(query)).mappings().all()
        for row in batch:
            note_states[row['id']] = dict(user_id=row['user_id'], deleted_at=row['deleted_at'], revision=row['revision'],
                is_indexed=row['is_indexed'], status=row['status'], operation=row['operation'],
                digest=_hash(row['content'] or row['title']))
            if row['source_manifest'] is not None:
                note_states[row['id']]['source_manifest'] = row['source_manifest']
        if len(batch) < BATCH_SIZE:
            break
        last = batch[-1]['id']
    return {'rag': {str(row['id']): dict(row) for row in doc_rows.mappings()}, 'notes': note_states}


async def _read_job_counts(db, owner):
    counts = {}
    for key, jobs in [('rag', knowledge_ingest_jobs), ('notes', note_index_jobs)]:
        query = sa.select(jobs.c.operation, jobs.c.status, sa.func.count().label('count')).group_by(
            jobs.c.operation, jobs.c.status).order_by(jobs.c.operation, jobs.c.status)
        if owner is not None:
            query = query.where(jobs.c.user_id == owner)
        counts[key] = [dict(row) for row in (await db.execute(query)).mappings()]
    return counts


def _catalog_entries(db, collection_id, key):
    fields = ('user_id', 'document_id', 'source_sha256', 'chunk_index', 'chroma:document') if key == 'rag' else (
        'user_id', 'note_id', 'index_revision', 'chroma:document', 'chunk_index')
    rows = db.execute('''SELECT e.id, e.embedding_id, m.key, coalesce(m.string_value, m.int_value)
        FROM embeddings e JOIN segments s ON e.segment_id = s.id
        LEFT JOIN embedding_metadata m ON m.id = e.id AND m.key IN (?, ?, ?, ?, ?)
        WHERE s.collection = ? AND s.scope = 'METADATA' ORDER BY e.id''', (*fields, collection_id))
    for _, group in groupby(rows, key=lambda row: row[0]):
        _, identifier, name, value = next(group)
        metadata = {name: value, **{row[2]: row[3] for row in group}}
        yield identifier, metadata


def _classify(key, source, metadata, owner, seen):
    if source is None:
        return 'orphan_vector'
    if source['user_id'] != owner:
        return 'owner_mismatch'
    if key == 'notes' and source['deleted_at'] is not None:
        return 'deleted_note_vector'
    if key == 'notes' and not source['is_indexed']:
        return 'disabled_note_vector'
    if source['status'] in ('queued', 'running', 'failed'):
        return 'pending_or_failed_vector'
    if key == 'notes':
        text = metadata.get('chroma:document')
        manifest = source.get('source_manifest')
        chunk, digest = 0, source['digest']
        if manifest:
            chunk = metadata.get('chunk_index')
            if type(chunk) is not int or not 0 <= chunk < len(manifest['chunks']):
                return 'unexpected_chunk'
            if manifest['resource_version_id'] != digest:
                return 'stale_vector'
            digest = manifest['chunks'][chunk]['text_sha256']
        if (not isinstance(text, str) or _hash(text) != digest
                or metadata.get('index_revision') != source['revision']
                or source['revision'] and (source['status'] != 'succeeded' or source['operation'] != 'upsert')):
            return 'stale_vector'
    else:
        if not source['ready_for_retrieval']:
            return 'pending_or_failed_vector'
        if source['source_sha256'] and metadata.get('source_sha256') != source['source_sha256']:
            return 'stale_vector'
        chunk = metadata.get('chunk_index')
        if not isinstance(chunk, int) or not 0 <= chunk < source['chunk_count']:
            return 'unexpected_chunk'
        if source['source_manifest'] is not None:
            manifest = source['source_manifest']
            text = metadata.get('chroma:document')
            if (metadata.get('source_sha256') != manifest['resource_version_id']
                    or not isinstance(text, str) or chunk >= len(manifest['chunks'])
                    or hash_text(text) != manifest['chunks'][chunk]['text_sha256']):
                return 'stale_vector'
    if chunk in seen:
        return 'duplicate_chunk'
    seen.add(chunk)
    return 'valid_vector'


def _inspect(db, location, binding, sources, owner, *, collect_repairs):
    row = db.execute('''SELECT c.id FROM collections c JOIN databases d ON c.database_id=d.id
        WHERE c.name=? AND d.name=? AND d.tenant_id=?''', (location.name, location.database, location.tenant)).fetchone()
    return _check_vectors(location, binding, row[0] if row else None,
                          _catalog_entries(db, row[0], next(iter(binding.keys))) if row else (),
                          sources, owner, collect_repairs=collect_repairs)


def _service_entries(collection):
    expected = collection.count()
    for offset in range(0, expected, BATCH_SIZE):
        batch = collection.get(limit=BATCH_SIZE, offset=offset, include=['metadatas', 'documents'])
        if len(batch['ids']) != min(BATCH_SIZE, expected - offset):
            raise ChromaTransportError('Chroma inventory changed while reading')
        for identifier, metadata, document in zip(batch['ids'], batch['metadatas'], batch['documents'], strict=True):
            metadata = dict(metadata or {})
            metadata['chroma:document'] = document
            yield identifier, metadata
    if collection.count() != expected:
        raise ChromaTransportError('Chroma inventory changed while reading')


def _inspect_service(collection, location, binding, sources, owner, *, collect_repairs):
    return _check_vectors(location, binding, str(collection.id), _service_entries(collection),
                          sources, owner, collect_repairs=collect_repairs)


def _check_vectors(location, binding, collection_id, entries, sources, owner, *, collect_repairs):
    report = {'collection': location.name, 'index_keys': sorted(binding.keys), 'legacy': binding.legacy,
              'active': binding.active, 'index_version_ids': sorted(binding.versions),
              'store_id': storage_identity(location.directory, location.tenant, location.database)}
    if len(binding.keys) != 1:
        return {**report, 'status': 'ambiguous_binding'}, []
    if collection_id is None:
        return {**report, 'status': 'collection_missing'}, []
    report['collection_id'] = collection_id
    if any(expected != collection_id for expected in binding.versions.values()):
        return {**report, 'status': 'collection_identity_mismatch'}, []

    key = next(iter(binding.keys))
    field = INDEXES[key][1]
    seen, counts, candidates, samples = defaultdict(set), Counter(), [], []

    def count(reason, source_id):
        counts[reason] += 1
        if reason != 'valid_vector' and len(samples) < SAMPLE_LIMIT:
            samples.append({'reason': reason, 'source_id': source_id})

    for identifier, metadata in entries:
        vector_owner = metadata.get('user_id')
        source_id = _source_id(key, metadata.get(field))
        source = sources.get(source_id)
        if owner is not None and vector_owner != owner and (source is None or source['user_id'] != owner):
            continue
        if not _uuid(vector_owner) or source_id is None:
            count('invalid_metadata', source_id)
            continue
        reason = _classify(key, source, metadata, vector_owner, seen[source_id])
        if reason == 'valid_vector' and source.get('source_manifest') is not None:
            # Do not count a duplicate or mismatched vector ID as coverage.
            ordinal = metadata['chunk_index']
            expected_id = f'{source_id}_{ordinal}' if key == 'rag' else note_chunk_id(source_id, ordinal)
            if identifier != expected_id:
                seen[source_id].remove(ordinal)
                reason = 'unexpected_chunk'
        count(reason, source_id)
        if collect_repairs and vector_owner == owner and reason in REMOVABLE:
            candidates.append(VectorRow(identifier, vector_owner, source_id))

    for source_id, source in sources.items():
        if owner is not None and source['user_id'] != owner:
            continue
        if key == 'notes' and (source['deleted_at'] is not None or not source['is_indexed']):
            continue
        if source['status'] in ('queued', 'running', 'failed'):
            count(f"job_{source['status']}", source_id)
            continue
        if key == 'rag' and not source['ready_for_retrieval']:
            count('unpublished_source', source_id)
            continue
        expected = source.get('chunk_count', 1)
        if key == 'notes' and source.get('source_manifest'):
            expected = len(source['source_manifest']['chunks'])
        actual = len(seen.get(source_id, ()))
        if actual < expected:
            count('missing_source_vectors' if actual == 0 else 'partial_source_vectors', source_id)
    return {**report, 'status': 'checked', 'counts': dict(sorted(counts.items())), 'samples': samples}, candidates


async def _purge(factory, location, key, collection_id, rows):
    table = SOURCE_TABLES[key]
    columns = [table.c.id, table.c.user_id]
    if key == 'notes':
        columns.extend([table.c.deleted_at, table.c.is_indexed])
    removed = 0
    for offset in range(0, len(rows), BATCH_SIZE):
        batch = {row.identifier: row for row in rows[offset:offset + BATCH_SIZE]}
        async with factory() as db, db.begin():
            refs = {int(row.source_id) if key == 'rag' else row.source_id for row in batch.values()}
            result = await db.execute(sa.select(*columns).where(table.c.id.in_(refs)).with_for_update())
            sources = {str(item['id']): item for item in result.mappings()}
            removed += await vector_io(location.directory,
                lambda: _purge_batch(location, collection_id, key, batch, sources), write=True)
    return removed


def _purge_batch(location, collection_id, key, batch, sources):
    if route_port(location.directory) is None and not (location.directory / 'chroma.sqlite3').is_file():
        raise RuntimeError('Vector catalog disappeared during reconciliation')
    with opened_chroma(location.directory, location.tenant, location.database, validate=True) as client:
        collection = client.get_collection(location.name, embedding_function=None)
        if str(collection.id) != collection_id:
            raise RuntimeError('Collection identity changed during reconciliation')
        current = collection.get(ids=list(batch), include=['metadatas'])
        live_orphans = []
        for identifier, metadata in zip(current['ids'], current['metadatas'], strict=True):
            row = batch[identifier]
            if (not metadata or row.owner != metadata.get('user_id')
                    or row.source_id != _source_id(key, metadata.get(INDEXES[key][1]))):
                continue
            source = sources.get(row.source_id)
            if source is None or (key == 'notes' and source['user_id'] == row.owner
                                  and (source['deleted_at'] is not None or not source['is_indexed'])):
                live_orphans.append(identifier)
        if live_orphans:
            collection.delete(ids=live_orphans)
            if collection.get(ids=live_orphans, include=[])['ids']:
                raise RuntimeError('Chroma did not confirm removal of orphan vectors')
        return len(live_orphans)


def _scan_local(directory, workspace, entries, sources, owner, apply):
    with open_catalog_snapshot(directory, workspace) as (catalog, _):
        return [(location, *_inspect(catalog, location, binding, sources[next(iter(binding.keys))],
                                     owner, collect_repairs=apply)) for location, binding in entries]


def _scan_service(port, entries, sources, owner, apply):
    scans = []
    for location, binding in entries:
        with http_chroma(port, location.tenant, location.database) as client:
            collection = client.get_collection(location.name)
            scans.append((location, *_inspect_service(collection, location, binding, sources[next(iter(binding.keys))],
                                                      owner, collect_repairs=apply)))
    return scans


async def reconcile(factory, legacy, *, owner=None, apply=False, workspace=WORKSPACE):
    if (owner is not None and not _uuid(owner)) or (apply and owner is None):
        raise ValueError('Reconciliation apply requires one canonical owner UUID')
    workspace = Path(workspace).resolve()
    with local_vector_owner(workspace):
        async with factory() as db, db.begin():
            bindings, unresolved = await _bindings(db, legacy)
            sources = await _read_sources(db)
            jobs = await _read_job_counts(db, owner)

        directories = defaultdict(list)
        for location, binding in sorted(bindings.items()):
            directories[location.directory].append((location, binding))
        reports = []
        for directory, entries in directories.items():
            try:
                port = route_port(directory)
                if port is not None:
                    scans = await asyncio.to_thread(_scan_service, port, entries, sources, owner, apply)
                else:
                    scans = await vector_io(directory, lambda: _scan_local(directory, workspace, entries, sources, owner, apply),
                                            write=True)
            except (BusinessError, ChromaTransportError, OSError, sqlite3.Error) as error:
                reports.extend({'collection': location.name, 'index_keys': sorted(binding.keys), 'active': binding.active,
                                'index_version_ids': sorted(binding.versions), 'status': 'catalog_unavailable',
                                'store_id': storage_identity(directory, location.tenant, location.database),
                                'error_type': type(error).__name__} for location, binding in entries)
                continue
            for location, report, rows in scans:
                if apply and report['status'] == 'checked':
                    key = report['index_keys'][0]
                    report['purged_vectors'] = await _purge(factory, location, key, report['collection_id'], rows) if rows else 0
                reports.append(report)
        return {'version': 1, 'mode': 'purge_orphans' if apply else 'scan', 'owner': owner,
                'collections': reports, 'jobs': jobs, 'unresolved_versions': unresolved}
