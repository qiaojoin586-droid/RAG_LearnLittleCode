"""Execute chat-scoped SQL writes and their receipts in one transaction."""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import json
import time
from uuid import uuid4

import sqlalchemy as sa
from langchain_core.messages import ToolMessage
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.exc import SQLAlchemyError

from app.ai_service.email_dispatch import EMAIL_STATUS_MESSAGES, EMAIL_TOOL, status_for
from app.ai_service.note_tool_writes import TRANSACTIONAL_TOOLS, run_after_commit  # noqa: F401
from app.ai_service.tool_approvals import (
    approval_workflow, bind_operation_step, operation_step_results, propose, require_operation_workflow,
)
from app.ai_service.tool_results import artifact_file_event, artifact_output_id, artifact_result, find_completed_artifact
from app.core.failed_response import BusinessError, ErrorCode
from app.core.logger_handler import logger
from app.model_config.contracts import content_hash
from app.model_config.snapshots import current_snapshot, inference_actor
from app.models.chat import ChatMessage, ChatSession
from app.models.tool_operation import OPERATION_SCOPE, tool_operations
from app.models.user import User


@dataclass(frozen=True)
class ToolTurn:
    session_id: str
    message_id: int


tool_turn: ContextVar[ToolTurn | None] = ContextVar('tool_operation_turn', default=None)


async def owned_operation_turn(db, user_id, operation_id):
    source = ChatMessage.__table__
    row = (await db.execute(sa.select(source.c.session_id, source.c.id, tool_operations.c.tool_name).join(
        tool_operations, tool_operations.c.turn_message_id == source.c.id,
    ).where(tool_operations.c.operation_id == operation_id, tool_operations.c.user_id == user_id))).one_or_none()
    if row is None:
        raise BusinessError(ErrorCode.SESSION_NOT_FOUND, message='待确认操作不存在', http_status=404)
    return ToolTurn(row.session_id, row.id), row.tool_name


async def authorize_turn(db, user_id, turn):
    actor = inference_actor.get()
    if not actor or actor.get('user_id') != user_id:
        raise BusinessError(ErrorCode.TOKEN_INVALID, http_status=401)
    if actor.get('expires') is not None and actor['expires'] <= time.time():
        raise BusinessError(ErrorCode.TOKEN_EXPIRED, http_status=401)
    if actor.get('jti'):
        from app.utils.auth_utils import is_token_blacklisted
        if await is_token_blacklisted(actor['jti']):
            raise BusinessError(ErrorCode.TOKEN_INVALID, http_status=401)
    status = await db.scalar(sa.select(User.status).where(User.uuid == user_id).with_for_update(read=True))
    if status != 'active':
        raise BusinessError(ErrorCode.NO_NOTE_ACCESS, message='账户当前不能执行写入', http_status=403)
    message = await db.scalar(sa.select(ChatMessage.id).join(ChatSession).where(
        ChatMessage.id == turn.message_id, ChatMessage.role == 'user',
        ChatSession.id == turn.session_id, ChatSession.user_id == user_id,
    ).with_for_update(read=True))
    if message is None:
        raise BusinessError(ErrorCode.SESSION_NOT_FOUND, http_status=404)


async def claim(db, user_id, turn, name, arguments):
    scope = {'user_id': user_id, 'turn_message_id': turn.message_id, 'tool_name': name,
             'arguments_hash': content_hash(arguments)}
    proposed = str(uuid4())
    snapshot = current_snapshot()
    values = {**scope, 'operation_id': proposed, 'release_id': snapshot.release_id if snapshot else None}
    table = tool_operations
    if db.get_bind().dialect.name == 'sqlite':
        statement = sqlite.insert(table).values(**values).on_conflict_do_nothing(index_elements=OPERATION_SCOPE)
    else:
        statement = mysql.insert(table).values(**values).on_duplicate_key_update(operation_id=table.c.operation_id)
    await db.execute(statement)
    receipt = (await db.execute(sa.select(table.c.operation_id, table.c.result_ref).where(
        *(table.c[field] == value for field, value in scope.items())
    ).with_for_update())).one()
    return receipt, receipt.operation_id == proposed


async def run_tool_write(db_factory, user_id, name, arguments, write, read_result):
    """Return a LangChain content/artifact pair with a server-generated operation ID."""
    turn = tool_turn.get()
    after_commit = []
    artifact = None
    try:
        async with db_factory() as db:
            async with db.begin():
                receipt = None
                if turn is not None:
                    await authorize_turn(db, user_id, turn)
                    workflow = approval_workflow.get()
                    step = None
                    if workflow is not None:
                        step = await require_operation_workflow(db, workflow, turn)
                        if step is not None:
                            existing = await operation_step_results(db, workflow.id, user_id, turn, step=step)
                            if existing:
                                return existing[step]
                    receipt, fresh = await claim(db, user_id, turn, name, arguments)
                    artifact = {'operation_id': receipt.operation_id, 'replayed': not fresh}
                    if step is not None:
                        await bind_operation_step(db, workflow.id, step, receipt.operation_id)
                    if workflow is not None and receipt.result_ref is None:
                        return await propose(db, receipt.operation_id, workflow.id, user_id, name, arguments)
                    if not fresh:
                        if receipt.result_ref is None:
                            raise BusinessError(ErrorCode.INTERNAL_ERROR, message='工具操作结果无法确认', http_status=503)
                        return await read_result(db, receipt.result_ref), artifact
                result, result_ref = await write(db, after_commit)
                if receipt is not None:
                    await db.execute(tool_operations.update().where(
                        tool_operations.c.operation_id == receipt.operation_id
                    ).values(result_ref=result_ref))
            await run_after_commit(name, after_commit)
    except SQLAlchemyError as error:
        logger.warning(f'工具事务失败: tool={name}, error_type={type(error).__name__}')
        raise BusinessError(ErrorCode.INTERNAL_ERROR, message='工具操作结果暂时无法确认', http_status=503) from None
    return result, artifact


async def request_email_approval(db_factory, user_id, arguments):
    turn, workflow = tool_turn.get(), approval_workflow.get()
    if turn is None or workflow is None or db_factory is None:
        raise BusinessError(ErrorCode.INVALID_PARAMETER, message='请在有审批入口的对话中申请发邮件', http_status=409)
    async with db_factory() as db:
        async with db.begin():
            await authorize_turn(db, user_id, turn)
            step = await require_operation_workflow(db, workflow, turn)
            if step is not None:
                existing = await operation_step_results(db, workflow.id, user_id, turn, step=step)
                if existing:
                    return existing[step]
            receipt, _ = await claim(db, user_id, turn, EMAIL_TOOL, arguments)
            await bind_operation_step(db, workflow.id, step, receipt.operation_id)
            if receipt.result_ref is not None:
                status = await status_for(db, receipt.operation_id)
                return EMAIL_STATUS_MESSAGES[status], {'operation_id': receipt.operation_id, 'approval_status': status}
            return await propose(db, receipt.operation_id, workflow.id, user_id, EMAIL_TOOL, arguments)


async def run_artifact_tool(workflow, user_id, turn, call, execute, *, on_reserved=None):
    name = call['name']
    async with workflow.session_factory() as db:
        async with db.begin():
            await authorize_turn(db, user_id, turn)
            step = await require_operation_workflow(db, workflow, turn)
            if step is not None:
                prior = await operation_step_results(db, workflow.id, user_id, turn, step=step)
                if prior:
                    message, artifact = prior[step]
                    return ToolMessage(content=message, artifact=artifact, name=name, tool_call_id=call['id'])
            receipt, fresh = await claim(db, user_id, turn, name, call['args'])
            await bind_operation_step(db, workflow.id, step, receipt.operation_id)
            if not fresh:
                reference = receipt.result_ref
                if reference == {'state': 'unknown'}:
                    reference = await asyncio.to_thread(find_completed_artifact, name, user_id, receipt.operation_id)
                message, artifact = artifact_result(name, reference, receipt.operation_id)
                return ToolMessage(content=message, artifact=artifact, name=name, tool_call_id=call['id'])
            await db.execute(tool_operations.update().where(tool_operations.c.operation_id == receipt.operation_id)
                             .values(result_ref={'state': 'unknown'}))

    if on_reserved is not None:
        await on_reserved(receipt.operation_id)
    identity = artifact_output_id.set(receipt.operation_id.replace('-', ''))
    try:
        output = await execute()
    finally:
        artifact_output_id.reset(identity)
    try:
        details = json.loads(output.content) if isinstance(output, ToolMessage) else None
    except (ValueError, TypeError):
        return output
    event = artifact_file_event(name, details, receipt.operation_id)
    if event is None or event['file_id'] != receipt.operation_id.replace('-', '') or output.status == 'error':
        return output
    reference = {key: event[key] for key in ('file_id', 'slide_count') if key in event}
    async with workflow.session_factory() as db:
        async with db.begin():
            await authorize_turn(db, user_id, turn)
            await require_operation_workflow(db, workflow, turn)
            await db.execute(tool_operations.update().where(tool_operations.c.operation_id == receipt.operation_id)
                             .values(result_ref=reference))
    return output.model_copy(update={'artifact': {'operation_id': receipt.operation_id, 'file': event}})
