"""Request-pinned vector models, permission checks and semantic cache partitions."""
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
import hashlib
import math
from pathlib import Path
import threading
import time

from app.model_config import runtime
from app.model_config.contracts import content_hash
from app.model_config.errors import fail
from app.model_config.index_binding import check_binding,SUPPORTED_INDEXES
from app.model_config.provider import ProviderFailure
from app.model_config.task_budget import ModelBudgetDenied
from app.model_config.context_budget import ModelContextExceeded
from app.model_config.snapshots import current_snapshot,inference_actor,thaw

_cache=OrderedDict()
_lock=threading.RLock()
CACHE_LIMIT=10000
CACHE_FLOAT_LIMIT=2000000
CACHE_TTL=300
BATCH_SIZE=10


def selected_index(key):
    snapshot=current_snapshot()
    if snapshot is None:fail(50305,'VECTOR_SNAPSHOT_REQUIRED',503)
    if key not in SUPPORTED_INDEXES:fail(40002,'INDEX_KEY_UNSUPPORTED',422)
    if not snapshot.manifest['role_bindings_raw']['embedding']['enabled']:fail(50305,'EMBEDDING_BASELINE_REQUIRED',503)
    binding=snapshot.manifest['index_bindings'].get(key)
    if not binding:fail(50305,'INDEX_NOT_BOUND',503)
    index=snapshot.indexes.get(binding['index_version_id']);profile=snapshot.profiles.get(binding['query_profile_revision_id'])
    if not index or not profile:fail(50303,'RELEASE_REFERENCE_MISSING',503)
    index,profile,binding=thaw(index),thaw(profile),thaw(binding)
    check_binding(index,profile,binding)
    return snapshot,index,profile


async def permit_index(snapshot,index,profile,session_factory=None):
    if session_factory is None:
        from app.db.database import async_session_factory
        session_factory=async_session_factory
    await runtime.permit(snapshot,profile['id'],session_factory)
    # Reference state can be retired/revoked independently of an old snapshot.
    import sqlalchemy as sa
    from app.model_config import tables as t
    from app.model_config.security import scoped
    async with session_factory() as db:
        row=(await db.execute(sa.select(t.rag_index_versions).where(scoped(t.rag_index_versions),
            t.rag_index_versions.c.id==index['id']))).mappings().first()
        if not row or row['status'] not in ('baseline_verified','ready'):
            fail(42220,'INDEX_BASELINE_UNVERIFIED',422)
        if any(row[name]!=index[name] for name in ('storage_binding','semantic_fingerprint','semantic_identity','dimension','collection_name')):
            fail(50303,'INDEX_REFERENCE_CHANGED',503)


@contextmanager
def bound_collection(index):
    from app.rag.chroma_transport import opened_chroma, route_port

    storage=index['storage_binding'];directory=Path(storage['directory'])
    try:
        if route_port(directory) is None and not (directory/'chroma.sqlite3').is_file():
            fail(50303,'INDEX_STORE_NOT_FOUND',503)
        with opened_chroma(directory, storage['tenant'], storage['database']) as client:
            collection=client.get_collection(index['collection_name'],embedding_function=None)
            if str(collection.id)!=storage['collection_id']:fail(42220,'INDEX_COLLECTION_REPLACED',422)
            if (collection.metadata or {}).get('hnsw:space')!='cosine':fail(42220,'INDEX_DISTANCE_METRIC_UNVERIFIED',422)
            yield collection
    except ProviderFailure:raise
    except Exception as error:
        from app.core.failed_response import BusinessError
        if isinstance(error,BusinessError):raise
        fail(50303,'INDEX_STORE_UNAVAILABLE',503)


def parse_vectors(response,protocol,count,dimension,*,normalization=None):
    try:
        if protocol=='ollama_embed':vectors=response['embeddings']
        else:
            data=response['output']['embeddings'] if protocol=='dashscope_embed' else response['data']
            key='text_index' if protocol=='dashscope_embed' else 'index'
            if (not isinstance(data,list) or len(data)!=count or any(type(item[key]) is not int for item in data)
                    or sorted(item[key] for item in data)!=list(range(count))):raise ValueError
            vectors=[item['embedding'] for item in sorted(data,key=lambda item:item[key])]
        if not isinstance(vectors,list) or len(vectors)!=count:raise ValueError
        for vector in vectors:
            if (not isinstance(vector,list) or len(vector)!=dimension
                    or any(type(value) not in (int,float) or not math.isfinite(value) for value in vector)
                    or not any(value!=0 for value in vector)):raise ValueError
        results = [[float(value) for value in vector] for vector in vectors]
    except (KeyError,TypeError,ValueError,OverflowError):raise ProviderFailure('EMBEDDING_RESPONSE_INVALID') from None
    if normalization == 'l2':
        for vector in results:
            norm = math.hypot(*vector)
            if norm == 0 or not math.isfinite(norm):
                raise ProviderFailure('EMBEDDING_NORMALIZATION_INVALID')
            vector[:] = [value / norm for value in vector]
    return results


async def _check_digest(gateway,profile):
    await gateway.verify_embedding_revision()


async def embeddings(texts,key,*,purpose='query',session_factory=None):
    snapshot,index,profile=selected_index(key)
    await permit_index(snapshot,index,profile,session_factory)
    if purpose not in ('query','document') or any(not isinstance(text,str) for text in texts):fail(40002,'EMBEDDING_INPUT_INVALID',422)
    if not texts:return []
    if profile['api_protocol']=='ollama_embed':
        try:
            async with runtime.gateway_for(snapshot,profile['id']) as gateway:await _check_digest(gateway,profile)
        except ProviderFailure as error:fail(50305,error.reason,503)
    if purpose == 'query' and len(texts) == 1:
        from app.rag.query_embeddings import query_embedding
        return await query_embedding((profile['semantic_fingerprint'], index['dimension'], texts[0]),
            lambda: _embed_values(texts, snapshot, index, profile, purpose, session_factory))
    return await _embed_values(texts, snapshot, index, profile, purpose, session_factory)


async def _embed_values(texts, snapshot, index, profile, purpose, session_factory):
    identity=profile['semantic_identity'];dimension=index['dimension']
    now=time.monotonic();results=[];missing=[]
    scope = {'owner': (inference_actor.get() or {}).get('user_id'), 'release': snapshot.release_id} if purpose == 'query' else {}
    keys=[content_hash({**scope,'space':profile['semantic_fingerprint'],'dimension':dimension,'purpose':purpose,
                       'text_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest()}) for text in texts]
    with _lock:
        for position,cache_key in enumerate(keys):
            value=_cache.get(cache_key)
            if value and now-value[1]<CACHE_TTL:
                _cache.move_to_end(cache_key);results.append(list(value[0]))
            else:results.append(None);missing.append(position)
    for offset in range(0,len(missing),BATCH_SIZE):
        positions=missing[offset:offset+BATCH_SIZE]
        prefix=identity['query_prefix'] if purpose=='query' else identity['document_prefix']
        inputs=[prefix+texts[position] for position in positions]
        started=time.monotonic();success=False;reason=None;response=None
        price_at = None
        dispatched = True
        reported_tokens = None
        try:
            await permit_index(snapshot,index,profile,session_factory)
            async with runtime.gateway_for(snapshot,profile['id']) as gateway:
                await _check_digest(gateway,profile)
                try:
                    response=await gateway.embed(inputs,purpose=purpose)
                finally:
                    price_at = getattr(gateway, 'request_price_at', None)
                await _check_digest(gateway,profile)
            vectors=parse_vectors(response,profile['api_protocol'],len(inputs),dimension,normalization=identity['normalization'])
            for position,vector in zip(positions,vectors):results[position]=vector
            success=True
        except ProviderFailure as error:
            reason=error.reason;fail(50305,reason,503)
        except ModelContextExceeded:
            dispatched = False
            raise
        except ModelBudgetDenied as error:
            dispatched = error.reported_usage is not None
            if dispatched:
                reported_tokens = error.reported_usage[0]
            reason = error.data['reason']
            raise
        finally:
            if dispatched:
                from app.core.model_trace import record_embedding_call
                record_embedding_call(snapshot,index,profile,purpose,success,response,round((time.monotonic()-started)*1000),
                                      reason,price_at,reported_tokens=reported_tokens)
    # A failed batch never inserts a partially validated result into the cache.
    await permit_index(snapshot,index,profile,session_factory)
    with _lock:
        for position in missing:_cache[keys[position]]=(tuple(results[position]),time.monotonic())
        floats=sum(len(value[0]) for value in _cache.values())
        while len(_cache)>CACHE_LIMIT or floats>CACHE_FLOAT_LIMIT:
            _,value=_cache.popitem(last=False);floats-=len(value[0])
    return results


def owned_metadata(metadatas):
    actor=inference_actor.get()
    if not actor or any(item.get('user_id')!=actor['user_id'] for item in metadatas):fail(40314,'VECTOR_OWNER_MISMATCH',403)


async def query(text,user_id,key,top_k):
    from app.rag.chroma_transport import vector_io

    actor=inference_actor.get()
    if not actor or actor['user_id']!=user_id:fail(40314,'VECTOR_OWNER_MISMATCH',403)
    snapshot,index,profile=selected_index(key)
    values=await embeddings([text],key,purpose='query')
    await permit_index(snapshot,index,profile)
    def search():
        with bound_collection(index) as collection:
            return collection.query(query_embeddings=values,n_results=top_k,where={'user_id':user_id},
                include=['documents','metadatas','distances'])

    return await vector_io(index['storage_binding']['directory'], search)


async def upsert(documents,metadatas,ids,key,*,write_guard=None):
    from app.rag.chroma_transport import vector_io

    owned_metadata(metadatas)
    if not (len(documents)==len(metadatas)==len(ids)):fail(40002,'VECTOR_INPUT_LENGTH_MISMATCH',422)
    snapshot,index,profile=selected_index(key)
    values=await embeddings(documents,key,purpose='document')
    await permit_index(snapshot,index,profile)
    def write():
        with bound_collection(index) as collection:
            existing=collection.get(ids=ids,include=['metadatas']) if ids else {'metadatas':[]}
            owned_metadata(existing.get('metadatas') or [])
            if ids:collection.upsert(ids=ids,documents=documents,metadatas=metadatas,embeddings=values)

    async with write_guard() if write_guard else nullcontext():
        await vector_io(index['storage_binding']['directory'], write, write=True)


async def delete(resource_id,key,field,*,user_id=None,write_guard=None):
    from app.rag.chroma_transport import vector_io

    snapshot,index,profile=selected_index(key)
    await permit_index(snapshot,index,profile)
    actor=inference_actor.get()
    if not actor:fail(40102,'INFERENCE_IDENTITY_REQUIRED',401)
    if user_id is not None and user_id != actor['user_id']:fail(40314,'VECTOR_OWNER_MISMATCH',403)
    def remove():
        with bound_collection(index) as collection:
            collection.delete(where={'$and':[{field:str(resource_id)},{'user_id':actor['user_id']}]})

    async with write_guard() if write_guard else nullcontext():
        await vector_io(index['storage_binding']['directory'], remove, write=True)
