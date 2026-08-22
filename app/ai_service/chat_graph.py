"""
对话编排图（P2-1 第二步：LangGraph StateGraph）

节点：classify →（条件边）react | plan → END
- classify_node: 查询复杂度分类（L1 规则 + L2 LLM），结果进 state
- route_after_classify: 条件边决策（simple→react；complex+plan_model→plan_execute）
- react_node: ReAct 路径（简单 / Plan 模型不可用降级），事件推入总线
- plan_node: Plan-Execute 路径，事件推入总线（plan_fallback 的 ReAct 重跑
  由 chat_route.plan_events 内部实现，图内无需 fallback 边——SSE 契约不变）

事件传递：节点把事件推入 state.event_queue（asyncio.Queue），
stream_chat_graph 从队列产出事件流，由调用方（chat.py）格式化 SSE。
契约与改造前完全一致：
- error 事件后停止产出（不发 done、不保存回复）
- ReAct 路径不转发 tool_*（现状）；Plan 路径转发 plan_*/tool_*
"""

import asyncio
from typing import Any, AsyncGenerator, TypedDict

from langgraph.graph import END, START, StateGraph

from app.ai_service.chat_route import ChatRouteContext, decide_route, plan_events, react_events
from app.core.logger_handler import logger
from app.core.model_trace import set_trace_stage
from app.core.task_runner import spawn_background_task


class ChatState(TypedDict):
    """对话图状态"""
    ctx: ChatRouteContext      # 执行上下文（含事件总线队列）
    classification: dict       # classify_node 产出
    route: str                 # 条件边决策结果（react / plan_execute）
    event_queue: Any           # asyncio.Queue：节点 → 调用方的事件总线


async def classify_node(state: ChatState) -> dict:
    """查询复杂度分类（L1 规则 + L2 LLM 精判）"""
    ctx = state["ctx"]
    set_trace_stage("classify")  # 模型 trace 阶段标记（P0）
    if ctx.user_message.strip():
        from app.ai_service.query_classifier import QueryClassifier

        classifier = QueryClassifier(llm_model=ctx.classifier_model)
        classification = await classifier.classify(ctx.user_message)
        result = {
            "complexity": classification.complexity,
            "source": classification.source,
            "reason": classification.reason,
        }
    else:
        # 仅附件消息：无文本可分类，直接走 ReAct 多模态路径
        # （等价于原 chat.py 的 _AttachmentOnlyClassification）
        result = {
            "complexity": "simple",
            "source": "attachment",
            "reason": "仅附件消息，无文本可分类",
        }
        logger.debug("仅附件消息，跳过复杂度分类，直接走 ReAct 多模态路径")
    logger.debug(
        f"查询分类: complexity={result['complexity']}, "
        f"source={result['source']}, reason={result['reason']}"
    )
    return {"classification": result}


def route_after_classify(state: ChatState) -> str:
    """条件边：按分类结果与 Plan 模型可用性选择执行路径（复用纯函数 decide_route）"""
    classification = state["classification"]
    plan_available = state["ctx"].plan_model is not None
    route = decide_route(classification.get("complexity"), plan_available)
    logger.debug(
        f"查询路由: complexity={classification.get('complexity')}, route={route}, "
        f"reason={classification.get('reason')}"
    )
    return route


async def react_node(state: ChatState) -> dict:
    """ReAct 执行（事件推入总线；error 后 react_events 自行终止）"""
    ctx = state["ctx"]
    set_trace_stage("agent")  # 模型 trace 阶段标记（P0）
    queue = state["event_queue"]
    async for event in react_events(ctx, timeout=ctx.react_timeout):
        await queue.put(event)
    return {}


async def plan_node(state: ChatState) -> dict:
    """Plan-Execute 执行（事件推入总线；含 plan_fallback → ReAct 重跑）"""
    ctx = state["ctx"]
    set_trace_stage("plan_execute")  # 模型 trace 阶段标记（P0）
    queue = state["event_queue"]
    async for event in plan_events(ctx, timeout=ctx.plan_timeout):
        await queue.put(event)
    return {}


# ========== 图构建（模块加载时编译一次） ==========

_compiled_graph = None


def build_chat_graph():
    """构建并编译对话编排图（幂等）"""
    global _compiled_graph
    if _compiled_graph is not None:
        return _compiled_graph

    graph = StateGraph(ChatState)
    graph.add_node("classify", classify_node)
    graph.add_node("react", react_node)
    graph.add_node("plan", plan_node)
    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify",
        route_after_classify,
        {"react": "react", "plan_execute": "plan"},
    )
    graph.add_edge("react", END)
    graph.add_edge("plan", END)
    _compiled_graph = graph.compile()
    logger.info("对话编排图构建完成（classify → react|plan → END）")
    return _compiled_graph


async def stream_chat_graph(ctx: ChatRouteContext) -> AsyncGenerator[dict, None]:
    """
    执行对话编排图并产出事件流

    事件契约与改造前一致（error 后停止；其余事件原样产出）；
    图内异常统一转为 error 事件，不影响调用方。

    Args:
        ctx: 路由上下文（含 classifier_model / react_timeout / plan_timeout）

    Yields:
        事件字典（response / error / plan_* / tool_*）
    """
    graph = build_chat_graph()
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    state: ChatState = {
        "ctx": ctx,
        "classification": {},
        "route": "",
        "event_queue": queue,
    }

    async def _run():
        try:
            await graph.ainvoke(state)
        except Exception as e:
            logger.error(f"对话图执行失败: {type(e).__name__}: {e}", exc_info=True)
            try:
                # 对外统一文案（审查 M15：内部异常细节仅入日志，不直出客户端）
                await queue.put({"type": "error", "content": "生成失败，请稍后重试"})
            except Exception:
                pass
        finally:
            # 结束哨兵：图完成后解除 drain 阻塞
            try:
                await queue.put(None)
            except Exception:
                pass

    task = spawn_background_task(_run(), name="chat_graph_run")
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield event
            if event.get("type") == "error":
                break  # 与现状一致：error 后停止产出
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            # 任务被取消属预期（error 后停止）；CancelledError 是 BaseException，须显式捕获
            pass
