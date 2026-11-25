"""Redacted notification inspection and audited, append-only dead-letter redrive."""
import sqlalchemy as sa

from app.model_config import tables as t
from app.model_config.contracts import validate
from app.model_config.errors import fail
from app.model_config.security import authorize, scoped, SCOPE
from app.model_config.service import json_view, uid

EVENT_TYPES=('model_config.published','credential.revoked')
STATUSES=('pending','inflight','succeeded','retry','dead_letter')
FIELDS=('id','event_type','aggregate_id','aggregate_version','status','attempts','next_attempt_at',
        'lease_expires_at','error_code','created_at')


class OutboxService:
    def __init__(self,control):self.control=control

    def _query(self):
        table=t.outbox_events;child=table.alias('redrive')
        return sa.select(*(table.c[name] for name in FIELDS),child.c.id.label('redrive_event_id')).select_from(
            table.outerjoin(child,sa.and_(scoped(child),child.c.event_key==sa.literal('redrive:')+table.c.id))
        ).where(scoped(table),table.c.event_type.in_(EVENT_TYPES))

    @staticmethod
    def view(row):
        result=dict(row)
        if result['error_code'] not in (None,'CONFIG_NOTIFY_FAILED','CONFIG_NOTIFY_LEASE_EXPIRED'):
            result['error_code']='CONFIG_NOTIFY_FAILED'
        return json_view(result)

    async def list(self,db,actor,*,status=None,cursor=None,limit=20):
        await authorize(db,actor,'config.audit.read')
        if status is not None and status not in STATUSES:fail(40002,'OUTBOX_STATUS_INVALID',422)
        if not 1<=limit<=100:fail(40002,'PAGE_LIMIT_INVALID',422)
        query=self._query()
        if status:query=query.where(t.outbox_events.c.status==status)
        if cursor:query=query.where(t.outbox_events.c.id<cursor)
        rows=(await db.execute(query.order_by(t.outbox_events.c.id.desc()).limit(limit+1))).mappings().all()
        return {'items':[self.view(row) for row in rows[:limit]],'limit':limit,
                'next_cursor':rows[limit-1]['id'] if len(rows)>limit else None}

    async def get(self,db,actor,event_id):
        await authorize(db,actor,'config.audit.read')
        row=(await db.execute(self._query().where(t.outbox_events.c.id==event_id))).mappings().first()
        if not row:fail(40401,'CONFIG_RESOURCE_NOT_FOUND',404)
        return self.view(row)

    async def redrive(self,db,actor,event_id,body,key,proof):
        validate('OutboxRedriveInput',body)
        async def operation(head):
            table=t.outbox_events
            row=(await db.execute(sa.select(table).where(scoped(table),table.c.id==event_id,
                table.c.event_type.in_(EVENT_TYPES)).with_for_update())).mappings().first()
            if not row:fail(40401,'CONFIG_RESOURCE_NOT_FOUND',404)
            if row['status']!='dead_letter' or row['attempts']!=body['expected_attempts']:
                fail(40923,'OUTBOX_STATE_CHANGED')
            child_key='redrive:'+event_id
            if (await db.execute(sa.select(table.c.id).where(scoped(table),table.c.event_key==child_key))).first():
                fail(40923,'OUTBOX_ALREADY_REDRIVEN')
            payload=({'release_id':row['aggregate_id'],'head_version':row['aggregate_version']}
                if row['event_type']=='model_config.published' else
                {'credential_revision_id':row['aggregate_id'],'revocation_epoch':row['aggregate_version']})
            if row['payload']!=payload:fail(40923,'OUTBOX_EVENT_INVALID')
            if row['event_type']=='credential.revoked' and not (await db.execute(sa.select(t.ai_secrets.c.id).where(
                    scoped(t.ai_secrets),t.ai_secrets.c.id==row['aggregate_id'],t.ai_secrets.c.status=='revoked'))).first():
                fail(40923,'OUTBOX_TARGET_UNAVAILABLE')
            now=await self.control._now(db);new_id=uid()
            # Never reset attempts/lease on the old event: it is also a fencing
            # token for workers whose completion may still arrive late.
            await db.execute(table.insert().values(id=new_id,**SCOPE,event_key=child_key,
                event_type=row['event_type'],aggregate_id=row['aggregate_id'],aggregate_version=row['aggregate_version'],
                payload=payload,status='pending',attempts=0,next_attempt_at=now,created_at=now))
            result={'source_event_id':event_id,'event_id':new_id,'status':'pending','attempts':0,
                'original_attempts':row['attempts'],'delivery_guarantee':'at_least_once','config_head_changed':False}
            validate('OutboxRedriveResult',result)
            return result,new_id,{**result,'reason_code':body['reason']}
        return await self.control._mutate(db,actor,'outbox.redrive','config.outbox.manage',event_id,body,key,
            operation,proof=proof,sensitive=True)
