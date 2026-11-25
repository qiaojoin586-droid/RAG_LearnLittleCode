"""Opt-in, bounded candidate retirement/purge; published revisions are retained."""
import asyncio
import os
from datetime import timedelta

import sqlalchemy as sa

from app.model_config import tables as t
from app.model_config.credential_calls import registry
from app.model_config.errors import fail
from app.model_config.security import scoped

SYSTEM_ACTOR='system:config-lifecycle'
CLOSED=('abandoned','expired','published')
OPEN=('editing','validating','ready')
KINDS=(('profile',t.ai_profile_revisions,'source_draft_id'),
       ('prompt',t.ai_prompt_revisions,'source_draft_id'),('credential',t.ai_secrets,'origin_draft_id'))


class CandidateLifecycleWorker:
    def __init__(self,control,session_factory,*,batch_size=20,purge=False):
        if not 1<=batch_size<=100:raise ValueError('Invalid lifecycle batch size')
        self.control=control;self.session_factory=session_factory;self.batch_size=batch_size;self.purge=purge
        self.cursor=None;self.task=None
        self.asset_cursors={}

    async def _protected(self,db,kind,row,now,*,purging=False):
        identifier=row['id']
        if (await db.execute(sa.select(t.ai_config_release_refs.c.release_id).where(scoped(t.ai_config_release_refs),
                t.ai_config_release_refs.c.entity_kind==kind,t.ai_config_release_refs.c.entity_id==identifier).limit(1))).first():return True
        quoted='"'+identifier+'"'
        releases=t.ai_config_releases
        if (await db.execute(sa.select(releases.c.id).where(scoped(releases),sa.or_(*(
                sa.cast(releases.c[name],sa.Text).contains(quoted,autoescape=True)
                for name in ('role_bindings_raw','resolved_static_bindings','index_bindings')))).limit(1))).first():return True
        # Inventory may include older draft versions; protect the whole live
        # inventory instead of guessing which JSON paths will be used next.
        items=t.ai_config_draft_items;drafts=t.ai_config_drafts
        if (await db.execute(sa.select(drafts.c.id).where(scoped(drafts),drafts.c.status.in_(OPEN),drafts.c.expires_at>now,
                sa.cast(drafts.c.payload,sa.Text).contains(quoted,autoescape=True)).limit(1))).first():return True
        if (await db.execute(sa.select(items.c.draft_id).select_from(items.join(drafts,sa.and_(
                items.c.draft_id==drafts.c.id,items.c.scope_type==drafts.c.scope_type,items.c.scope_id==drafts.c.scope_id)))
                .where(scoped(items),items.c.entity_kind==kind,items.c.entity_id==identifier,
                    drafts.c.status.in_(OPEN),drafts.c.expires_at>now).limit(1))).first():return True
        source=row['origin_draft_id' if kind=='credential' else 'source_draft_id']
        validations=t.ai_config_validations
        if (await db.execute(sa.select(validations.c.id).where(scoped(validations),
                sa.or_(validations.c.draft_id==source,validations.c.credential_revision_id==identifier,
                    sa.cast(validations.c.target_spec,sa.Text).contains(quoted,autoescape=True)),
                sa.or_(validations.c.lease_expires_at>now,
                    sa.and_(validations.c.status.in_(('pending','running')),validations.c.deadline_at>now),
                    sa.and_(validations.c.status=='passed',validations.c.valid_until>now))).limit(1))).first():return True
        if kind=='profile':
            if (await db.execute(sa.select(t.rag_index_versions.c.id).where(scoped(t.rag_index_versions),
                    t.rag_index_versions.c.build_profile_revision_id==identifier).limit(1))).first():return True
        if kind=='prompt':
            if (await db.execute(sa.select(t.ai_prompt_revisions.c.id).where(scoped(t.ai_prompt_revisions),
                    t.ai_prompt_revisions.c.restored_from_revision_id==identifier).limit(1))).first():return True
        if kind=='credential':
            if registry.count(identifier):return True
            if (await db.execute(sa.select(t.outbox_events.c.id).where(scoped(t.outbox_events),
                    t.outbox_events.c.event_type=='credential.revoked',t.outbox_events.c.aggregate_id==identifier,
                    t.outbox_events.c.status.in_(('pending','inflight','retry'))).limit(1))).first():return True
            profiles=t.ai_profile_revisions
            query=sa.select(profiles.c.id).where(scoped(profiles),profiles.c.credential_revision_id==identifier)
            if not purging:query=query.where(sa.or_(profiles.c.status=='published',profiles.c.source_draft_id!=source))
            if (await db.execute(query.limit(1))).first():return True
        return False

    async def tick(self):
        changes={'expired_drafts':0,'retired':0,'purged':0,'protected':0}
        asset_cursors=dict(self.asset_cursors)
        async with self.session_factory() as db:
            async with db.begin():
                await db.execute(sa.select(t.ai_config_authorization_heads).where(scoped(t.ai_config_authorization_heads)).with_for_update())
                manager=await self.control._manager(db);now=await self.control._now(db)
                drafts=t.ai_config_drafts
                expired=(await db.execute(sa.select(drafts.c.id).where(scoped(drafts),drafts.c.status.in_(OPEN),
                    drafts.c.expires_at<=now).order_by(drafts.c.expires_at,drafts.c.id).limit(self.batch_size).with_for_update())).scalars().all()
                for identifier in expired:
                    await db.execute(drafts.update().where(scoped(drafts),drafts.c.id==identifier).values(status='expired',updated_at=now))
                    await self.control._audit(db,SYSTEM_ACTOR,'draft.expire',identifier,{'draft_id':identifier,'maintenance':True})
                changes['expired_drafts']=len(expired)
                query=sa.select(drafts.c.id).where(scoped(drafts),drafts.c.status.in_(CLOSED))
                if self.cursor:query=query.where(drafts.c.id>self.cursor)
                sources=(await db.execute(query.order_by(drafts.c.id).limit(self.batch_size).with_for_update())).scalars().all()
                # Advance only after commit, so failures revisit the same batch.
                next_cursor=sources[-1] if len(sources)==self.batch_size else None
                for source in sources:
                    for kind,table,origin in KINDS:
                        token=(source,kind)
                        query=sa.select(table).where(scoped(table),table.c[origin]==source,
                            table.c.status.in_(('candidate','retired','revoked') if kind=='credential' else ('candidate','retired')),
                            sa.or_(table.c.status=='candidate',table.c.retired_at.is_(None),table.c.retired_at<=now-timedelta(days=7))
                                if self.purge else sa.or_(table.c.status=='candidate',table.c.retired_at.is_(None)))
                        if token in asset_cursors:query=query.where(table.c.id>asset_cursors[token])
                        candidates=(await db.execute(query.order_by(table.c.id).limit(100).with_for_update())).mappings().all()
                        if len(candidates)==100:asset_cursors[token]=candidates[-1]['id']
                        else:asset_cursors.pop(token,None)
                        for row in candidates:
                            if await self._protected(db,kind,row,now,purging=row['status']!='candidate' and row['retired_at'] is not None):changes['protected']+=1;continue
                            if row['status']=='candidate' or row['retired_at'] is None:
                                await db.execute(table.update().where(scoped(table),table.c.id==row['id']).values(
                                    status='revoked' if row['status']=='revoked' else 'retired',retired_at=now))
                                changes['retired']+=1;action='candidate.retire'
                            elif self.purge and row['retired_at']<=now-timedelta(days=7):
                                await db.execute(t.ai_config_draft_items.delete().where(scoped(t.ai_config_draft_items),
                                    t.ai_config_draft_items.c.entity_kind==kind,t.ai_config_draft_items.c.entity_id==row['id']))
                                await db.execute(table.delete().where(scoped(table),table.c.id==row['id']))
                                changes['purged']+=1;action='candidate.purge'
                            else:continue
                            diff={'entity_kind':kind,'entity_id':row['id'],'origin_draft_id':source,'maintenance':True}
                            if kind=='credential':diff.update(credential_revision_id=row['id'],credential_version=row['credential_version'])
                            await self.control._audit(db,SYSTEM_ACTOR,action,row['id'],diff)
                if await self.control._now(db)>=manager['manager_lease_expires_at']:fail(50304,'MANAGER_LEASE_LOST',503)
        self.cursor=next_cursor
        self.asset_cursors=asset_cursors
        return changes

    async def _run(self):
        while True:
            try:
                result=await self.tick()
                delay=60 if self.cursor or self.asset_cursors or result['expired_drafts']==self.batch_size else 3600
            except asyncio.CancelledError:raise
            except Exception:
                from app.core.logger_handler import logger
                logger.warning('候选配置维护失败，事务已回滚，将在 60 秒后重试')
                delay=60  # No values or SQL in logs.
            await asyncio.sleep(delay)

    def start(self):
        if self.task is None or self.task.done():self.task=asyncio.create_task(self._run(),name='config_candidate_lifecycle')

    async def stop(self):
        if self.task:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True);self.task=None


def configured_worker(control,default_factory):
    """Explicit enablement; production purge needs a separate maintenance role."""
    truth=lambda name:os.getenv(name,'false').lower() in ('true','1','yes')
    if not truth('AI_CONFIG_CANDIDATE_GC_ENABLED'):return None,None
    purge=truth('AI_CONFIG_CANDIDATE_GC_PURGE');dsn=os.getenv('AI_CONFIG_MAINTENANCE_DATABASE_URL')
    development=os.getenv('APP_ENV','development').split('#',1)[0].strip().lower()=='development'
    if purge and not development and not dsn:
        fail(50303,'MAINTENANCE_STORE_REQUIRED',503)
    factory=default_factory;engine=None
    if dsn:
        from sqlalchemy.engine import make_url
        from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
        invalid=False
        try:
            target=make_url(dsn);ordinary=default_factory.kw['bind'].url
            if (target.get_backend_name(),target.host,target.port,target.database)!=(
                    ordinary.get_backend_name(),ordinary.host,ordinary.port,ordinary.database):raise ValueError
            if target.username==ordinary.username and not development:raise ValueError
            engine=create_async_engine(target,pool_pre_ping=True,pool_size=2,max_overflow=0)
            factory=async_sessionmaker(engine,expire_on_commit=False)
        except Exception:invalid=True
        if invalid:fail(50303,'MAINTENANCE_STORE_INVALID',503)
    return CandidateLifecycleWorker(control,factory,purge=purge),engine
