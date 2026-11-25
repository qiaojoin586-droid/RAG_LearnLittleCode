"""Durable source receipts and fenced extraction jobs. No business tools are exposed."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json

import sqlalchemy as sa

from app.model_config.contracts import validate
from app.model_config.service import uid,utcnow,json_view
from app.model_config.snapshots import _current,inference_actor,load_snapshot
from app.model_config.runtime import model_for,template_for
from app.models.chat import ChatMessage,ChatSession
from app.user_memory import tables as t
from app.user_memory.service import error,normalize,SENSITIVE


class MemoryJobs:
    def __init__(self,memory):self.memory=memory

    async def source(self,db,user_id,message_id):
        row=(await db.execute(sa.select(ChatMessage.__table__).join(ChatSession,ChatMessage.session_id==ChatSession.id).where(
            ChatSession.user_id==user_id,ChatMessage.id==message_id,ChatMessage.role=='user'))).mappings().first()
        if not row or row['created_at']<utcnow()-timedelta(days=7):error(42241,'MEMORY_SOURCE_REQUIRED',422)
        return dict(row)

    async def _enqueue(self,db,user_id,head,request_id,message_id,*,retry=False,release_id=None):
        available,release=await self.memory.availability(db,release_id)
        if not available or not head['enabled'] or not head['auto_extract']:error(40944,'MEMORY_EXTRACTION_UNAVAILABLE')
        source=await self.source(db,user_id,message_id)
        table=t.user_memory_jobs
        old=(await db.execute(sa.select(table).where(table.c.user_id==user_id,
            table.c.source_message_id==message_id).order_by(table.c.created_at.desc()).limit(1).with_for_update())).mappings().first()
        if old and (not retry or old['status'] not in ('failed','obsolete','cancelled')):
            return self.view(old)
        active=(await db.execute(sa.select(sa.func.count()).select_from(table).where(
            table.c.user_id==user_id,table.c.status.in_(['queued','running'])))).scalar_one()
        revision=await self.memory.revision(db,user_id,head['active_revision_id']) if head['active_revision_id'] else None
        content=source['content'].encode()[:4096].decode('utf-8',errors='ignore')
        rejected=bool(SENSITIVE.search(source['content']))
        if not source['content'].strip():error(42241,'MEMORY_SOURCE_REQUIRED',422)
        now=utcnow()
        row=dict(id=old['id'] if old else uid(),user_id=user_id,source_request_id=old['source_request_id'] if old else request_id,
            source_message_id=message_id,source_snapshot=None if rejected else {'user_message':content,
                'content_md':revision['content_md'] if revision else '', 'source_truncated':content!=source['content']},
            base_revision_id=head['active_revision_id'],expected_head_version=head['head_version'],memory_epoch=head['memory_epoch'],
            scope_type='system',scope_id='system',config_release_id=release,
            generation_number=old['generation_number']+1 if old else 1,status='failed' if rejected else 'skipped_limit' if active>=5 else 'queued',
            attempt_count=0,lease_owner=None,lease_until=None,deadline_at=now+timedelta(seconds=180),
            proposal_expires_at=None,proposed_content_md=None,proposed_content_hash=None,applied_revision_id=None,
            error_reason='MEMORY_CONTENT_REJECTED' if rejected else 'MEMORY_QUEUE_LIMIT' if active>=5 else None,
            created_at=old['created_at'] if old else now,updated_at=now)
        if old:await db.execute(table.update().where(table.c.id==old['id'],table.c.user_id==user_id).values(**row))
        else:await db.execute(table.insert().values(**row))
        return self.view(row)

    async def record_success(self,db,user_id,request_id,message_id):
        """Called before committing the assistant reply in the same SQL transaction."""
        await self.memory.authorize(db,user_id,lock=True)
        head=await self.memory.head(db,user_id,lock=True)
        if not head['enabled'] or not head['active_revision_id']:return
        source=await self.source(db,user_id,message_id)
        if not source['content'].strip():return
        if head.get('last_source_message_id')!=message_id:
            await db.execute(t.user_memory_heads.update().where(t.user_memory_heads.c.user_id==user_id).values(
                last_source_request_id=request_id,last_source_message_id=message_id,last_source_completed_at=utcnow()))
        else:request_id=head['last_source_request_id']
        from app.model_config.snapshots import current_snapshot
        parent=current_snapshot()
        available,_=await self.memory.availability(db,parent.release_id) if parent else (False,None)
        if available and head['auto_extract']:
            await self._enqueue(db,user_id,head,request_id,message_id,release_id=parent.release_id)

    @staticmethod
    def view(row):
        return json_view({key:row[key] for key in ('id','status','generation_number','attempt_count','source_request_id',
            'source_message_id','base_revision_id','expected_head_version','memory_epoch','config_release_id',
            'proposed_content_md','applied_revision_id','error_reason','created_at','updated_at','proposal_expires_at')})

    async def get_job(self,db,user_id,job_id,*,replay=False):
        await self.memory.authorize(db,user_id)
        row=(await db.execute(sa.select(t.user_memory_jobs).where(t.user_memory_jobs.c.user_id==user_id,
            t.user_memory_jobs.c.id==job_id))).mappings().first()
        if not row:error(40440,'MEMORY_NOT_FOUND',404)
        if replay and row['error_reason']=='MEMORY_RESULT_PURGED':error(41040,'MEMORY_RESULT_PURGED',410)
        return self.view(row)

    async def list(self,db,user_id,limit=20,cursor=None):
        await self.memory.authorize(db,user_id)
        table=t.user_memory_jobs;where=[table.c.user_id==user_id]
        if cursor:
            previous=await self.get_job(db,user_id,cursor)
            from datetime import datetime
            time=datetime.fromisoformat(previous['created_at'].replace('Z',''))
            where.append(sa.or_(table.c.created_at<time,sa.and_(table.c.created_at==time,table.c.id<cursor)))
        rows=(await db.execute(sa.select(table).where(*where).order_by(table.c.created_at.desc(),table.c.id.desc()).limit(limit+1))).mappings().all()
        return {'items':[self.view(row) for row in rows[:limit]],'next_cursor':rows[limit-1]['id'] if len(rows)>limit else None}

    async def generate(self,db,user_id,body,key,match):
        validate('MemoryEmptyInput',body)
        async def operation(head):
            if not head.get('last_source_message_id') or not head.get('last_source_completed_at') or head['last_source_completed_at']<utcnow()-timedelta(days=7):
                error(42241,'MEMORY_SOURCE_REQUIRED',422)
            return await self._enqueue(db,user_id,head,head['last_source_request_id'],head['last_source_message_id'],retry=True)
        return await self.memory.mutate(db,user_id,'generate',body,key,match,operation)

    async def proposal(self,db,user_id,job_id,body,key,match,*,dismiss=False):
        validate('MemoryEmptyInput',body)
        async def operation(head):
            row=(await db.execute(sa.select(t.user_memory_jobs).where(t.user_memory_jobs.c.user_id==user_id,
                t.user_memory_jobs.c.id==job_id).with_for_update())).mappings().first()
            if not row:error(40440,'MEMORY_NOT_FOUND',404)
            if row['status']!='proposed':error(40941,'MEMORY_PROPOSAL_OBSOLETE')
            if not dismiss:
                if (row['base_revision_id']!=head['active_revision_id'] or row['expected_head_version']!=head['head_version']
                    or row['memory_epoch']!=head['memory_epoch'] or row['proposal_expires_at']<=utcnow()):error(40941,'MEMORY_PROPOSAL_OBSOLETE')
                head['management_mode']='manual'
                revision=await self.memory.new_revision(db,head,row['proposed_content_md'],'proposal_accept',row['source_request_id'])
            await db.execute(t.user_memory_jobs.update().where(t.user_memory_jobs.c.id==job_id).values(
                status='cancelled' if dismiss else 'applied',applied_revision_id=None if dismiss else revision['id'],updated_at=utcnow()))
        return await self.memory.mutate(db,user_id,'dismiss' if dismiss else 'apply',{'job_id':job_id},key,match,operation)


class MemoryWorker:
    def __init__(self,memory,session_factory):
        self.memory=memory;self.jobs=MemoryJobs(memory);self.session_factory=session_factory;self.owner=uid();self.task=None

    async def claim(self):
        table=t.user_memory_jobs
        async with self.session_factory() as db:
            candidate=(await db.execute(sa.select(table.c.id,table.c.user_id).where(sa.or_(table.c.status=='queued',
                sa.and_(table.c.status=='running',table.c.lease_until<=utcnow()))).order_by(table.c.created_at).limit(1))).first()
        if not candidate:return None
        async with self.session_factory() as db:
            async with db.begin():
                from app.core.failed_response import BusinessError
                try:await self.memory.authorize(db,candidate.user_id,lock=True)
                except BusinessError:
                    await db.execute(table.update().where(table.c.id==candidate.id,table.c.user_id==candidate.user_id).values(
                        status='cancelled',error_reason='ACCOUNT_INACTIVE',updated_at=utcnow()));return None
                head=await self.memory.head(db,candidate.user_id,lock=True)
                job=(await db.execute(sa.select(table).where(table.c.id==candidate.id,table.c.user_id==candidate.user_id).with_for_update())).mappings().first()
                if job['status'] not in ('queued','running'):return None
                if job['status']=='running' or job['deadline_at']<=utcnow():
                    await db.execute(table.update().where(table.c.id==job['id']).values(status='failed',error_reason='MEMORY_RESULT_UNKNOWN',updated_at=utcnow()));return None
                if not head['enabled'] or not head['auto_extract'] or head['memory_epoch']!=job['memory_epoch']:
                    await db.execute(table.update().where(table.c.id==job['id']).values(status='cancelled',updated_at=utcnow()));return None
                running=(await db.execute(sa.select(sa.func.count()).select_from(table).where(table.c.user_id==job['user_id'],
                    table.c.status=='running',table.c.lease_until>utcnow()))).scalar_one()
                if running:return None
                row=dict(job);row.update(status='running',attempt_count=1,lease_owner=self.owner,lease_until=utcnow()+timedelta(seconds=65),updated_at=utcnow())
                await db.execute(table.update().where(table.c.id==row['id']).values(**row))
                return row

    async def tick(self):
        job=await self.claim()
        if not job:return False
        result=None;reason='MEMORY_EXTRACTION_FAILED'
        try:
            async with self.session_factory() as db:
                # Source authorization is rechecked after claiming and before a call.
                await self.jobs.source(db,job['user_id'],job['source_message_id'])
                snapshot=await load_snapshot(db,release_id=job['config_release_id'])
            from app.model_config.runtime import dispatch_fence
            async def check_fence():
                async with self.session_factory() as check_db:
                    current=(await check_db.execute(sa.select(t.user_memory_jobs).where(t.user_memory_jobs.c.id==job['id'],
                        t.user_memory_jobs.c.user_id==job['user_id']))).mappings().one()
                    head=await self.memory.head(check_db,job['user_id'])
                    if (current['status']!='running' or current['generation_number']!=job['generation_number'] or
                        head['memory_epoch']!=job['memory_epoch'] or head['head_version']!=job['expected_head_version']):
                        error(40941,'MEMORY_PROPOSAL_OBSOLETE')
            token=_current.set(snapshot);actor=inference_actor.set({'user_id':job['user_id'],'jti':None,'expires':None})
            fence_token=dispatch_fence.set(check_fence)
            try:
                source=job['source_snapshot']
                prompt=template_for('memory_extraction','',content_md=source['content_md'],user_message=source['user_message'])
                model=model_for('memory_extraction')
                profile=snapshot.profiles[model.profile_id]
                if len(prompt.encode())>(profile['context_window'] or 8192)-(profile['max_output_tokens'] or 2048):
                    reason='MEMORY_INPUT_LIMIT';raise ValueError
                from app.core.model_trace import trace_scope
                with trace_scope(request_id=job['source_request_id'],user_id=job['user_id'],stage='memory_extraction',
                    step_id=job['id'],memory_context={'revision_id':job['base_revision_id'],
                    'head_version':job['expected_head_version'],'memory_epoch':job['memory_epoch'],
                    'content_hash':hashlib.sha256(source['content_md'].encode()).hexdigest(),
                    'source_truncated':source['source_truncated']}):
                    async with asyncio.timeout(min(60,max(0,(job['deadline_at']-utcnow()).total_seconds()))):
                        answer=await model.ainvoke([('human',prompt+'\n来源用户消息 ID：'+str(job['source_message_id']))])
                result=json.loads(answer.content)
                validate('MemoryExtractionResult',result)
                if result['decision']=='update':
                    result['content_md']=normalize(result['content_md'])
                    for evidence in result['evidence']:
                        if evidence['source_message_id']!=job['source_message_id'] or evidence['quote'] not in source['user_message']:
                            reason='MEMORY_EVIDENCE_INVALID';raise ValueError
            finally:_current.reset(token);inference_actor.reset(actor);dispatch_fence.reset(fence_token)
        except asyncio.CancelledError:
            raise  # lease expires; never claim the unknown result as successful
        except Exception:result=None
        await self.finish(job,result,reason)
        return True

    async def finish(self,job,result,reason):
        table=t.user_memory_jobs
        async with self.session_factory() as db:
            async with db.begin():
                from app.core.failed_response import BusinessError
                try:await self.memory.authorize(db,job['user_id'],lock=True)
                except BusinessError:
                    await db.execute(table.update().where(table.c.id==job['id'],table.c.user_id==job['user_id'],
                        table.c.generation_number==job['generation_number']).values(status='cancelled',error_reason='ACCOUNT_INACTIVE',updated_at=utcnow()));return
                head=await self.memory.head(db,job['user_id'],lock=True)
                current=(await db.execute(sa.select(table).where(table.c.id==job['id']).with_for_update())).mappings().one()
                if current['status']!='running' or current['generation_number']!=job['generation_number'] or current['lease_owner']!=self.owner:return
                values={'status':'failed','error_reason':reason,'updated_at':utcnow(),'lease_owner':None,'lease_until':None}
                if (head['memory_epoch']!=job['memory_epoch'] or head['head_version']!=job['expected_head_version']
                    or head['active_revision_id']!=job['base_revision_id'] or not head['enabled'] or not head['auto_extract']):
                    values.update(status='obsolete',error_reason='MEMORY_PROPOSAL_OBSOLETE')
                elif result and utcnow()<job['deadline_at']:
                    values['error_reason']=None
                    if result['decision']=='no_change':values['status']='no_change'
                    elif head['management_mode']=='manual':values.update(status='proposed',proposed_content_md=result['content_md'],
                        proposed_content_hash=hashlib.sha256(result['content_md'].encode()).hexdigest(),proposal_expires_at=utcnow()+timedelta(days=30))
                    else:
                        revision=await self.memory.new_revision(db,head,result['content_md'],'model_generation',job['source_request_id'])
                        values.update(status='applied',applied_revision_id=revision['id'])
                await db.execute(table.update().where(table.c.id==job['id']).values(**values))

    async def cleanup(self):
        async with self.session_factory() as db:
            async with db.begin():
                table=t.user_memory_jobs
                await db.execute(table.update().where(table.c.status.not_in(['queued','running']),table.c.updated_at<utcnow()-timedelta(days=7)).values(source_snapshot=None))
                await db.execute(table.update().where(table.c.status=='proposed',table.c.proposal_expires_at<=utcnow()).values(
                    status='obsolete',proposed_content_md=None,proposed_content_hash=None,error_reason='MEMORY_PROPOSAL_EXPIRED'))
                await db.execute(t.user_memory_requests.delete().where(t.user_memory_requests.c.tombstone_until<utcnow()))

    async def _run(self):
        while True:
            try:
                worked=await self.tick();await self.cleanup()
            except asyncio.CancelledError:raise
            except Exception:worked=False
            await asyncio.sleep(.5 if worked else 5)

    def start(self):
        if self.task is None or self.task.done():self.task=asyncio.create_task(self._run(),name='personal_memory_worker')
    async def stop(self):
        if self.task:self.task.cancel();await asyncio.gather(self.task,return_exceptions=True);self.task=None
