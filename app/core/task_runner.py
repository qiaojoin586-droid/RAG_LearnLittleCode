"""
后台任务治理（审查 M9：fire-and-forget 任务无边界、无失败监控）

问题：各处直接 `asyncio.create_task(...)` 不持有引用、无失败回调，
高并发下任务无限堆积；任务异常只有 "Task exception was never retrieved"
噪音，无法定位是哪个任务挂了。

统一入口 `spawn_background_task(coro, name)`：
- 持有任务引用（防 GC），活跃任务计数
- 并发上限（默认 50），超限拒绝并告警，防堆积
- done 回调：取消记 debug；异常统一记日志（含任务名与 traceback），
  杜绝 "never retrieved" 噪音
"""

import asyncio
import inspect
from typing import Awaitable, Optional

from app.core.logger_handler import logger

# 活跃后台任务上限（防高并发下任务无限堆积；超限拒绝并告警）
MAX_BACKGROUND_TASKS = 50

# 活跃任务集合（持引用防 GC；同时提供上限与监控）
_active_tasks: set = set()


def spawn_background_task(coro: Awaitable, name: str = "background") -> Optional[asyncio.Task]:
    """
    启动受控后台任务（替换裸 asyncio.create_task）

    Args:
        coro: 协程对象
        name: 任务名（日志/监控用，便于定位）

    Returns:
        Task 对象；超过并发上限时返回 None 并告警（调用方自行决定降级）
    """
    if len(_active_tasks) >= MAX_BACKGROUND_TASKS:
        logger.error(
            f"后台任务并发超限（{MAX_BACKGROUND_TASKS}），拒绝启动: {name}"
        )
        # Admission failed before asyncio owns the coroutine. Close it explicitly.
        if inspect.iscoroutine(coro):
            coro.close()
        return None

    try:
        task = asyncio.create_task(coro, name=name)
    except BaseException:
        if inspect.iscoroutine(coro):
            coro.close()
        raise
    _active_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _active_tasks.discard(t)
        if t.cancelled():
            logger.debug(f"后台任务被取消: {name}")
            return
        # 读取异常（不读取会触发 "Task exception was never retrieved" 噪音）
        exc = t.exception()
        if exc is not None:
            logger.error(
                f"后台任务异常: {name} - {type(exc).__name__}: {exc}",
                exc_info=exc,
            )

    task.add_done_callback(_on_done)
    logger.debug(f"后台任务启动: {name}（活跃 {len(_active_tasks)}/{MAX_BACKGROUND_TASKS}）")
    return task


def active_task_count() -> int:
    """当前活跃后台任务数（监控/测试用）"""
    return len(_active_tasks)
