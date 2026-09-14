"""
Agent 中间件模块

基于 LangGraph 的 Agent 生命周期钩子：
- before_agent / after_agent
- before_tool / after_tool
- 用于日志记录、性能监控、工具调用审计（P2-3）
"""

import asyncio
from collections import deque
from contextvars import ContextVar
from typing import Any, Callable, Dict, Optional

from app.core.logger_handler import logger
from app.core.model_trace import get_trace_context
from app.core.task_runner import spawn_background_task



class AgentMiddleware:
    """
    Agent 中间件

    提供 Agent 执行生命周期的钩子，用于：
    - 日志记录（模型调用、工具调用）
    - 性能监控（耗时统计）
    - 调试辅助
    """

    def __init__(self):
        self._hooks: Dict[str, list] = {
            "before_agent": [],
            "after_agent": [],
            "before_tool": [],
            "after_tool": [],
        }

    def register_hook(self, event: str, callback: Callable) -> None:
        """
        注册生命周期钩子

        Args:
            event: 事件名称（before_agent/after_agent/before_tool/after_tool）
            callback: 回调函数（支持同步和异步）
        """
        if event in self._hooks:
            self._hooks[event].append(callback)

    async def trigger(self, event: str, context: Dict[str, Any]) -> None:
        """
        触发指定事件的所有钩子

        Args:
            event: 事件名称
            context: 上下文信息
        """
        for callback in self._hooks.get(event, []):
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(context)
                else:
                    callback(context)
            except Exception as e:
                logger.error(f"Agent 中间件钩子执行失败 [{event}]: {e}")


# 默认中间件实例（带日志记录）
default_middleware = AgentMiddleware()


async def _log_before_agent(context: dict) -> None:
    """Agent 执行前日志"""
    logger.info(f"Agent 开始执行: input={context.get('input', '')[:100]}...")


async def _log_after_agent(context: dict) -> None:
    """Agent 执行后日志"""
    output = context.get("output", "")
    logger.info(f"Agent 执行完成: output_length={len(output)}")


async def _log_before_tool(context: dict) -> None:
    """工具调用前日志"""
    logger.debug(f"工具调用开始: {context.get('tool_name', 'unknown')}")


async def _log_after_tool(context: dict) -> None:
    """工具调用后日志"""
    logger.debug(
        f"工具调用完成: {context.get('tool_name', 'unknown')}, "
        f"duration_ms={context.get('duration_ms', 0)}"
    )


# 注册默认日志钩子
default_middleware.register_hook("before_agent", _log_before_agent)
default_middleware.register_hook("after_agent", _log_after_agent)
default_middleware.register_hook("before_tool", _log_before_tool)
default_middleware.register_hook("after_tool", _log_after_tool)


# ============================================================
# 工具调用审计（P2-3）
# ============================================================

# 每个请求的工具调用暂存队列（before_tool 入队、after_tool 出队配对成一行）。
# 用 contextvar 隔离并发请求；default=None 避免并发请求共享同一 deque。
_tool_pending: ContextVar[Optional[deque]] = ContextVar("tool_audit_pending", default=None)


async def _audit_before_tool(context: dict) -> None:
    """工具调用开始：暂存入参（与 after_tool 配对后写审计行）"""
    pending = _tool_pending.get()
    if pending is None:
        pending = deque()
        _tool_pending.set(pending)
    pending.append({
        "tool_name": context.get("tool_name", "unknown"),
        "params_json": (context.get("input") or "")[:500],
    })


async def _audit_after_tool(context: dict) -> None:
    """工具调用结束：出队配对 → 异步写审计表（失败仅告警，不阻塞链路）"""
    pending = _tool_pending.get()
    start = pending.popleft() if pending else {}

    trace_ctx = get_trace_context() or {}
    row = {
        "user_id": trace_ctx.get("user_id") or "",
        "session_id": trace_ctx.get("session_id"),
        "request_id": trace_ctx.get("request_id"),
        "tool_name": start.get("tool_name") or context.get("tool_name", "unknown"),
        "params_json": start.get("params_json") or "",
        "result_preview": (context.get("output") or "")[:500],
        "latency_ms": context.get("duration_ms"),
        "success": context.get("success", True),
    }
    try:
        spawn_background_task(_write_audit(row), name="tool_audit_write")
    except Exception as e:
        logger.warning(f"工具审计任务创建失败: {e}")


async def _write_audit(row: dict) -> None:
    """写工具调用审计表（延迟导入避免循环依赖）"""
    from app.db.database import async_session_factory
    from app.models.tool_audit import ToolCallAudit

    try:
        async with async_session_factory() as db:
            db.add(ToolCallAudit(**row))
            await db.commit()
    except Exception as e:
        logger.warning(f"工具审计落库失败: {e}")


# 注册审计钩子（与日志钩子并存）
default_middleware.register_hook("before_tool", _audit_before_tool)
default_middleware.register_hook("after_tool", _audit_after_tool)
