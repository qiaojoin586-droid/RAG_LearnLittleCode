"""Queries in one recall can reuse a vector in the same semantic space."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from app.rag.read_tasks import retrieval_reads


_inflight = ContextVar('retrieval_query_embeddings', default=None)
query_owner = ContextVar('retrieval_query_owner', default=None)


@asynccontextmanager
async def share_query_embeddings(owner=None):
    # A transport in cancellation cleanup may still hold its copy of this context.
    # Mark the scope closed before cancelling its outstanding work.
    shared = _SharedEmbeddings()
    token = _inflight.set(shared)
    owner_token = query_owner.set(owner)
    try:
        yield
    finally:
        _inflight.reset(token)
        query_owner.reset(owner_token)
        shared.closed = True
        for task in shared.tasks.values():
            if not task.done():
                task.cancel()


class _SharedEmbeddings:
    def __init__(self):
        self.tasks = {}
        self.closed = False


async def query_embedding(key, generate):
    shared = _inflight.get()
    if shared is None:
        return await generate()
    if shared.closed:
        raise TimeoutError
    if key not in shared.tasks:
        shared.tasks[key] = retrieval_reads.start(generate)
    return await asyncio.shield(shared.tasks[key])
