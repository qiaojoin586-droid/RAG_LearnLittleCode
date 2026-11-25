"""Local Chroma and the loopback Chroma v2 service transport."""

from contextlib import ExitStack, contextmanager
from functools import lru_cache
import json
import os
from pathlib import Path
import re
from threading import RLock
from urllib.parse import quote
from uuid import UUID

import httpx
from app.rag.blocking_io import BlockingPool


SERVICE_MARKER = '.raglearn-chroma-service.json'
# Embedded client creation/close shares process-global Chroma state.
_embedded_io = BlockingPool('chroma-local', workers=1, queued=32)
_http_io = BlockingPool('chroma-http', workers=4, queued=16)
_client_lifecycle = RLock()


class ChromaTransportError(RuntimeError):
    pass


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate Chroma route')
        result[key] = value
    return result


@lru_cache(maxsize=8)
def _routes(raw):
    try:
        routes = json.loads(raw, object_pairs_hook=_unique_fields)
        if not isinstance(routes, dict):
            raise ValueError
        mapped = {}
        for directory, port in routes.items():
            if not directory.strip() or type(port) is not int or not 1024 <= port <= 65535:
                raise ValueError
            path = Path(directory).resolve()
            if path in mapped or port in mapped.values():
                raise ValueError
            mapped[path] = port
        return mapped
    except (ValueError, OSError, RuntimeError) as error:
        raise ChromaTransportError('Invalid RAG_CHROMA_HTTP_ROUTES') from error


def route_port(directory):
    if directory is None:
        return None
    routes = _routes(os.environ.get('RAG_CHROMA_HTTP_ROUTES', '{}'))
    return routes.get(Path(directory).resolve()) if routes else None


def validate_routes():
    _routes(os.environ.get('RAG_CHROMA_HTTP_ROUTES', '{}'))


def _segment(value):
    if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]*', value):
        raise ChromaTransportError('Invalid Chroma namespace')
    return quote(value, safe='')


class HttpChroma:
    def __init__(self, session, tenant, database):
        self.session = session
        self.namespace = f'tenants/{_segment(tenant)}/databases/{_segment(database)}/collections'

    def request(self, method, path, **kwargs):
        try:
            response = self.session.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ChromaTransportError('Chroma service request failed') from error

    def get_collection(self, name, *, embedding_function=None):
        model = self.request('GET', f'{self.namespace}/{_segment(name)}')
        try:
            return HttpCollection(self, model)
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ChromaTransportError('Chroma service returned an invalid collection') from error


class HttpCollection:
    def __init__(self, client, model):
        self.client = client
        self.id = UUID(model['id'])
        self.metadata = model.get('metadata')
        self.dimension = model.get('dimension')
        self.path = f'{client.namespace}/{self.id}'

    def count(self):
        return self.client.request('GET', f'{self.path}/count')

    def get(self, **payload):
        return self.client.request('POST', f'{self.path}/get', json=payload)

    def query(self, **payload):
        return self.client.request('POST', f'{self.path}/query', json=payload)

    def upsert(self, **payload):
        self.client.request('POST', f'{self.path}/upsert', json=payload)

    def delete(self, **payload):
        self.client.request('POST', f'{self.path}/delete', json=payload)


@contextmanager
def http_chroma(port, tenant='default_tenant', database='default_database'):
    with httpx.Client(base_url=f'http://127.0.0.1:{port}/api/v2/', trust_env=False,
                      timeout=httpx.Timeout(10, connect=2)) as session:
        yield HttpChroma(session, tenant, database)


def open_chroma(directory, tenant='default_tenant', database='default_database', *, validate=False):
    port = route_port(directory)
    if port is not None:
        return http_chroma(port, tenant, database)
    if (Path(directory) / SERVICE_MARKER).exists():
        raise ChromaTransportError('A Chroma service volume requires an offline restore before embedded use')
    from chromadb import PersistentClient
    from chromadb.config import Settings

    options = {'migrations': 'validate'} if validate else {}
    with _client_lifecycle:
        return PersistentClient(path=str(directory), tenant=tenant, database=database,
                                settings=Settings(anonymized_telemetry=False, **options))


@contextmanager
def opened_chroma(*args, **kwargs):
    # Maintenance and foreground readers may open the same local SDK system.
    stack = ExitStack()
    with _client_lifecycle:
        client = stack.enter_context(open_chroma(*args, **kwargs))
    try:
        yield client
    finally:
        with _client_lifecycle:
            stack.close()


async def vector_io(directory, operation, *, write=False):
    pool = _embedded_io if route_port(directory) is None else _http_io
    return await pool.run(operation, finish_on_cancel=write)
