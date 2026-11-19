"""Persist an immutable proposal and apply only the owner's confirmed write."""

import asyncio
from contextvars import ContextVar
from datetime import timedelta
import json

import sqlalchemy as sa

from app.ai_service.email_dispatch import EMAIL_STATUS_MESSAGES, EMAIL_TOOL, lock_account_for_email, reserve_email, send_reserved_email, status_for
from app.ai_service.note_tool_writes import TRANSACTIONAL_TOOLS, apply_write, read_target, run_after_commit
from app.ai_service.tool_results import ARTIFACT_TOOLS, artifact_result, find_completed_artifact
from app.core.failed_response import BusinessError, ErrorCode
from app.model_config.contracts import content_hash
from app.models.chat import ChatMessage, ChatSession
from app.models.operation_approval import operation_approvals as approvals
from app.models.tool_operation import tool_operation_steps as steps, tool_operations as operations
from app.models.workflow_run import workflow_runs
from app.utils.time_utils import to_utc_iso


approval_workflow = ContextVar('tool_approval_workflow', default=None)
approval_step = ContextVar('tool_approval_step', default=0)
APPROVAL_TTL = timedelta(minutes=10)
MAX_ARGUMENTS_BYTES = 64 * 1024
APPROVAL_TOOLS = TRANSACTIONAL_TOOLS | {EMAIL_TOOL}


async def require_operation_workflow(db, workflow, turn):
    active = (await db.execute(sa.select(workflow_runs.c.is_detached, workflow_runs.c.job_payload).where(
        *workflow._active(sa.func.current_timestamp()), workflow_runs.c.turn_message_id == turn.message_id,
    ).with_for_update())).one_or_none()
    if active is None or (active.is_detached and (active.job_payload or {}).get('allow_actions') is not True):
        raise BusinessError(ErrorCode.INVALID_PARAMETER, message='当前会话不能申请写入', http_status=409)
    return approval_step.get() if active.is_detached else None


async def operation_step_results(db, workflow_id, user_id, turn, *, step=None):
    query = sa.select(steps.c.step_number, operations.c.operation_id, operations.c.tool_name,
        operations.c.result_ref, approvals.c.status, approvals.c.expires_at).join(
            operations, steps.c.operation_id == operations.c.operation_id,
        ).outerjoin(approvals, approvals.c.operation_id == operations.c.operation_id).where(
            steps.c.workflow_id == workflow_id, operations.c.user_id == user_id, operations.c.turn_message_id == turn.message_id,
        ).order_by(steps.c.step_number)
    if step is not None:
        query = query.where(steps.c.step_number == step)
    rows = (await db.execute(query)).mappings().all()
    if not rows:
        return {}
    now = await db.scalar(sa.select(sa.func.current_timestamp()))
    results = {}
    for row in rows:
        status = row['status'] or 'unknown'
        name, operation_id = row['tool_name'], row['operation_id']
        if name in ARTIFACT_TOOLS:
            reference = row['result_ref']
            if reference == {'state': 'unknown'}:
                reference = await asyncio.to_thread(find_completed_artifact, name, user_id, operation_id)
            results[row['step_number']] = artifact_result(name, reference, operation_id)
            continue
        if status == 'pending' and row['expires_at'] <= now:
            status = 'expired'
        if status == 'executed' and name == EMAIL_TOOL:
            status = await status_for(db, operation_id)
        message = EMAIL_STATUS_MESSAGES.get(status) if name == EMAIL_TOOL else None
        message = message or {
            'pending': '操作等待用户确认。请在待确认操作中核对。',
            'executed': '操作已确认并完成。',
            'rejected': '用户已拒绝此操作。',
            'expired': '操作确认期限已过。',
            'conflict': '目标内容已变化，此操作未执行。',
            'unknown': '操作结果待核对。',
        }[status]
        results[row['step_number']] = (f'{name}：{message}', {
            'operation_id': operation_id, 'approval_status': status, 'replayed': True,
        })
    return results


async def bind_operation_step(db, workflow_id, step, operation_id):
    if step is not None:
        await db.execute(steps.insert().values(workflow_id=workflow_id, step_number=step, operation_id=operation_id))


def _end(status, now):
    return {'status': status, 'decided_at': now, 'arguments': None, 'target': None}


def _preview_digest(operation_id, workflow_id, name, expires_at, arguments, target):
    return content_hash({'operation_id': operation_id, 'workflow_id': workflow_id, 'tool_name': name,
                         'expires': to_utc_iso(expires_at), 'arguments': arguments, 'target': target})


async def propose(db, operation_id, workflow_id, user_id, name, arguments):
    row = (await db.execute(sa.select(approvals).where(approvals.c.operation_id == operation_id))).mappings().one_or_none()
    now = await db.scalar(sa.select(sa.func.current_timestamp()))
    if row is None:
        if len(json.dumps(arguments, ensure_ascii=False).encode('utf-8')) > MAX_ARGUMENTS_BYTES:
            raise BusinessError(ErrorCode.INVALID_PARAMETER, message='待确认的写入内容过长', http_status=422)
        target = {'recipient': arguments['to']} if name == EMAIL_TOOL else await read_target(db, user_id, name, arguments)
        expiry = now + APPROVAL_TTL
        preview = _preview_digest(operation_id, workflow_id, name, expiry, arguments, target)
        await db.execute(approvals.insert().values(operation_id=operation_id, workflow_id=workflow_id, status='pending',
                         preview_hash=preview, arguments=arguments, target=target, expires_at=expiry))
        status = 'pending'
    else:
        status = row['status']
        if status == 'pending' and row['expires_at'] <= now:
            status = 'expired'
            await db.execute(approvals.update().where(approvals.c.operation_id == operation_id).values(**_end(status, now)))
    if name == EMAIL_TOOL:
        message = ('邮件正在等待用户确认，尚未发送。请提示用户在待确认操作中核对收件信息。' if status == 'pending'
                   else '此邮件申请已结束，未通过该申请发送邮件。')
    else:
        message = ('操作正在等待用户确认，业务数据尚未写入。请提示用户在待确认操作中查看。' if status == 'pending'
                   else '此写入申请已结束，业务数据未通过该申请写入。')
    return message, {'operation_id': operation_id, 'approval_status': status}


async def list_approvals(db, user_id, session_id):
    source = ChatMessage.__table__
    chat = ChatSession.__table__
    owned = await db.scalar(sa.select(chat.c.id).where(chat.c.id == session_id, chat.c.user_id == user_id))
    if owned is None:
        raise BusinessError(ErrorCode.SESSION_NOT_FOUND, http_status=404)
    rows = (await db.execute(sa.select(approvals.c.operation_id, approvals.c.preview_hash, approvals.c.arguments,
                                     approvals.c.target, approvals.c.expires_at, operations.c.tool_name)
        .join(operations, approvals.c.operation_id == operations.c.operation_id)
        .join(source, operations.c.turn_message_id == source.c.id)
        .where(operations.c.user_id == user_id, source.c.session_id == session_id, source.c.role == 'user',
               approvals.c.status == 'pending', approvals.c.expires_at > sa.func.current_timestamp())
        .order_by(approvals.c.expires_at, approvals.c.operation_id).limit(20))).mappings().all()
    return [{**row, 'expires_at': to_utc_iso(row['expires_at'])} for row in rows]


async def decide_approval(factory, user_id, operation_id, preview_hash, decision, note_service, review_service, *,
                          email_service_factory=None):
    from app.ai_service.tool_operations import authorize_turn, owned_operation_turn

    if decision not in ('approve', 'reject'):
        raise BusinessError(ErrorCode.INVALID_PARAMETER, http_status=422)
    async with factory() as db:
        turn, name = await owned_operation_turn(db, user_id, operation_id)

    effects = []
    reserved = service = None
    async with factory() as db:
        async with db.begin():
            if name == EMAIL_TOOL and decision == 'approve':
                await lock_account_for_email(db, user_id)
            await authorize_turn(db, user_id, turn)
            operation = (await db.execute(sa.select(operations).where(
                operations.c.operation_id == operation_id, operations.c.user_id == user_id,
            ).with_for_update())).mappings().one_or_none()
            proposal = (await db.execute(sa.select(approvals).where(
                approvals.c.operation_id == operation_id,
            ).with_for_update())).mappings().one_or_none()
            if proposal is None or operation is None or operation['tool_name'] != name or name not in APPROVAL_TOOLS:
                raise BusinessError(ErrorCode.SESSION_NOT_FOUND, message='待确认操作不存在', http_status=404)
            if proposal['preview_hash'] != preview_hash:
                raise BusinessError(ErrorCode.INVALID_PARAMETER, message='操作内容已变动，请刷新后重新核对', http_status=409)
            status, result = proposal['status'], operation['result_ref']
            if status == 'pending':
                now = await db.scalar(sa.select(sa.func.current_timestamp()))
                if now >= proposal['expires_at']:
                    status = 'expired'
                elif decision == 'reject':
                    status = 'rejected'
                elif name == EMAIL_TOOL:
                    arguments = _confirmed_arguments(proposal, operation)
                    service = email_service_factory() if email_service_factory else None
                    reserved, result = await reserve_email(db, operation_id, user_id, turn, arguments,
                                                           service, proposal['expires_at'])
                    status = 'executed' if reserved else 'expired'
                else:
                    status, result = await _execute_confirmed(db, user_id, proposal, operation, effects, note_service, review_service)
                await db.execute(approvals.update().where(approvals.c.operation_id == operation_id).values(**_end(status, now)))
    await run_after_commit(operation['tool_name'], effects)
    if name == EMAIL_TOOL and status == 'executed':
        if reserved is not None:
            await send_reserved_email(factory, reserved, service)
        async with factory() as db:
            status = await status_for(db, operation_id)
    return {'operation_id': operation_id, 'status': status, 'result': result}


def _confirmed_arguments(proposal, operation):
    name, arguments = operation['tool_name'], proposal['arguments']
    preview = _preview_digest(operation['operation_id'], proposal['workflow_id'], name,
                              proposal['expires_at'], arguments, proposal['target'])
    if preview != proposal['preview_hash'] or content_hash(arguments) != operation['arguments_hash']:
        raise BusinessError(ErrorCode.INVALID_PARAMETER, message='待确认内容校验失败', http_status=409)
    return arguments


async def _execute_confirmed(db, user_id, proposal, operation, effects, note_service, review_service):
    name, arguments = operation['tool_name'], _confirmed_arguments(proposal, operation)
    try:
        target = await read_target(db, user_id, name, arguments)
    except BusinessError as error:
        if error.http_status in (404, 409):
            return 'conflict', None
        raise
    if target != proposal['target']:
        return 'conflict', None
    if await db.scalar(sa.select(sa.func.current_timestamp())) >= proposal['expires_at']:
        return 'expired', None
    _, result = await apply_write(db, user_id, name, arguments, effects, note_service, review_service, target=target)
    await db.execute(operations.update().where(operations.c.operation_id == operation['operation_id']).values(result_ref=result))
    return 'executed', result


async def expire_approvals(factory):
    async with factory() as db:
        async with db.begin():
            now = sa.func.current_timestamp()
            await db.execute(approvals.update().where(approvals.c.status == 'pending', approvals.c.expires_at <= now)
                             .values(**_end('expired', now)))
