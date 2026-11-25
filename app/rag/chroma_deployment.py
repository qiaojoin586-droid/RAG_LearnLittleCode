"""Prepare or serve a verified, dedicated Chroma volume on the local host."""

from argparse import ArgumentParser
import asyncio
from contextlib import contextmanager
import hashlib
import json
from math import isclose
import os
from pathlib import Path
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

from app.model_config.contracts import content_hash
from app.model_config.index_switch import lock_switch_sources
from app.model_config.service import json_view
from app.rag.chroma_transport import SERVICE_MARKER, ChromaTransportError, http_chroma
from app.rag.maintenance_lock import WORKSPACE, local_vector_owner, service_store_owner
from app.rag.reconcile import BATCH_SIZE, _bindings, _inspect_service


def _is_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _path(value, workspace):
    path = Path(os.path.abspath(value))
    if path == workspace or not path.is_relative_to(workspace):
        raise ValueError('Chroma deployment paths must be within the workspace')
    for parent in (path, *path.parents):
        if parent == workspace:
            break
        if os.path.lexists(parent) and _is_link(parent):
            raise ValueError('Chroma deployment paths cannot contain links')
    return path


def _disjoint(left, right):
    if left.is_relative_to(right) or right.is_relative_to(left):
        raise ValueError('Chroma source and destination must use separate directories')


def _store_files(directory):
    if not (directory / 'chroma.sqlite3').is_file():
        raise ValueError('Chroma SQLite file is missing')
    result = {}
    for path in sorted(directory.rglob('*')):
        if _is_link(path):
            raise ValueError('Chroma store contains a linked entry')
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError('Chroma store contains a special file')
        with path.open('rb') as handle:
            result[path.relative_to(directory).as_posix()] = hashlib.file_digest(handle, 'sha256').hexdigest()
    return result


@contextmanager
def serve_store(directory, port, *, workspace=WORKSPACE, quiet=True):
    if not 1024 <= port <= 65535:
        raise ValueError('Invalid Chroma service port')
    with socket.socket() as check:
        if os.name == 'nt':
            check.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        check.bind(('127.0.0.1', port))
    # The wheel's Windows console launcher starts another Python process. Invoke
    # the packaged server in our child directly so that it owns the listener.
    entry = """import sys
from chromadb_rust_bindings import cli
from app.rag.maintenance_lock import service_store_owner
directory, workspace, port = sys.argv[1:]
with service_store_owner(directory, workspace):
    cli(['chroma', 'run', '--path', directory, '--host', '127.0.0.1', '--port', port])
"""
    output = subprocess.DEVNULL if quiet else None
    with subprocess.Popen([sys.executable, '-c', entry, str(directory), str(workspace), str(port)],
                          stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0) as process:
        try:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise ChromaTransportError('Chroma process did not become ready')
                try:
                    with http_chroma(port) as client:
                        client.request('GET', 'heartbeat')
                    break
                except ChromaTransportError:
                    time.sleep(.1)
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def _observe(collection, dimension):
    count = collection.count()
    if (collection.metadata or {}).get('hnsw:space') != 'cosine':
        raise ValueError('Chroma collection uses an unexpected metric')
    checksums, owners, probes = {}, {}, {}
    for offset in range(0, count, BATCH_SIZE):
        batch = collection.get(limit=BATCH_SIZE, offset=offset, include=['embeddings', 'documents', 'metadatas'])
        for identifier, vector, document, metadata in zip(batch['ids'], batch['embeddings'], batch['documents'],
                                                         batch['metadatas'], strict=True):
            vector = list(map(float, vector))
            owner = (metadata or {}).get('user_id')
            if identifier in checksums or not isinstance(owner, str) or not owner or len(vector) != dimension:
                raise ValueError('Chroma record has missing ownership, invalid dimension or duplicate identity')
            vector_bytes = struct.pack(f'<{len(vector)}f', *vector)
            checksums[identifier] = content_hash([hashlib.sha256(vector_bytes).hexdigest(), document, metadata])
            owners.setdefault(owner, set()).add(identifier)
            candidates = probes.setdefault(owner, {})
            candidates[identifier] = vector
            if len(candidates) > 2:
                del candidates[max(candidates)]
    if len(checksums) != count or collection.count() != count:
        raise ValueError('Chroma record count changed during validation')
    return {'collection_id': str(collection.id), 'dimension': dimension, 'count': count,
            'contents_sha256': content_hash(checksums)}, owners, probes


def _observe_queries(collection, owners, probes):
    results = []
    for owner in sorted(owners)[:16]:
        where = {'user_id': owner}
        for identifier, vector in sorted(probes[owner].items()):
            response = collection.query(query_embeddings=[vector], where=where, n_results=5,
                                        include=['distances', 'metadatas'])
            hits = response['ids'][0]
            distances = response['distances'][0]
            metadata = response['metadatas'][0]
            if (len(hits) != min(5, len(owners[owner])) or len(set(hits)) != len(hits)
                    or len(hits) != len(distances) or len(hits) != len(metadata)
                    or not set(hits).issubset(owners[owner])
                    or any(item.get('user_id') != owner for item in metadata)):
                raise ValueError('Chroma ownership filter or result shape differs')
            results.append((hits, distances))
    return results


def _observe_bindings(entries, sources, open_client, dimension_of):
    result = {}
    for location, binding in entries:
        with open_client(location) as client:
            collection = client.get_collection(location.name, embedding_function=None)
            if len(binding.keys) != 1 or any(expected != str(collection.id) for expected in binding.versions.values()):
                raise ValueError('Chroma collection binding is inconsistent')
            if binding.active:
                key = next(iter(binding.keys))
                report, _ = _inspect_service(collection, location, binding, sources[key], None, collect_repairs=False)
                if report['status'] != 'checked' or set(report['counts']) - {'valid_vector'}:
                    raise ValueError('Active Chroma collection has not caught up to the SQL watermark')
            observed, owners, probes = _observe(collection, dimension_of(collection))
            result[location] = (observed, _observe_queries(collection, owners, probes))
    return result


def _compare(left, right):
    reports = []
    for location, (before, before_queries) in left.items():
        after, after_queries = right[location]
        if before != after or len(before_queries) != len(after_queries):
            raise ValueError('Chroma collection identity, count or content changed during migration')
        for (before_ids, before_scores), (after_ids, after_scores) in zip(before_queries, after_queries, strict=True):
            if before_ids != after_ids or any(not isclose(a, b, abs_tol=1e-6, rel_tol=1e-6)
                                              for a, b in zip(before_scores, after_scores, strict=True)):
                raise ValueError('Chroma filtered retrieval results changed during migration')
        reports.append({'collection': location.name, 'tenant': location.tenant, 'database': location.database,
                        **after, 'acl_query_cases': len(after_queries)})
    return reports


def _require_marker(directory, logical):
    marker = directory / SERVICE_MARKER
    if not marker.is_file() or _is_link(marker):
        raise ValueError('Chroma service volume has no verified deployment marker')
    try:
        value = json.loads(marker.read_text(encoding='utf8'))
    except (OSError, ValueError) as error:
        raise ValueError('Chroma service volume has an invalid marker') from error
    if value != {'version': 1, 'logical_store': str(logical)}:
        raise ValueError('Chroma service volume belongs to a different logical store')


def _install_volume(staged, target):
    for attempt in range(5):
        if os.path.lexists(target):
            raise FileExistsError('Chroma destination already exists')
        try:
            staged.rename(target)
            return
        except PermissionError:
            # Windows can briefly retain a handle after the server exits.
            if os.name != 'nt' or attempt == 4:
                raise
            time.sleep(.2)


def _prepare_volume(source, target, port, entries, source_state, logical, workspace, restore):
    from chromadb import PersistentClient
    from chromadb.config import Settings

    def embedded(location):
        return PersistentClient(path=str(source), tenant=location.tenant, database=location.database,
                                settings=Settings(anonymized_telemetry=False, migrations='validate'))

    _store_files(source)
    expected = _observe_bindings(entries, source_state, embedded, lambda collection: collection.get_model().dimension)
    files = _store_files(source)
    with tempfile.TemporaryDirectory(prefix='chroma-transfer-', dir=target.parent) as temporary:
        staged = Path(temporary) / 'store'
        shutil.copytree(source, staged)
        if _store_files(staged) != files or _store_files(source) != files:
            raise ValueError('Chroma source changed while making the offline copy')
        marker = staged / SERVICE_MARKER
        if restore:
            marker.unlink()
        else:
            marker.write_text(json.dumps({'version': 1, 'logical_store': str(logical)}), encoding='utf8')
        with serve_store(staged, port, workspace=workspace):
            observed = _observe_bindings(entries, source_state,
                lambda location: http_chroma(port, location.tenant, location.database), lambda collection: collection.dimension)
            reports = _compare(expected, observed)
        _install_volume(staged, target)
    return {'files': len(files), 'source_storage_sha256': content_hash(files), 'collections': reports}


async def prepare_deployment(factory, legacy, logical, source, target, port, *, restore=False, workspace=WORKSPACE):
    workspace = Path(workspace).resolve()
    logical, source, target = (_path(value, workspace) for value in (logical, source, target))
    _disjoint(source, target)
    if logical != target:
        _disjoint(logical, target)
    if logical != source:
        _disjoint(logical, source)
    with local_vector_owner(workspace), service_store_owner(source, workspace), service_store_owner(target, workspace):
        if os.path.lexists(target) or not target.parent.is_dir():
            raise ValueError('Chroma destination must be absent and its parent must exist')
        if restore:
            _require_marker(source, logical)
        else:
            _disjoint(logical, target)
            if source != logical or os.path.lexists(source / SERVICE_MARKER):
                raise ValueError('Chroma cutover requires the original embedded store')
        async with factory() as db, db.begin():
            source_state = await lock_switch_sources(db)
            from app.rag.keyword_publication import verify_keyword_publication
            keyword = await verify_keyword_publication(db, source_state)
            bindings, unresolved = await _bindings(db, legacy)
            entries = [(location, binding) for location, binding in sorted(bindings.items())
                       if location.directory == logical]
            if not entries or unresolved:
                raise ValueError('Chroma deployment requires resolved SQL index bindings')
            transfer = asyncio.create_task(asyncio.to_thread(
                _prepare_volume, source, target, port, entries, source_state, logical, workspace, restore))
            try:
                report = await asyncio.shield(transfer)
            except asyncio.CancelledError:
                await asyncio.gather(transfer, return_exceptions=True)
                raise
            return {**report, 'version': 1, 'source_watermark': content_hash(json_view(source_state)),
                    'keyword': keyword, 'port': port}


async def _prepare(args):
    from dotenv import load_dotenv

    load_dotenv()
    from app.db.database import async_session_factory, close_db
    from app.utils.config import get_chroma_config

    try:
        return await prepare_deployment(async_session_factory, get_chroma_config(), args.logical_store,
                                        args.source, args.target, args.port, restore=args.restore)
    finally:
        await close_db()


def main():
    parser = ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', help='Copy a stopped store and verify the copy over HTTP')
    prepare.add_argument('--source', type=Path, required=True)
    prepare.add_argument('--target', type=Path, required=True)
    prepare.add_argument('--restore', action='store_true', help='Prepare an embedded copy from the stopped service volume')
    server = commands.add_parser('serve', help='Run a dedicated Chroma volume until interrupted')
    server.add_argument('--data-dir', type=Path, required=True)
    for command in (prepare, server):
        command.add_argument('--logical-store', type=Path, required=True)
        command.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('--port must be between 1024 and 65535')
    try:
        if args.command == 'prepare':
            print(json.dumps(asyncio.run(_prepare(args)), indent=2))
        else:
            logical = _path(args.logical_store, WORKSPACE)
            directory = _path(args.data_dir, WORKSPACE)
            _disjoint(logical, directory)
            _require_marker(directory, logical)
            with serve_store(directory, args.port, quiet=False) as process:
                print(f'Chroma ready on 127.0.0.1 port {args.port}', flush=True)
                return process.wait()
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(json.dumps({'error': type(error).__name__,
                          'reason': getattr(error, 'message', None)
                                    or (str(error) if type(error) is ValueError else 'CHROMA_DEPLOYMENT_FAILED')}))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
