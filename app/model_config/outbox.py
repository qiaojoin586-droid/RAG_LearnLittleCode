"""Bounded notification/prewarm work. Database head remains the authority."""
import asyncio
from datetime import timedelta

import sqlalchemy as sa

from app.model_config import tables as t
from app.model_config.credential_calls import registry
from app.model_config.errors import fail
from app.model_config.security import scoped
from app.model_config.snapshots import load_snapshot

CONFIG_CHANNEL = 'raglearn:model-config:changed'


class ConfigOutboxWorker:
    def __init__(self,control,session_factory,handler=None,*,redis=None):
        self.control=control;self.session_factory=session_factory;self.handler=handler or self._verify_current
        self.redis = redis
        self.task=None

    async def _verify_current(self,event):
        if event['event_type']=='credential.revoked':
            payload=event['payload']
            async with self.session_factory() as db:
                row=(await db.execute(sa.select(t.ai_secrets.c.status,t.ai_secrets.c.revocation_epoch).where(
                    scoped(t.ai_secrets),t.ai_secrets.c.id==payload['credential_revision_id']))).mappings().first()
                if not row or row['status']!='revoked' or row['revocation_epoch']<payload['revocation_epoch']:
                    raise RuntimeError('Credential revocation state differs')
            registry.request_cancel(payload['credential_revision_id'])
            return
        if event['event_type']!='model_config.published':raise RuntimeError('Unknown configuration event type')
        async with self.session_factory() as db:
            snapshot=await load_snapshot(db)
            if snapshot is None:raise RuntimeError('Missing active release')

    async def tick(self):
        async with self.session_factory() as db:
            async with db.begin():
                await db.execute(sa.select(t.ai_config_authorization_heads).where(
                    scoped(t.ai_config_authorization_heads)).with_for_update())
                manager=await self.control._manager(db)
                now=await self.control._now(db)
                table=t.outbox_events
                query=sa.select(table).where(scoped(table),sa.or_(
                    sa.and_(table.c.status.in_(['pending','retry']),table.c.next_attempt_at<=now),
                    sa.and_(table.c.status=='inflight',table.c.lease_expires_at<=now))).order_by(
                        table.c.created_at).limit(1).with_for_update(skip_locked=True)
                row=(await db.execute(query)).mappings().first()
                if not row:return False
                event=dict(row)
                if event['attempts']>=5:
                    await db.execute(table.update().where(scoped(table),table.c.id==event['id']).values(status='dead_letter',
                        error_code=event['error_code'] or 'CONFIG_NOTIFY_LEASE_EXPIRED',lease_owner=None,lease_expires_at=None))
                    return True
                attempt=event['attempts']+1
                await db.execute(table.update().where(table.c.id==event['id']).values(status='inflight',
                    attempts=attempt,lease_owner=self.control.owner_id,lease_expires_at=now+timedelta(seconds=15)))
        success=False
        try:
            async with asyncio.timeout(10):
                await self.handler(event)
                if self.redis is not None:
                    await self.redis.publish(CONFIG_CHANNEL, 'changed')
            success=True
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        async with self.session_factory() as db:
            async with db.begin():
                head=await self.control._head(db,lock=True)
                now=await self.control._now(db)
                if (not head or head['manager_owner_id']!=self.control.owner_id
                        or head['manager_generation']!=manager['manager_generation']
                        or not head['manager_lease_expires_at'] or head['manager_lease_expires_at']<=now):
                    fail(50304,'MANAGER_LEASE_LOST',503)
                await db.execute(t.outbox_events.update().where(scoped(t.outbox_events),
                    t.outbox_events.c.id==event['id'],t.outbox_events.c.status=='inflight',
                    t.outbox_events.c.lease_owner==self.control.owner_id,t.outbox_events.c.attempts==attempt,
                    t.outbox_events.c.lease_expires_at>now).values(
                        status='succeeded' if success else 'dead_letter' if attempt>=5 else 'retry',
                        error_code=None if success else 'CONFIG_NOTIFY_FAILED',
                        next_attempt_at=now+timedelta(seconds=min(60,2**attempt)),lease_owner=None,lease_expires_at=None))
        return True

    async def _run(self):
        while True:
            try:
                worked=await self.tick()
            except asyncio.CancelledError:raise
            except Exception:worked=False
            await asyncio.sleep(.2 if worked else 5)

    def start(self):
        if self.task is None or self.task.done():self.task=asyncio.create_task(self._run(),name='model_config_outbox')

    async def stop(self):
        if self.task:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True);self.task=None


class CredentialRevocationWatcher:
    """Reconcile local in-flight HTTP calls even if another process consumed the event."""

    def __init__(self, session_factory, *, calls=registry, redis=None):
        self.session_factory = session_factory
        self.calls = calls
        self.task = None
        self.redis = redis
        self.listener = None
        self.wakeup = asyncio.Event()

    async def reconcile(self):
        active = self.calls.active_ids()
        if not active:
            return
        table = t.ai_secrets
        async with self.session_factory() as db:
            for offset in range(0, len(active), 128):
                revoked = await db.execute(sa.select(table.c.id).where(scoped(table),
                    table.c.id.in_(active[offset:offset + 128]), table.c.status == 'revoked'))
                for identifier in revoked.scalars():
                    self.calls.request_cancel(identifier)

    async def _run(self):
        from app.core.logger_handler import logger

        while True:
            try:
                async with asyncio.timeout(2):
                    await self.reconcile()
            except Exception as error:
                logger.debug(f'凭据撤销核验重试: error_type={type(error).__name__}')
            try:
                await asyncio.wait_for(self.wakeup.wait(), 2)
            except TimeoutError:
                pass
            self.wakeup.clear()

    async def _listen(self):
        from app.core.logger_handler import logger

        while True:
            try:
                async with self.redis.pubsub(ignore_subscribe_messages=True) as subscriber:
                    await subscriber.subscribe(CONFIG_CHANNEL)
                    self.wakeup.set()
                    async for message in subscriber.listen():
                        if message['type'] == 'message':
                            self.wakeup.set()
            except Exception as error:
                logger.debug(f'配置通知订阅重试: error_type={type(error).__name__}')
            await asyncio.sleep(2)

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name='credential_revocation_watcher')
            if self.redis is not None:
                self.listener = asyncio.create_task(self._listen(), name='config_notification_listener')

    async def stop(self):
        if self.task is not None:
            tasks = [task for task in (self.task, self.listener) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.task = None
            self.listener = None
