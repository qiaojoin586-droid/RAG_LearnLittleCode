"""Same-process best-effort cancellation of credential-bound HTTP operations.

The DB permit remains authoritative. Register before the guarded permit and keep
the lease until response closure, so a revoke-after-permit race is in flight.
Only HTTP work is cancelled; the caller receives a domain error, not Task.cancel.
"""
from __future__ import annotations

import asyncio
import threading
import weakref

import httpx

from app.model_config.errors import fail


class CallLease:
    def __init__(self,registry,credential_id):
        self.registry=registry;self.credential_id=credential_id
        self.loop=asyncio.get_running_loop();self.cancelled=asyncio.Event();self.closed=False

    def __repr__(self):return '<CredentialCall redacted>'

    def request_cancel(self):
        if not self.closed and not self.loop.is_closed():self.loop.call_soon_threadsafe(self.cancelled.set)

    def close(self):
        if self.closed:return
        self.closed=True;self.registry.release(self)

    async def run(self,operation):
        if self.cancelled.is_set():fail(40926,'CREDENTIAL_REVOKED')
        work=asyncio.ensure_future(operation());watch=asyncio.create_task(self.cancelled.wait())
        try:
            await asyncio.wait((work,watch),return_when=asyncio.FIRST_COMPLETED)
            if self.cancelled.is_set():
                # An operation may finish in the same tick as the signal. Close
                # returned response headers too, rather than leaking its pool.
                if work.done() and not work.cancelled() and work.exception() is None:
                    value=work.result()
                    if isinstance(value,httpx.Response):await value.aclose()
                fail(40926,'CREDENTIAL_REVOKED')
            return await work
        finally:
            for task in (work,watch):
                if not task.done():task.cancel()
            await asyncio.gather(work,watch,return_exceptions=True)


class CredentialCallRegistry:
    def __init__(self):self._lock=threading.RLock();self._calls={}
    def __repr__(self):return '<CredentialCallRegistry>'

    def open(self,credential_id):
        lease=CallLease(self,credential_id)
        with self._lock:self._calls.setdefault(credential_id,weakref.WeakSet()).add(lease)
        return lease

    def release(self,lease):
        with self._lock:
            calls=self._calls.get(lease.credential_id)
            if calls is not None:
                calls.discard(lease)
                if not calls:self._calls.pop(lease.credential_id,None)

    def request_cancel(self,credential_id):
        with self._lock:calls=list(self._calls.get(credential_id,()))
        for lease in calls:
            try:lease.request_cancel()
            except RuntimeError:lease.close()  # an already-closed event loop
        return len(calls)

    def count(self,credential_id):
        with self._lock:return len(self._calls.get(credential_id,()))

    def active_ids(self):
        with self._lock:
            return tuple(identifier for identifier, calls in self._calls.items() if calls)


registry=CredentialCallRegistry()


class CredentialStream(httpx.AsyncByteStream):
    def __init__(self,stream,lease):self.stream=stream;self.lease=lease;self.closed=False
    async def __aiter__(self):
        iterator=self.stream.__aiter__()
        try:
            while True:
                try:chunk=await self.lease.run(lambda:iterator.__anext__())
                except StopAsyncIteration:break
                yield chunk
        finally:await self.aclose()
    async def aclose(self):
        if self.closed:return
        self.closed=True
        try:await self.stream.aclose()
        finally:self.lease.close()


class CredentialTransport(httpx.AsyncBaseTransport):
    def __init__(self,inner,credential_id,*,calls=registry):
        self.inner=inner;self.credential_id=credential_id;self.calls=calls
    async def handle_async_request(self,request):
        lease=self.calls.open(self.credential_id)
        try:
            response=await lease.run(lambda:self.inner.handle_async_request(request))
            response.stream=CredentialStream(response.stream,lease)
            return response
        except BaseException:
            lease.close();raise
    async def aclose(self):await self.inner.aclose()
