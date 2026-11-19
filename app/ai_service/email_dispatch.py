"""Track a confirmed SMTP attempt through acceptance or manual reconciliation."""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import html
import re

import aiosmtplib
from anyio import move_on_after
import sqlalchemy as sa

from app.core.failed_response import BusinessError, ErrorCode
from app.core.logger_handler import logger
from app.models.chat import ChatMessage, ChatSession
from app.models.email_dispatch import email_dispatches as dispatches
from app.models.tool_operation import tool_operations as operations
from app.models.user import User
from app.utils.time_utils import to_utc_iso


EMAIL_TOOL = 'send_email'
EMAIL_PATTERN = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
DISPATCH_TIMEOUT = 30
RESERVATION_TTL = timedelta(seconds=DISPATCH_TIMEOUT + 15)
RATE_WINDOW = timedelta(hours=1)
EMAIL_STATUS_MESSAGES = {
    'dispatching': '邮件已确认，等待 SMTP 结果。',
    'accepted': '邮件已由 SMTP 服务受理，收件情况可在助手中核对。',
    'unknown': '邮件发送结果待核对。请按邮件 Message-ID 核对收件情况。',
    'failed': '邮件未由 SMTP 服务受理。',
    'received': '用户已核实收件。',
}


def email_arguments(to, subject, body, format):
    to, subject = to.strip(), subject.strip()
    if (not EMAIL_PATTERN.fullmatch(to) or len(to) > 254 or not 1 <= len(subject) <= 200
            or any(ord(char) < 32 or ord(char) == 127 for char in subject)
            or len(body) > 50000 or format not in ('md', 'pdf', 'text')):
        raise BusinessError(ErrorCode.INVALID_PARAMETER, message='请核对收件地址、邮件主题、正文长度及格式', http_status=422)
    return {'to': to, 'subject': subject, 'body': body, 'format': format}


def current_status():
    return sa.case((sa.and_(dispatches.c.status == 'dispatching',
                            dispatches.c.deadline_at <= sa.func.current_timestamp()), 'unknown'),
                   else_=dispatches.c.status)


async def status_for(db, operation_id):
    return await db.scalar(sa.select(current_status()).where(dispatches.c.operation_id == operation_id)) or 'unknown'


async def lock_account_for_email(db, user_id):
    await db.scalar(sa.select(User.uuid).where(User.uuid == user_id).with_for_update())


@dataclass(frozen=True)
class ReservedEmail:
    operation_id: str
    user_id: str
    turn: object
    arguments: dict
    message_id: str


async def reserve_email(db, operation_id, user_id, turn, arguments, service, expires_at):
    from app.utils.config import get_security_config

    if not service or not service.available:
        raise BusinessError(ErrorCode.INTERNAL_ERROR, message='SMTP 邮件服务未就绪', http_status=503)
    now = await db.scalar(sa.select(sa.func.current_timestamp()))
    if now >= expires_at:
        return None, None
    count = await db.scalar(sa.select(sa.func.count()).select_from(dispatches).where(
        dispatches.c.user_id == user_id, dispatches.c.started_at > now - RATE_WINDOW,
    ))
    if count >= get_security_config().get('email_rate_limit_per_hour', 10):
        raise BusinessError(ErrorCode.INVALID_PARAMETER, message='邮件发送频次已达到上限，请稍后确认', http_status=429)
    message_id = f'<{operation_id}@raglearn.local>'
    result = {'message_id': message_id, 'to': arguments['to'], 'subject': arguments['subject'], 'format': arguments['format']}
    await db.execute(dispatches.insert().values(operation_id=operation_id, user_id=user_id, status='dispatching',
                     started_at=now, updated_at=now, deadline_at=now + RESERVATION_TTL))
    await db.execute(operations.update().where(operations.c.operation_id == operation_id).values(result_ref=result))
    return ReservedEmail(operation_id, user_id, turn, arguments, message_id), result


async def _attachments(service, arguments):
    fmt, subject, body = arguments['format'], arguments['subject'], arguments['body']
    if fmt == 'text':
        return None
    filename = re.sub(r'[\\/:*?"<>|]', '_', subject)
    if fmt == 'md':
        return [{'filename': f'{filename}.md', 'data': body.encode('utf-8'), 'mime': 'text/markdown'}]
    # Treat raw HTML in a proposed Markdown input as text within the PDF renderer.
    data = await asyncio.to_thread(service.render_markdown_pdf, subject, html.escape(body, quote=False))
    return [{'filename': f'{filename}.pdf', 'data': data, 'mime': 'application/pdf'}]


async def send_reserved_email(factory, reserved, service):
    """Only the request that committed the reservation can call SMTP."""
    from app.ai_service.tool_operations import authorize_turn

    outcome = 'failed'
    try:
        async with asyncio.timeout(DISPATCH_TIMEOUT):
            attachments = await _attachments(service, reserved.arguments)
            async with factory() as db:
                async with db.begin():
                    await authorize_turn(db, reserved.user_id, reserved.turn)
                    if await status_for(db, reserved.operation_id) != 'dispatching':
                        outcome = 'unknown'
                        return outcome
            outcome = 'unknown'
            await service.send_email(reserved.arguments['to'], reserved.arguments['subject'], reserved.arguments['body'],
                                     attachments=attachments, retries=0, message_id=reserved.message_id)
            outcome = 'accepted'
    except (aiosmtplib.SMTPRecipientsRefused, aiosmtplib.SMTPSenderRefused, aiosmtplib.SMTPDataError,
            aiosmtplib.SMTPAuthenticationError):
        outcome = 'failed'
    except Exception as error:
        logger.warning(f'审批邮件发送阶段中止: operation_id={reserved.operation_id}, error_type={type(error).__name__}')
    finally:
        with move_on_after(2, shield=True):
            async with factory() as db:
                async with db.begin():
                    await db.execute(dispatches.update().where(dispatches.c.operation_id == reserved.operation_id,
                        dispatches.c.status.in_(('dispatching', 'unknown'))).values(
                            status=outcome, updated_at=sa.func.current_timestamp(),
                        ))
    return outcome


async def list_email_operations(db, user_id, session_id):
    source = ChatMessage.__table__
    chat = ChatSession.__table__
    if await db.scalar(sa.select(chat.c.id).where(chat.c.id == session_id, chat.c.user_id == user_id)) is None:
        raise BusinessError(ErrorCode.SESSION_NOT_FOUND, http_status=404)
    status = current_status()
    needs_review = sa.case((status.in_(('unknown', 'dispatching')), 0), else_=1)
    rows = (await db.execute(sa.select(dispatches.c.operation_id, status.label('status'),
                        dispatches.c.started_at, operations.c.result_ref)
        .join(operations, operations.c.operation_id == dispatches.c.operation_id)
        .join(source, source.c.id == operations.c.turn_message_id)
        .where(dispatches.c.user_id == user_id, operations.c.user_id == user_id,
               source.c.session_id == session_id, source.c.role == 'user')
        .order_by(needs_review, dispatches.c.started_at.desc(), dispatches.c.operation_id).limit(20))).mappings().all()
    return [{'operation_id': row['operation_id'], 'status': row['status'], 'result': row['result_ref'],
             'started_at': to_utc_iso(row['started_at'])} for row in rows]


async def mark_email_received(factory, user_id, operation_id):
    from app.ai_service.tool_operations import authorize_turn, owned_operation_turn

    async with factory() as db:
        async with db.begin():
            turn, name = await owned_operation_turn(db, user_id, operation_id)
            if name != EMAIL_TOOL:
                raise BusinessError(ErrorCode.SESSION_NOT_FOUND, http_status=404)
            await authorize_turn(db, user_id, turn)
            status = await db.scalar(sa.select(current_status()).where(dispatches.c.operation_id == operation_id,
                                                                      dispatches.c.user_id == user_id).with_for_update())
            if status not in ('accepted', 'unknown', 'received'):
                raise BusinessError(ErrorCode.INVALID_PARAMETER, message='当前邮件状态无法记录收件', http_status=409)
            if status != 'received':
                await db.execute(dispatches.update().where(dispatches.c.operation_id == operation_id).values(
                    status='received', updated_at=sa.func.current_timestamp(),
                ))
    return {'operation_id': operation_id, 'status': 'received'}


async def maintain_email_dispatches(factory):
    async with factory() as db:
        async with db.begin():
            now = await db.scalar(sa.select(sa.func.current_timestamp()))
            await db.execute(dispatches.update().where(dispatches.c.status == 'dispatching',
                dispatches.c.deadline_at <= now).values(status='unknown', updated_at=now))
            await db.execute(dispatches.delete().where(dispatches.c.started_at <= now - RATE_WINDOW,
                ~sa.exists(sa.select(operations.c.operation_id).where(operations.c.operation_id == dispatches.c.operation_id))))
