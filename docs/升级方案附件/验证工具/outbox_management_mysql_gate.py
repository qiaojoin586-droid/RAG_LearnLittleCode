"""Append-only redrive and late-completion fencing on disposable MySQL."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import threading

import sqlalchemy as sa
from sqlalchemy.orm import Session


def run_outbox_gate(engine,Database,actor,control,report):
    from app.core.failed_response import BusinessError
    from app.model_config import tables as t
    from app.model_config.outbox import ConfigOutboxWorker
    from app.model_config.outbox_service import OutboxService
    from app.model_config.security import SCOPE,issue_proof
    from app.model_config.service import uid,utcnow
    from datetime import timedelta
    service=OutboxService(control);proof=issue_proof(control.keyring,actor,1)['proof']
    @asynccontextmanager
    async def database():
        with Session(engine) as session:yield Database(session)
    with engine.connect() as connection:
        head=dict(connection.execute(sa.select(t.ai_config_heads)).mappings().one())
        secrets=[dict(row) for row in connection.execute(sa.select(t.ai_secrets)).mappings().all()]
    def fixture():
        identifier=uid();now=utcnow()
        with engine.begin() as connection:
            connection.execute(t.outbox_events.insert().values(id=identifier,**SCOPE,event_key='fixture:'+identifier,
                event_type='model_config.published',aggregate_id=head['active_release_id'],aggregate_version=head['head_version'],
                payload={'release_id':head['active_release_id'],'head_version':head['head_version']},
                status='dead_letter',attempts=5,error_code='CONFIG_NOTIFY_FAILED',next_attempt_at=now,created_at=now))
        return identifier
    def redrive(identifier,key):
        with Session(engine) as session:
            return asyncio.run(service.redrive(Database(session),actor,identifier,
                {'expected_attempts':5,'reason':'operator_retry'},key,proof))[0]
    source=fixture()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(redrive,source,'outbox-concurrent');second=pool.submit(redrive,source,'outbox-concurrent')
        result=first.result();assert second.result()==result
    try:redrive(source,'outbox-another-key')
    except BusinessError as error:assert error.data['reason']=='OUTBOX_ALREADY_REDRIVEN'
    else:raise AssertionError('A source dead letter may have only one redrive')
    with engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(t.outbox_events).where(
            t.outbox_events.c.event_key=='redrive:'+source)).scalar_one()==1
        original=connection.execute(sa.select(t.outbox_events).where(t.outbox_events.c.id==source)).mappings().one()
        assert original['status']=='dead_letter' and original['attempts']==5
    report['checks']['outbox_mysql_concurrent_redrive_replay_creates_one_child_preserves_source']=True
    failed=fixture();original_audit=control._audit
    async def broken(*args,**kwargs):raise RuntimeError('simulated audit failure')
    control._audit=broken
    try:
        try:redrive(failed,'outbox-atomic-failure')
        except RuntimeError:pass
        else:raise AssertionError('Audit failure must roll back redrive')
    finally:control._audit=original_audit
    with engine.connect() as connection:
        assert not connection.execute(sa.select(t.outbox_events.c.id).where(t.outbox_events.c.event_key=='redrive:'+failed)).first()
        assert not connection.execute(sa.select(t.ai_config_requests.c.id).where(t.ai_config_requests.c.request_key=='outbox-atomic-failure')).first()
    report['checks']['outbox_mysql_audit_failure_rolls_back_child_and_request']=True
    entered=threading.Event();resume=threading.Event()
    async def delayed(event):
        assert event['id']==result['event_id'];entered.set()
        if not await asyncio.to_thread(resume.wait,5):raise AssertionError('Gate completion signal timed out')
        raise RuntimeError('late old attempt must not overwrite successful replacement')
    with ThreadPoolExecutor(max_workers=1) as pool:
        late=pool.submit(lambda:asyncio.run(ConfigOutboxWorker(control,database,delayed).tick()))
        try:
            assert entered.wait(5)
            with engine.begin() as connection:
                connection.execute(t.outbox_events.update().where(t.outbox_events.c.id==result['event_id']).values(
                    lease_expires_at=utcnow()-timedelta(seconds=1)))
            assert asyncio.run(ConfigOutboxWorker(control,database).tick())
        finally:resume.set()
        assert late.result()
    with engine.connect() as connection:
        child=connection.execute(sa.select(t.outbox_events).where(t.outbox_events.c.id==result['event_id'])).mappings().one()
        assert child['status']=='succeeded' and child['attempts']==2 and child['error_code'] is None
        assert connection.execute(sa.select(t.ai_config_heads.c.head_version)).scalar_one()==head['head_version']
        assert [dict(row) for row in connection.execute(sa.select(t.ai_secrets)).mappings().all()]==secrets
    report['checks']['outbox_mysql_expired_attempt_reclaimed_late_failure_cannot_override_success']=True
    report['checks']['outbox_mysql_redrive_never_moves_head_or_restores_revoked_credentials']=True
