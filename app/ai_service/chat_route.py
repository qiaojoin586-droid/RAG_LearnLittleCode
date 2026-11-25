"""
对话路由层（P2-1 编排层改造 · 第一步）

把 chat.py generate_stream 的路由决策与事件转发抽取为独立结构：
- ChatRouteContext: 执行上下文（显式化原闭包状态）
- decide_route: 纯函数路由决策（simple/complex → react / plan_execute）
- react_events / plan_events: 各路径事件流（async generator，产出事件字典）

SSE 格式化仍由路由层（chat.py）完成，事件类型与顺序契约零变化：
- ReAct 路径：仅转发 response / error（tool_* 不转发，与改造前一致）
- Plan 路径：转发 plan_* / tool_* / response / error；plan_fallback 后接 ReAct 重跑
- error 事件后终止（上游据此不发 done、不保存回复）

第二步将把本层迁移为 LangGraph StateGraph（事件总线 + 条件边），
本模块的决策与事件流语义将原样保留。
"""

import asyncio
from contextlib import aclosing
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.ai_service.agent_runner import execute_agent
from app.ai_service.plan_execute_agent import execute_plan_agent
from app.ai_service.plan_checkpoints import PlanCheckpoint
from app.ai_service.reflection import build_repair_note, critique_answer
from app.ai_service.tool_operations import ToolTurn
from app.ai_service.workflows import ChatWorkflow
from app.core.logger_handler import logger
from app.core.failed_response import ErrorCode
from app.utils.config import get_reflection_config
from app.utils.reflection_metrics import record_reflection_metric


@dataclass
class ChatRouteContext:
    """一次对话请求的执行上下文（原 generate_stream 闭包状态的显式化）"""
    agent_model: Any                      # 执行链路模型（按模态/思考开关路由后的实例）
    user_id: str
    user_message: str
    system_prompt: str                    # 含 RAG 上下文与引用笔记
    compressed_messages: list             # Token 预算压缩后的历史消息
    db_session_factory: Any               # 数据库会话工厂（供工具使用）
    plan_model: Any = None                # Plan 模型（route=plan_execute 时使用）
    classifier_model: Any = None          # 分类器 L2 模型（classify 节点使用）
    note_service: Any = None
    review_service: Any = None
    email_service: Any = None
    ppt_service: Any = None               # PPT 生成服务（§6.4 注入链路）
    attachment_content: list = field(default_factory=list)   # 多模态 content blocks
    attachment_names: Optional[list] = None                  # 附件文件名（Plan 摘要注入）
    react_timeout: int = 60               # ReAct 路径超时（上游传 LLM_STREAM_TIMEOUT）
    plan_timeout: int = 120               # Plan 路径超时（上游传 LLM_STREAM_TIMEOUT * 2）
    tool_turn: ToolTurn | None = None
    workflow: ChatWorkflow | None = None
    plan_checkpoint: PlanCheckpoint | None = None
    recovered_operations: dict = field(default_factory=dict)


def decide_route(complexity: str, plan_model_available: bool) -> str:
    """
    路由决策（纯函数）

    Args:
        complexity: 分类结果（simple / complex）
        plan_model_available: Plan 模型是否可用

    Returns:
        "react"（ReAct）或 "plan_execute"（Plan-and-Execute）
    """
    if complexity == "complex" and plan_model_available:
        return "plan_execute"
    return "react"


async def react_events(ctx: ChatRouteContext, timeout: int) -> AsyncGenerator[dict, None]:
    """
    ReAct 路径事件流（简单问题 / Plan 降级共用）

    含两层 Reflection：
    - L2 失败修复轮（Part B §9.3 决策 4）：整体执行失败（error 事件）时拦截
      错误、回灌失败原因重跑 1 次；no_retry_tools（send_email）失败不自动重试。
      error 事件仅在最终失败时透传（避免前端 onError 提前关流）。
    - L1 结果自检（P2 增强 3，Part B 决策 5 原案：分段流式）：stream_done 后
      轻量模型评判已流出的回答，不合格则纯文本流式输出修正版（草稿已流出，
      修正版以第二段 response 续接）。

    Yields:
        response / reflection(checking|refining|repairing) / error 事件
        （error 后终止；stream_done 不转发，与改造前一致）
    """
    reflection_cfg = get_reflection_config()
    l2_config = reflection_cfg.get("l2_repair", {})
    l2_enable = l2_config.get("enable", False)
    no_retry_tools = set(l2_config.get("no_retry_tools", ["send_email"]))
    max_repairs = min(int(l2_config.get("max_repair_attempts", 1)), 1)  # 硬顶 1（决策 3 同款兜底）

    # ReAct 路径 L1 配置（复用 trigger.min_answer_chars / critic_timeout）
    react_l1_enable = reflection_cfg.get("react_l1_enable", False)
    min_answer_chars = reflection_cfg.get("trigger", {}).get("min_answer_chars", 500)
    critic_timeout = reflection_cfg.get("critic_timeout", 15)

    repair_note = ""
    full_response = ""
    for attempt in range(max_repairs + 1):
        failed_tool = None
        error_event = None
        async with aclosing(execute_agent(
            chat_model=ctx.agent_model,
            user_id=ctx.user_id,
            user_message=ctx.user_message + repair_note,
            system_prompt=ctx.system_prompt,
            compressed_messages=ctx.compressed_messages,
            db_session_factory=ctx.db_session_factory,
            note_service=ctx.note_service,
            review_service=ctx.review_service,
            email_service=ctx.email_service,
            ppt_service=ctx.ppt_service,
            timeout=timeout,
            attachment_content=ctx.attachment_content,
        )) as _owned_stream_3:
            async for event in _owned_stream_3:
                event_type = event.get("type", "")
                # 跟踪流内最后失败的工具（供修复轮定向 + no_retry_tools 判定）
                if event_type == "tool_end" and event.get("error"):
                    failed_tool = event.get("name")
                if event_type == "response":
                    full_response += event.get("content", "")
                    yield event
                elif event_type == "error":
                    error_event = event  # 拦截：先决定是否重试，不立即透传
                    break
                # ★ 简单路径补转发工具事件（§6.3 第 2 段，行为变更提示见设计方案）：
                # 此前连 tool_start/tool_end 都不转发，补上后简单对话中的工具状态
                # （含 generate_ppt_tool 的生成进度）对前端可见
                elif event_type in ("tool_start", "tool_end", "tool_file"):
                    yield event
                # ★ 思考过程实时转发（审查 B+C）：深度思考模式下 reasoning_content
                # 作为 thinking 事件推送，前端显示思考进度（消除零反馈等待焦虑）
                elif event_type == "thinking":
                    yield event
                elif event_type == "stream_done":
                    full_response = event.get("full_response", full_response)
            # 其余事件不转发（与现状一致）

        if error_event is None:
            # 本轮成功（含修复轮成功）→ 记录 L2 结果并进入 L1
            if repair_note:
                record_reflection_metric({
                    "task": "react", "l2_repair_triggered": True,
                    "l2_repair_success": True,
                })
            break

        # 失败 → L2 判定：关闭 / no_retry_tools / 轮次耗尽 → 透传 error 终止
        if error_event.get("retryable") is False or error_event.get("code") == ErrorCode.MODEL_CONTEXT_EXCEEDED:
            yield error_event
            return
        if not l2_enable or failed_tool in no_retry_tools or attempt >= max_repairs:
            if failed_tool in no_retry_tools:
                logger.warning(f"L2 修复跳过: 工具 {failed_tool} 在 no_retry_tools（避免副作用），error 直接透传")
            if repair_note:
                record_reflection_metric({
                    "task": "react", "l2_repair_triggered": True,
                    "l2_repair_success": False, "failed_tool": failed_tool,
                })
            yield error_event
            return

        # 修复轮：转 repairing 状态事件（前端计划卡片显示），回灌失败原因重跑
        logger.info(f"L2 修复: ReAct 执行失败（tool={failed_tool}），重试 {attempt + 1}/{max_repairs}")
        yield {"type": "reflection", "stage": "repairing", "round": attempt + 1}
        repair_note = build_repair_note(failed_tool, error_event.get("content", ""))
        full_response = ""  # 丢弃失败尝试的部分输出，修复轮重新累计

    # ===== ReAct 路径 L1 结果自检（P2 增强 3：分段流式）=====
    from app.model_config.runtime import model_for
    critic_model=model_for('reflection',ctx.plan_model)
    if not react_l1_enable or critic_model is None:
        return
    if len(full_response) < min_answer_chars:
        return  # 短回答不检（零额外调用）

    yield {"type": "reflection", "stage": "checking", "round": 0}
    t0 = time.monotonic()
    verdict = await critique_answer(
        critic_model, ctx.user_message,
        "ReAct 直接回答（无执行计划）",
        "（ReAct 模式无分步执行结果，请结合用户问题与一般事实校验回答的完整性与诚实性）",
        full_response, timeout=critic_timeout,
    )
    critique_latency_ms = int((time.monotonic() - t0) * 1000)

    refine_rounds = 0
    if not verdict.passed:
        # 修正轮：纯文本流式续接（禁工具——Part B 决策 3）
        refine_rounds = 1
        yield {"type": "reflection", "stage": "refining", "round": 1}
        refine_prompt = (
            f"你之前对用户问题「{ctx.user_message}」的回答未通过质量评审。\n\n"
            f"上一版回答：\n{full_response[:4000]}\n\n"
            f"评审意见（必须修正的问题）：\n{verdict.issues}\n\n"
            "请输出修正后的完整回答（只输出回答正文，不要解释修改过程）。"
        )
        messages = [
            SystemMessage(content=ctx.system_prompt),
            HumanMessage(content=refine_prompt),
        ]
        try:
            async with asyncio.timeout(timeout):
                async with aclosing(ctx.agent_model.astream(messages)) as _owned_stream_2:
                    async for chunk in _owned_stream_2:
                        if hasattr(chunk, "content") and chunk.content:
                            yield {"type": "response", "content": chunk.content}
        except asyncio.TimeoutError:
            logger.warning("ReAct L1 修正超时（草稿已流出，不兜底）")
        except Exception as e:
            logger.error(f"ReAct L1 修正失败（草稿已流出，不兜底）: {type(e).__name__}: {e}")

    record_reflection_metric({
        "task": "react", "triggered": True,
        "draft_chars": len(full_response),
        "refine_rounds": refine_rounds,
        "final_passed": verdict.passed,
        "critique_latency_ms": [critique_latency_ms],
    })


async def plan_events(
    ctx: ChatRouteContext,
    timeout: int,
    fallback_timeout: int = 60,
) -> AsyncGenerator[dict, None]:
    """
    Plan-Execute 路径事件流（含 plan_fallback → ReAct 重跑）

    Args:
        ctx: 路由上下文
        timeout: Plan 执行总超时（上游传 LLM_STREAM_TIMEOUT * 2）
        fallback_timeout: plan_fallback 后 ReAct 重跑的超时

    Yields:
        plan_start / plan_step / plan_step_start / plan_step_end /
        plan_synthesize / plan_complete / reflection / tool_start / tool_end /
        tool_file / response / error
        （reflection 为 L1 综合后自检状态事件；plan_fallback 事件后接 ReAct
        重跑事件；error 后终止）
    """
    forwarded = {
        "plan_start", "plan_step", "plan_step_start", "plan_step_end",
        "plan_synthesize", "plan_complete", "tool_start", "tool_end",
        "tool_file",   # ★ 新增：PPT 生成事件（§6.3 第 2 段）
        "reflection",  # ★ 新增：L1 综合后自检状态事件（AI对话Reflection反思机制设计方案.md）
    }
    async with aclosing(execute_plan_agent(
        chat_model=ctx.agent_model,
        plan_model=ctx.plan_model,
        user_id=ctx.user_id,
        user_message=ctx.user_message,
        system_prompt=ctx.system_prompt,
        compressed_messages=ctx.compressed_messages,
        db_session_factory=ctx.db_session_factory,
        note_service=ctx.note_service,
        review_service=ctx.review_service,
        email_service=ctx.email_service,
        ppt_service=ctx.ppt_service,
        timeout=timeout,
        attachment_content=ctx.attachment_content,
        attachment_names=ctx.attachment_names,
        checkpoint=ctx.plan_checkpoint,
        recovered_operations=getattr(ctx, 'recovered_operations', None),
    )) as _owned_stream_1:
        async for event in _owned_stream_1:
            event_type = event.get("type", "")
            if event_type == "plan_fallback":
                # Plan 失败，降级为 ReAct 重跑
                logger.info(f"Plan 降级: {event.get('reason', '')}")
                yield event
                async with aclosing(react_events(ctx, timeout=fallback_timeout)) as _owned_stream_0:
                    async for fallback_event in _owned_stream_0:
                        yield fallback_event
                return
            if event_type in ("response", "error") or event_type in forwarded:
                yield event
                if event_type == "error":
                    return
        # stream_done 不转发（与现状一致）


async def collect_events(events: AsyncGenerator[dict, None]) -> tuple:
    """
    事件流收集（供测试/诊断使用）

    Returns:
        (事件列表, 累积文本)
    """
    collected = []
    accumulated = ""
    async for event in events:
        collected.append(event)
        if event.get("type") == "response":
            accumulated += event.get("content", "")
    return collected, accumulated
