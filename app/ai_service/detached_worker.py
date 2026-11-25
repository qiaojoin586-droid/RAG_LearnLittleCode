"""Poll and claim durable chat jobs using database ownership."""

import asyncio
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

import sqlalchemy as sa

from app.ai_service.detached_chat import RUN_TIMEOUT_SECONDS, has_detached_chat, start_detached_chat
from app.ai_service.tool_operations import ToolTurn, authorize_turn
from app.ai_service.workflows import ChatWorkflow, LEASE, database_now, lock_detached_claims
from app.core.failed_response import BusinessError, ErrorCode
from app.core.logger_handler import logger
from app.core.model_trace import trace_scope
from app.core import task_runner
from app.model_config.durable_task_budget import DurableTaskModelBudget, create_budget, get_budget_row
from app.model_config.snapshots import inference_actor, load_snapshot, use_snapshot
from app.model_config.task_budget import TaskModelBudget, current_model_budget
from app.models.chat import ChatMessage, ChatSession
from app.models.user import User
from app.models.workflow_run import workflow_runs


MAX_ACTIVE_JOBS = 8
MAX_ATTEMPTS = 2
POLL_SECONDS = 0.5


async def lock_chat_admission(db, user_id):
    """Serialize enqueue for one account before reading or writing its turn."""
    status = await db.scalar(sa.select(User.status).where(User.uuid == user_id).with_for_update())
    if status != 'active':
        raise BusinessError(ErrorCode.NO_NOTE_ACCESS, http_status=403)
    table = workflow_runs
    message = ChatMessage.__table__
    session = ChatSession.__table__
    active = await db.scalar(sa.select(table.c.id).join(message, table.c.turn_message_id == message.c.id)
        .join(session, message.c.session_id == session.c.id).where(
            session.c.user_id == user_id, table.c.is_detached.is_(True), table.c.status == 'running',
            sa.or_(table.c.job_deadline_at > sa.func.current_timestamp(), sa.and_(
                table.c.job_deadline_at.is_(None), table.c.lease_expires_at > sa.func.current_timestamp())),
        ).limit(1))
    if active is not None:
        raise BusinessError(50306, message='已有后台对话在运行', http_status=503)


async def enqueue_chat(db, user_id, turn, release_id, *, thinking=False, attachment_only=False, allow_actions=False):
    await authorize_turn(db, user_id, turn)
    actor = inference_actor.get()
    budget = current_model_budget.get()
    now = await database_now(db)
    workflow_id = str(uuid4())
    await db.execute(workflow_runs.insert().values(
        id=workflow_id, turn_message_id=turn.message_id, release_id=release_id, is_detached=True,
        status='running', execution_attempt=0, finished_steps=[], state_version=1, created_at=now, updated_at=now,
        lease_expires_at=now, job_deadline_at=now + timedelta(seconds=RUN_TIMEOUT_SECONDS),
        job_payload={'thinking': thinking, 'attachment_only': attachment_only, 'jti': actor.get('jti'),
                     'expires': actor.get('expires'), 'retry': budget is None,
                     'allow_actions': allow_actions,
                     'budget_mode': 'persisted' if budget is not None else 'unlimited'},
    ))
    if budget is not None:
        await create_budget(db, workflow_id, budget)
    return workflow_id


@dataclass(frozen=True)
class ChatJob:
    workflow: ChatWorkflow
    turn_message_id: int
    release_id: str
    options: dict
    deadline: float
    created_at: datetime
    budget: TaskModelBudget | DurableTaskModelBudget | None = None


class DetachedChatWorker:
    def __init__(self, session_factory, producer):
        self.session_factory = session_factory
        self.producer = producer
        self.task = None
        self.running = set()

    async def claim(self):
        table = workflow_runs
        async with self.session_factory() as db:
            async with db.begin():
                if not await lock_detached_claims(db):
                    return None
                now = await database_now(db)
                active = await db.scalar(sa.select(sa.func.count()).select_from(table).where(
                    table.c.is_detached.is_(True), table.c.status == 'running', table.c.execution_attempt > 0,
                    table.c.lease_expires_at > now, table.c.job_deadline_at > now,
                ))
                due = table.c.job_deadline_at <= now
                if active < MAX_ACTIVE_JOBS:
                    due = sa.or_(due, table.c.lease_expires_at <= now)
                local_ids = [run.workflow.id for run in self.running]
                row = (await db.execute(sa.select(table).where(
                    table.c.is_detached.is_(True), table.c.status == 'running', table.c.execution_attempt.is_not(None),
                    due, table.c.id.not_in(local_ids),
                ).order_by(table.c.lease_expires_at, table.c.id).limit(1).with_for_update(skip_locked=True))).mappings().first()
                if row is None:
                    return None
                remaining = (row['job_deadline_at'] - now).total_seconds()
                attempt = row['execution_attempt']
                options = row['job_payload']
                reason = None
                budget_row = budget = None
                if remaining <= 0:
                    reason = 'DETACHED_DEADLINE_EXCEEDED'
                elif options.get('budget_mode') == 'persisted':
                    budget_row = await get_budget_row(db, row['id'], lock=True)
                    reason = budget_row['stopped_reason'] if budget_row is not None else 'MODEL_BUDGET_STORE_UNAVAILABLE'
                elif attempt < MAX_ATTEMPTS and options.get('budget_mode') != 'unlimited':
                    # Jobs accepted before durable budgets keep the original no-retry rule for capped work.
                    try:
                        budget = TaskModelBudget.configured()
                    except ValueError:
                        reason = 'MODEL_BUDGET_CONFIG_INVALID'
                    if reason is None and attempt and (not options['retry'] or budget is not None):
                        reason = 'DETACHED_RECOVERY_EXHAUSTED'
                if reason is None and attempt >= MAX_ATTEMPTS:
                    reason = 'DETACHED_RECOVERY_EXHAUSTED'
                values = {'state_version': row['state_version'] + 1, 'updated_at': now}
                if reason is not None:
                    await db.execute(table.update().where(table.c.id == row['id']).values(
                        **values, status='failed', error_code=reason, job_payload=sa.null(), plan_checkpoint=sa.null(),
                    ))
                    return None
                await db.execute(table.update().where(table.c.id == row['id']).values(
                    **values, execution_attempt=attempt + 1, lease_expires_at=now + LEASE, route=None, finished_steps=[],
                ))
                deadline = asyncio.get_running_loop().time() + remaining
                workflow = ChatWorkflow(row['id'], self.session_factory, attempt + 1)
                if budget_row is not None:
                    budget = DurableTaskModelBudget(workflow, budget_row)
                return ChatJob(workflow, row['turn_message_id'], row['release_id'], options, deadline, row['created_at'], budget)

    async def tick(self):
        if len(self.running) >= MAX_ACTIVE_JOBS or task_runner.active_task_count() >= task_runner.MAX_BACKGROUND_TASKS:
            return False
        job = await self.claim()
        if job is None:
            return False
        async with self.session_factory() as db:
            source = (await db.execute(sa.select(ChatSession.user_id, ChatMessage.session_id).join(
                ChatMessage, ChatMessage.session_id == ChatSession.id,
            ).where(ChatMessage.id == job.turn_message_id, ChatMessage.role == 'user'))).one_or_none()
        if source is None:
            await job.workflow.finish('cancelled', 'DETACHED_SOURCE_MISSING')
            return True
        if has_detached_chat(source.user_id):
            # An attempt that lost its lease may still be closing in this process.
            await job.workflow.relinquish()
            return False

        async def execute(workflow):
            actor = inference_actor.set({'user_id': source.user_id, 'jti': job.options['jti'],
                                         'expires': job.options['expires']})
            budget_token = current_model_budget.set(job.budget)
            try:
                async with self.session_factory() as db:
                    await authorize_turn(db, source.user_id, ToolTurn(source.session_id, job.turn_message_id))
                    snapshot = await load_snapshot(db, release_id=job.release_id)
                await workflow.assert_active()
                with use_snapshot(snapshot), trace_scope(request_id=workflow.id.replace('-', ''), user_id=source.user_id,
                                                         session_id=source.session_id, stage='chat'):
                    async with aclosing(self.producer(job, source.user_id, source.session_id)) as frames:
                        async for frame in frames:
                            yield frame
            except BusinessError as error:
                reason = (error.data or {}).get('reason', 'DETACHED_AUTHORIZATION_DENIED')
                await workflow.finish('failed' if error.http_status >= 500 else 'cancelled', reason)
            finally:
                current_model_budget.reset(budget_token)
                inference_actor.reset(actor)

        run = start_detached_chat(job.workflow, source.user_id, execute, deadline=job.deadline,
                                  allow_actions=job.options.get('allow_actions') is True)
        if run is None:
            await job.workflow.relinquish()
            return False
        self.running.add(run)
        run.task.add_done_callback(lambda _: self.running.discard(run))
        return True

    async def _run(self):
        while True:
            try:
                if await self.tick():
                    continue
            except Exception as error:
                logger.warning(f'后台对话 Worker 领取失败: error_type={type(error).__name__}')
            await asyncio.sleep(POLL_SECONDS)

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name='detached_chat_worker')

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.wait([self.task], timeout=2)
            self.task = None
