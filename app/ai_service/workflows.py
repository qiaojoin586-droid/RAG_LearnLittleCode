"""Persist workflow progress without serializing the LangGraph runtime state."""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.ai_service.tool_operations import authorize_turn
from app.core.failed_response import BusinessError, ErrorCode
from app.core.logger_handler import logger
from app.model_config.security import scoped
from app.model_config.tables import ai_config_heads
from app.models.chat import ChatMessage, ChatSession
from app.models.workflow_run import workflow_runs
from app.utils.time_utils import to_utc_iso


LEASE = timedelta(seconds=45)
HEARTBEAT_SECONDS = 10


async def database_now(db):
    return await db.scalar(sa.select(sa.func.current_timestamp()))


async def lock_detached_claims(db):
    """Serialize capacity changes on the scope row required by published chat jobs."""
    return await db.scalar(sa.select(ai_config_heads.c.head_version).where(
        scoped(ai_config_heads)).with_for_update()) is not None


def lease_lost():
    return BusinessError(ErrorCode.INTERNAL_ERROR, message='对话执行检查点已失效，请重试',
                         http_status=503, data={'reason': 'WORKFLOW_LEASE_LOST'})


@dataclass(frozen=True)
class ChatWorkflow:
    id: str
    session_factory: object
    attempt: int | None = None

    @classmethod
    async def start(cls, session_factory, user_id, turn, release_id, *, detached=False):
        run = cls(str(uuid4()), session_factory)
        async with session_factory() as db:
            async with db.begin():
                await authorize_turn(db, user_id, turn)
                now = await database_now(db)
                await db.execute(workflow_runs.insert().values(
                    id=run.id, turn_message_id=turn.message_id, release_id=release_id, status='running', is_detached=detached,
                    finished_steps=[], state_version=1, created_at=now, updated_at=now, lease_expires_at=now + LEASE,
                ))
        return run

    def _active(self, now):
        table = workflow_runs
        return (table.c.id == self.id, table.c.status == 'running', table.c.lease_expires_at > now,
                table.c.execution_attempt == self.attempt,
                sa.or_(table.c.job_deadline_at.is_(None), table.c.job_deadline_at > now))

    async def assert_active(self):
        async with self.session_factory() as db:
            if await db.scalar(sa.select(workflow_runs.c.id).where(*self._active(sa.func.current_timestamp()))) is None:
                raise lease_lost()

    async def execution_state(self):
        table = workflow_runs
        now = sa.func.current_timestamp()
        status = sa.case(
            ((table.c.status == 'running') & sa.or_(table.c.lease_expires_at <= now,
                table.c.execution_attempt.is_distinct_from(self.attempt), table.c.job_deadline_at <= now), 'interrupted'),
            else_=table.c.status,
        )
        async with self.session_factory() as db:
            return await db.scalar(sa.select(status).where(table.c.id == self.id))

    async def cancel_in(self, db):
        table = workflow_runs
        now = sa.func.current_timestamp()
        result = await db.execute(table.update().where(
            table.c.id == self.id, table.c.status == 'running', table.c.is_detached.is_(True),
            sa.or_(table.c.job_deadline_at > now,
                   sa.and_(table.c.job_deadline_at.is_(None), table.c.lease_expires_at > now)),
        ).values(status='cancelled', error_code='USER_CANCELLED', job_payload=sa.null(), plan_checkpoint=sa.null(),
                 state_version=table.c.state_version + 1, updated_at=now))
        return result.rowcount == 1

    async def _checkpoint(self, db, now, **values):
        table = workflow_runs
        # Evaluate lease ownership at the write, even if an attempt had to wait on locks.
        changed = await db.execute(table.update().where(*self._active(sa.func.current_timestamp())).values(
            **values, state_version=table.c.state_version + 1, updated_at=now,
        ))
        return changed.rowcount == 1

    async def record_route(self, route):
        if route not in ('react', 'plan_execute'):
            raise ValueError('Unknown chat workflow route')
        async with self.session_factory() as db:
            async with db.begin():
                now = await database_now(db)
                if not await self._checkpoint(db, now, route=route):
                    raise lease_lost()

    async def record_step_end(self, step):
        if type(step) is not int or not 1 <= step <= 2**31 - 1:
            raise ValueError('Invalid plan step checkpoint')
        async with self.session_factory() as db:
            async with db.begin():
                table = workflow_runs
                now = await database_now(db)
                steps = await db.scalar(sa.select(table.c.finished_steps).where(*self._active(now)).with_for_update())
                if steps is None:
                    raise lease_lost()
                if step not in steps:
                    await self._checkpoint(db, now, finished_steps=sorted([*steps, step]))

    async def succeed_in(self, db, reply_message_id=None):
        """Commit the checkpoint together with the assistant message when present."""
        if not await self._checkpoint(db, await database_now(db), status='succeeded', reply_message_id=reply_message_id,
                                      job_payload=sa.null(), plan_checkpoint=sa.null()):
            raise lease_lost()

    async def finish(self, status, error_code=None):
        """Best-effort terminal bookkeeping that never replaces a committed result."""
        try:
            async with self.session_factory() as db:
                async with db.begin():
                    await self._checkpoint(db, await database_now(db), status=status, error_code=error_code,
                                           job_payload=sa.null(), plan_checkpoint=sa.null())
        except SQLAlchemyError as error:
            logger.warning(f'对话终态检查点写入失败: error_type={type(error).__name__}')

    async def relinquish(self):
        async with self.session_factory() as db:
            async with db.begin():
                await db.execute(workflow_runs.update().where(*self._active(sa.func.current_timestamp())).values(
                    lease_expires_at=sa.func.current_timestamp(),
                ))

    async def heartbeat(self):
        table = workflow_runs
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                async with self.session_factory() as db:
                    async with db.begin():
                        # A renewal that races an expired lease must not add a slot
                        # after another Worker has already counted it as free.
                        if self.attempt is not None and not await lock_detached_claims(db):
                            return
                        now = await database_now(db)
                        result = await db.execute(table.update().where(*self._active(sa.func.current_timestamp())).values(
                            lease_expires_at=now + LEASE))
                        if result.rowcount != 1:
                            return
            except SQLAlchemyError as error:
                logger.warning(f'对话检查点续租失败: error_type={type(error).__name__}')


async def read_workflow(db, user_id, workflow_id):
    turn = ChatMessage.__table__
    session = ChatSession.__table__
    public_columns = [column for column in workflow_runs.c if column.name not in {'job_payload', 'plan_checkpoint'}]
    query = sa.select(*public_columns, session.c.id.label('session_id')).select_from(
        workflow_runs.join(turn, workflow_runs.c.turn_message_id == turn.c.id).join(session, turn.c.session_id == session.c.id)
    ).where(workflow_runs.c.id == workflow_id, session.c.user_id == user_id, turn.c.role == 'user')
    row = (await db.execute(query)).mappings().one_or_none()
    if row is None:
        raise BusinessError(ErrorCode.SESSION_NOT_FOUND, message='对话运行记录不存在', http_status=404)
    result = dict(row)
    if result['status'] == 'running':
        now = await database_now(db)
        deadline = result['job_deadline_at']
        if deadline is not None and deadline <= now:
            result.update(status='failed', error_code='DETACHED_DEADLINE_EXCEEDED')
        elif deadline is None and result['lease_expires_at'] <= now:
            result.update(status='interrupted', error_code='WORKFLOW_LEASE_EXPIRED')
    for column in ('created_at', 'updated_at', 'lease_expires_at', 'job_deadline_at'):
        if result[column] is None:
            continue
        result[column] = to_utc_iso(result[column])
    return result
