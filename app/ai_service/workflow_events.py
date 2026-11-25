"""Replay selected progress and the canonical committed reply."""

from dataclasses import dataclass
import json
import re

import sqlalchemy as sa

from app.ai_service.workflows import ChatWorkflow, database_now, lease_lost, read_workflow
from app.ai_service.tool_results import artifact_file_event
from app.model_config.task_budget import ModelBudgetDenied, TOOL_CALL_LIMIT_EXCEEDED
from app.models.chat import ChatMessage
from app.models.workflow_event import workflow_events
from app.models.workflow_run import workflow_runs


MAX_PROGRESS_EVENTS = 256
RAG_NOTICES = {
    'empty': '未找到可引用的知识库文档或笔记。',
    'partial': '部分检索来源暂不可用，本轮仅使用已核验的检索结果。',
    'degraded': '检索优化步骤未完成，已采用可用的检索证据。',
    'error': '知识检索暂不可用，本轮将使用其他可用上下文。',
}
INTEGER_FIELDS = {
    'plan_start': ('total_steps',),
    'plan_step': ('step',),
    'plan_step_start': ('step',),
    'plan_step_end': ('step',),
    'plan_complete': ('total_steps', 'completed_steps'),
    'reflection': ('round',),
    'tool_end': ('duration_ms',),
}
ENUM_FIELDS = {
    'plan_step': ('status', frozenset({'pending', 'running', 'completed', 'failed', 'skipped'})),
    'reflection': ('stage', frozenset({'checking', 'refining', 'repairing', ''})),
}
REPLAYABLE = frozenset(INTEGER_FIELDS) | {'plan_synthesize', 'plan_fallback', 'tool_start'}
TERMINAL_MESSAGES = {
    'failed': '本轮生成失败，已恢复可用的进度。',
    'cancelled': '原对话已停止，已恢复可用的进度。',
    'interrupted': '原对话已中断，已恢复可用的进度。',
}


def project_progress(event):
    kind = event.get('type')
    if not isinstance(kind, str):
        return None
    if kind == 'thinking' and event.get('stage') == 'processing':
        return {'type': kind, 'stage': 'processing', 'content': '正在思考...'}
    if kind == 'thinking' and event.get('stage') == 'rag':
        status = event.get('status')
        if isinstance(status, str) and status in RAG_NOTICES:
            return {'type': kind, 'stage': 'rag', 'status': status, 'content': RAG_NOTICES[status]}
    if kind == 'tool_file':
        return artifact_file_event(event.get('name'), event, event.get('operation_id'))
    if kind not in REPLAYABLE:
        return None
    payload = {'type': kind}
    for key in INTEGER_FIELDS.get(kind, ()):
        value = event.get(key)
        if type(value) is int and 0 <= value <= 2**31 - 1:
            payload[key] = value
    if kind in ENUM_FIELDS:
        key, options = ENUM_FIELDS[kind]
        value = event.get(key)
        if isinstance(value, str) and value in options:
            payload[key] = value
    if kind in ('tool_start', 'tool_end'):
        name = event.get('name')
        if isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', name):
            payload['name'] = name
        if kind == 'tool_end' and type(event.get('error')) is bool:
            payload['error'] = event['error']
    return payload


def event_frame(event):
    sequence = event.get('sequence')
    prefix = f'id: {sequence}\n' if sequence is not None else ''
    return f'{prefix}data: {json.dumps(event, ensure_ascii=False)}\n\n'


@dataclass
class WorkflowJournal:
    workflow: ChatWorkflow
    sequence: int = 0

    @classmethod
    async def resume(cls, workflow):
        table = workflow_events
        async with workflow.session_factory() as db:
            sequence = await db.scalar(sa.select(sa.func.max(table.c.sequence)).where(table.c.workflow_id == workflow.id))
        return cls(workflow, sequence or 0)

    async def live_frame(self, event):
        data = {**event, 'workflow_id': self.workflow.id}
        data.pop('sequence', None)
        payload = project_progress(event)
        if payload is not None and self.sequence < MAX_PROGRESS_EVENTS:
            sequence = self.sequence + 1
            async with self.workflow.session_factory() as db:
                async with db.begin():
                    now = await database_now(db)
                    active = await db.scalar(sa.select(workflow_runs.c.id).where(
                        *self.workflow._active(now)).with_for_update())
                    if active is None:
                        raise lease_lost()
                    await db.execute(workflow_events.insert().values(
                        workflow_id=self.workflow.id, sequence=sequence, payload=payload,
                    ))
            self.sequence = sequence
            data['sequence'] = sequence
        if event.get('type') == 'done':
            # This event represents the already committed terminal checkpoint.
            data['sequence'] = self.sequence + 1
        return event_frame(data)


async def read_workflow_events(db, user_id, workflow_id, after, limit):
    workflow = await read_workflow(db, user_id, workflow_id)
    table = workflow_events
    rows = (await db.execute(sa.select(table.c.sequence, table.c.payload).where(
        table.c.workflow_id == workflow_id, table.c.sequence > after,
    ).order_by(table.c.sequence).limit(limit + 1))).all()
    events = [{**row.payload, 'sequence': row.sequence, 'workflow_id': workflow_id} for row in rows[:limit]]
    cursor = events[-1]['sequence'] if events else after
    more = len(rows) > limit
    status = workflow['status']
    if status != 'running' and not more:
        # Terminal identity stays stable even if an individual reply is later removed.
        last = await db.scalar(sa.select(sa.func.max(table.c.sequence)).where(table.c.workflow_id == workflow_id))
        terminal = (last or 0) + 1
        if cursor < terminal and len(events) == limit:
            more = True
        elif cursor < terminal:
            final = {'sequence': terminal, 'workflow_id': workflow_id, 'session_id': workflow['session_id']}
            if status == 'succeeded':
                final['type'] = 'done'
                if workflow['reply_message_id'] is not None:
                    reply = (await db.execute(sa.select(ChatMessage.content, ChatMessage.citations_json).where(
                        ChatMessage.id == workflow['reply_message_id'], ChatMessage.session_id == workflow['session_id'],
                        ChatMessage.role == 'assistant',
                    ))).one_or_none()
                    final['replay_content'] = reply.content if reply else None
                    if reply and reply.citations_json:
                        final.update(reply_message_id=workflow['reply_message_id'], citation_ids=list(reply.citations_json))
            elif workflow['error_code'] == TOOL_CALL_LIMIT_EXCEEDED:
                final.update(ModelBudgetDenied(TOOL_CALL_LIMIT_EXCEEDED).stream_event())
            else:
                final.update(type='error', content=TERMINAL_MESSAGES[status], reason=workflow['error_code'])
            events.append(final)
            cursor = terminal
    return {'events': events, 'cursor': cursor, 'has_more': more, 'status': status}
