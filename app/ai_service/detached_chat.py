"""Own a bounded chat run independently of its SSE subscriber."""

import asyncio
from collections import deque
from contextlib import aclosing
from contextvars import ContextVar

from anyio import move_on_after
from sqlalchemy.exc import SQLAlchemyError

from app.ai_service.tool_operations import ToolTurn, authorize_turn
from app.ai_service.workflows import ChatWorkflow, read_workflow
from app.core.failed_response import BusinessError, ErrorCode
from app.core.logger_handler import logger
from app.core.task_runner import spawn_background_task
from app.model_config.runtime import dispatch_fence


RUN_TIMEOUT_SECONDS = 180
CANCEL_POLL_SECONDS = 1
MAX_RELAY_FRAMES = 64
MAX_RELAY_BYTES = 256 * 1024
READ_ONLY_TOOLS = frozenset({
    'what_time_is_now', 'get_user_info_tools', 'search_notes_tool', 'get_note_content_tool',
    'get_note_stats_tool', 'get_today_reviews_tool', 'get_related_notes_tool',
})
read_only_chat = ContextVar('detached_read_only_chat', default=False)
background_actions = ContextVar('background_tool_actions', default=False)


def restrict_background_tools(names):
    if not read_only_chat.get():
        return list(names)
    from app.ai_service.tool_approvals import APPROVAL_TOOLS
    from app.ai_service.tool_results import ARTIFACT_TOOLS

    allowed = READ_ONLY_TOOLS | APPROVAL_TOOLS | ARTIFACT_TOOLS.keys() if background_actions.get() else READ_ONLY_TOOLS
    return [name for name in names if name in allowed]


class DetachedChat:
    def __init__(self, workflow, user_id):
        self.workflow = workflow
        self.user_id = user_id
        self.task = None
        self.started = False
        self.stop_reason = None
        self.attached = True
        self.done = False
        self.buffer = deque()
        self.size = 0
        self.wake = asyncio.Event()

    def stop(self, reason):
        if self.stop_reason is None:
            self.stop_reason = reason
            if self.started:
                self.task.cancel()

    def offer(self, frame):
        if not self.attached:
            return
        size = len(frame.encode('utf-8'))
        if len(self.buffer) >= MAX_RELAY_FRAMES or self.size + size > MAX_RELAY_BYTES:
            self.attached = False
        else:
            self.buffer.append((frame, size))
            self.size += size
        self.wake.set()

    async def frames(self):
        try:
            while True:
                while self.buffer:
                    frame, size = self.buffer.popleft()
                    self.size -= size
                    yield frame
                if self.done or not self.attached:
                    return
                self.wake.clear()
                await self.wake.wait()
        finally:
            self.attached = False
            self.buffer.clear()
            self.size = 0


_active = {}


def has_detached_chat(user_id):
    return any(run.user_id == user_id for run in _active.values())


def find_detached_chat(workflow_id, user_id):
    run = _active.get(workflow_id)
    return run if run is not None and run.user_id == user_id else None


async def observe_execution(run):
    """Use the database as the cancellation signal, including after a missed wakeup."""
    while run.stop_reason is None:
        await asyncio.sleep(CANCEL_POLL_SECONDS)
        try:
            status = await run.workflow.execution_state()
        except SQLAlchemyError as error:
            logger.warning(f'后台对话状态轮询失败: error_type={type(error).__name__}')
            await asyncio.sleep(CANCEL_POLL_SECONDS)
            continue
        if status == 'running':
            continue
        if status in ('cancelled', 'interrupted', None):
            reason = 'WORKFLOW_LEASE_LOST' if status != 'cancelled' else 'USER_CANCELLED'
            run.stop(reason)
        return


def start_detached_chat(workflow, user_id, producer, release_slot=None, *, deadline=None, allow_actions=False):
    if workflow.id in _active or has_detached_chat(user_id):
        return None
    run = DetachedChat(workflow, user_id)

    async def execute():
        run.started = True
        inherited_fence = dispatch_fence.get()

        async def fence():
            if inherited_fence is not None:
                await inherited_fence()
            await workflow.assert_active()

        fence_token = dispatch_fence.set(fence)
        tool_token = read_only_chat.set(True)
        approval_token = background_actions.set(allow_actions)
        terminal_status, reason = 'cancelled', 'DETACHED_WORKER_EXITED'
        observer = heartbeat = None
        try:
            if run.stop_reason is None:
                timeout = asyncio.timeout(RUN_TIMEOUT_SECONDS) if deadline is None else asyncio.timeout_at(deadline)
                async with timeout:
                    if await workflow.execution_state() == 'running':
                        observer = asyncio.create_task(observe_execution(run), name='detached_chat_control')
                        if workflow.attempt is not None:
                            heartbeat = asyncio.create_task(workflow.heartbeat(), name='detached_chat_heartbeat')
                        async with aclosing(producer(workflow)) as frames:
                            async for frame in frames:
                                run.offer(frame)
        except TimeoutError:
            terminal_status, reason = 'failed', 'DETACHED_DEADLINE_EXCEEDED'
        except Exception:
            terminal_status, reason = 'failed', 'DETACHED_EXECUTION_FAILED'
            raise
        finally:
            dispatch_fence.reset(fence_token)
            read_only_chat.reset(tool_token)
            background_actions.reset(approval_token)
            controls = [task for task in (observer, heartbeat) if task is not None]
            for task in controls:
                task.cancel()
            try:
                try:
                    with move_on_after(2, shield=True):
                        if controls:
                            await asyncio.gather(*controls, return_exceptions=True)
                        if workflow.attempt is not None and run.stop_reason == 'SERVICE_SHUTDOWN':
                            await workflow.relinquish()
                        else:
                            await workflow.finish(terminal_status, run.stop_reason or reason)
                finally:
                    if release_slot is not None:
                        with move_on_after(2, shield=True):
                            await release_slot()
            finally:
                _active.pop(workflow.id, None)
                run.done = True
                run.wake.set()

    run.task = spawn_background_task(execute(), name='detached_chat')
    if run.task is None:
        return None
    _active[workflow.id] = run
    return run


async def cancel_detached_chat(session_factory, workflow_id, user_id):
    async with session_factory() as db:
        async with db.begin():
            view = await read_workflow(db, user_id, workflow_id)
            await authorize_turn(db, user_id, ToolTurn(view['session_id'], view['turn_message_id']))
            if view['status'] != 'running':
                return view
            if not view['is_detached']:
                raise BusinessError(ErrorCode.INVALID_PARAMETER, message='当前对话由原连接管理停止',
                                    http_status=409, data={'reason': 'WORKFLOW_NOT_DETACHED'})
            changed = await ChatWorkflow(workflow_id, session_factory).cancel_in(db)
    if changed:
        run = find_detached_chat(workflow_id, user_id)
        if run is not None:
            run.stop('USER_CANCELLED')
    async with session_factory() as db:
        return await read_workflow(db, user_id, workflow_id)


async def stop_detached_chats():
    runs = tuple(_active.values())
    for run in runs:
        run.stop('SERVICE_SHUTDOWN')
    if runs:
        await asyncio.wait([run.task for run in runs], timeout=5)
