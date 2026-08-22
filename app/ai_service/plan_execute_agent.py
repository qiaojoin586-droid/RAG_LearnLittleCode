"""
Plan-and-Execute Agent 执行引擎

阶段状态机：
- Phase 1: Plan（轻量模型生成执行计划）
- Phase 2: Execute（逐步执行，每步用 ReAct Agent）
- Phase 3: Synthesize（汇总结果，生成最终回答）
- Phase 4: Reflect（可选，综合后自检 L1：批判 → 修正，设计文档
  AI对话Reflection反思机制设计方案.md；开关 reflection.enable）

通过 SSE 事件流推送计划进度和中间结果给前端。
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncGenerator, Dict, Any, List, Optional

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from app.ai_service.tool_registry import tool_registry
from app.core.logger_handler import logger
from app.utils.config import (
    get_plan_execute_config,
    get_reflection_config,
    get_tool_groups_config,
)


# ============================================================
# 数据结构
# ============================================================

@dataclass
class PlanStep:
    """执行计划步骤"""
    step: int
    action: str
    tool: str  # 工具名或 "none"
    depends_on: List[int] = field(default_factory=list)
    result: str = ""  # 执行后填充


@dataclass
class ExecutionPlan:
    """执行计划"""
    goal: str
    steps: List[PlanStep]


# ============================================================
# Phase 1: Plan（规划阶段）
# ============================================================

# Plan 规划器的工具短描述（供规划 LLM 决策步骤用；执行阶段的 Agent
# 绑定真实工具 schema。描述集中维护，工具名来自配置自动同步，见 _build_plan_tool_list）
_PLAN_TOOL_DESC = {
    "what_time_is_now": "获取当前时间",
    "get_user_info_tools": "获取当前用户基本信息（用户名、邮箱、ID）",
    "search_notes_tool": "语义搜索用户笔记（返回 200 字符摘要）",
    "get_note_content_tool": "获取单篇笔记的完整内容",
    "get_note_stats_tool": "笔记分类统计",
    "get_today_reviews_tool": "获取今日待回顾笔记",
    "mark_reviewed_tool": "标记回顾完成",
    "create_note_tool": "创建新笔记（支持 Markdown）",
    "update_note_tool": "更新已有笔记",
    "get_related_notes_tool": "获取关联笔记推荐",
    "send_email": "发送邮件（笔记以 Markdown/PDF 附件发送到指定邮箱）",
    "generate_ppt_tool": "根据选中的笔记生成可放映、可下载的讲解 PPT 文件（.pptx，工具返回下载链接；用户要求生成/制作 PPT、幻灯片、演示文稿时【必须】使用本工具，不要只输出文本大纲）",
}


def _build_plan_tool_list() -> str:
    """从 agent.yaml tool_groups 动态构建「可用工具」清单。

    修复（v1.6）：原 prompt 硬编码工具清单，新增工具（如 generate_ppt_tool）
    不会被规划 LLM 感知——现在工具名取自配置，新增工具自动同步；
    编号随清单动态生成，消除硬编码重复编号问题。
    """
    from app.utils.config import get_tool_groups_config

    tool_groups = get_tool_groups_config()
    names: List[str] = []
    for tools in tool_groups.values():
        for name in tools:
            if name not in names:
                names.append(name)
    # 追加 MCP 动态工具名（若已注册）：planner 感知外部能力工具（联网搜索/网页抓取），
    # 可规划 tool="tavily_search" 等步骤（实施文档 §5.2 gap #1 闭合）
    for name in tool_registry.resolve_names(["mcp"]):
        if name not in names:
            names.append(name)
    return "\n".join(
        f"{i + 1}. {name} - {_plan_tool_desc(name)}"
        for i, name in enumerate(names)
    )


def _plan_tool_desc(name: str) -> str:
    """
    工具描述：内置工具查静态表；MCP 动态工具取 tool 对象 description（截断防膨胀）
    """
    desc = _PLAN_TOOL_DESC.get(name)
    if desc:
        return desc
    dyn = tool_registry.get_dynamic(name)
    if dyn is not None:
        desc = getattr(dyn, "description", None)
        if desc:
            return str(desc)[:150]
    return "可用工具"


async def _generate_plan(
    plan_model,
    user_message: str,
    system_prompt: str,
    attachment_names: Optional[List[str]] = None,
) -> ExecutionPlan:
    """
    使用轻量模型生成执行计划

    plan_model 为文本模型（无多模态能力），附件只以摘要文本注入，
    使模型感知附件存在、能制定正确计划（设计方案 §6.4）。

    Args:
        plan_model: 轻量 Chat 模型
        user_message: 用户消息
        system_prompt: 系统提示词（含 RAG 上下文）
        attachment_names: 附件文件名列表（无多模态能力的 plan_model 感知附件）

    Returns:
        ExecutionPlan 实例

    Raises:
        ValueError: JSON 解析失败时抛出（调用方负责降级）
    """
    from app.utils.prompt_loader import load_prompt

    prompt_template = load_prompt("plan_generation")
    prompt = prompt_template.replace("{current_time}", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    prompt = prompt.replace("{user_message}", user_message[:1000])
    # 动态注入工具清单（从 agent.yaml tool_groups 构建，与执行阶段工具集同步）
    prompt = prompt.replace("{tool_list}", _build_plan_tool_list())

    # 注入附件摘要（plan_model 无法看图，但需知道附件存在以制定正确计划）
    if attachment_names:
        prompt += (
            f"\n用户本次请求附带了 {len(attachment_names)} 个附件："
            f"{', '.join(attachment_names[:5])}。"
            "请将这些附件作为任务的一部分制定计划（附件内容将由执行阶段的多模态模型处理）。"
        )

    config = get_plan_execute_config()
    plan_timeout = config.get("plan_timeout", 30)  # 与 agent.yaml 对齐（P2-1 技术债务）

    try:
        async with asyncio.timeout(plan_timeout):
            response = await plan_model.ainvoke([HumanMessage(content=prompt)])
            content = response.content.strip()
    except asyncio.TimeoutError:
        logger.warning(f"Plan 生成超时 ({plan_timeout}s)")
        raise ValueError("Plan 生成超时")

    # 解析 JSON（兼容 markdown code block）
    content = re.sub(r"```json\s*", "", content)
    content = re.sub(r"```\s*", "", content)
    content = content.strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        logger.warning(f"Plan JSON 解析失败: {e}, raw={content[:300]}")
        raise ValueError(f"Plan JSON 解析失败: {e}")

    goal = data.get("goal", user_message[:50])
    raw_steps = data.get("steps", [])

    if not raw_steps:
        raise ValueError("Plan 无步骤")

    # 限制最大步骤数
    max_steps = config.get("max_steps", 5)
    raw_steps = raw_steps[:max_steps]

    steps = []
    for s in raw_steps:
        steps.append(PlanStep(
            step=s.get("step", len(steps) + 1),
            action=s.get("action", ""),
            tool=s.get("tool", "none"),
            depends_on=s.get("depends_on", []),
        ))

    plan = ExecutionPlan(goal=goal, steps=steps)
    logger.info(f"Plan 生成完成: goal={goal}, steps={len(steps)}")
    return plan


# ============================================================
# Phase 2: Execute（执行阶段）
# ============================================================

def _topological_batches(steps: List[PlanStep]) -> List[List[PlanStep]]:
    """
    拓扑排序，将无依赖的步骤分组为批次（可并行执行）

    Returns:
        批次列表，每个批次内的步骤可并行执行
    """
    completed = set()
    remaining = list(steps)
    batches = []

    while remaining:
        # 找到所有依赖已完成的步骤
        batch = [
            s for s in remaining
            if all(d in completed for d in s.depends_on)
        ]
        if not batch:
            # 存在循环依赖，强制顺序执行剩余步骤
            logger.warning("检测到循环依赖，强制顺序执行剩余步骤")
            batch = remaining

        batches.append(batch)
        for s in batch:
            completed.add(s.step)
            remaining.remove(s)

    return batches


def _resolve_step_tool_groups(step: PlanStep) -> Optional[List[str]]:
    """
    根据步骤的 tool 字段反查所属工具组

    从 agent.yaml 的 tool_groups 配置构建 工具名 → 组名 反向映射，
    根据 step.tool 查找其所属的工具组。
    始终包含 "base" 组（基础工具）。

    Args:
        step: 执行计划步骤

    Returns:
        工具组列表，如 ["base", "note_write"]；tool 为 "none" 时返回 None
    """
    if not step.tool or step.tool == "none":
        return None

    # 注意：tool_groups 定义在 agent.yaml 顶层（get_tool_groups_config），
    # 不能使用 get_agent_config()（只返回 "agent:" 段），否则映射永远为空。
    tool_groups_config = get_tool_groups_config()

    # 构建反向映射：工具名 → 组名
    tool_to_group = {}
    for group_name, tools in tool_groups_config.items():
        for tool_name in tools:
            tool_to_group[tool_name] = group_name

    # 查找 step.tool 所属的组
    matched_groups = ["base"]  # 始终包含基础组
    group = tool_to_group.get(step.tool)
    if group is None:
        # 工具名未匹配内置组：若为已注册的 MCP 动态工具 → 返回 base + mcp；
        # 否则回退关键词路由（全量组），由 execute_agent 按 default_groups 加载，
        # 避免步骤无工具可用（实施文档 §5.2 gap #2）
        if step.tool in tool_registry.resolve_names(["mcp"]):
            logger.debug(f"步骤 {step.step} 工具 '{step.tool}' 命中 MCP 动态工具 → 工具组 ['base', 'mcp']")
            return matched_groups + ["mcp"]
        logger.warning(f"步骤 {step.step} 工具 '{step.tool}' 未匹配到任何工具组，回退关键词路由（全量组）")
        return None
    elif group != "base" and group not in matched_groups:
        matched_groups.append(group)
        logger.debug(f"步骤 {step.step} 工具路由: tool='{step.tool}' → 工具组 {matched_groups}")

    # MCP 动态工具组：步骤执行始终携带已注册的 MCP 工具（实施文档 §5.2 gap #2 闭合；
    # 未注册时 resolve_names(["mcp"]) 返回空列表，无副作用）
    if tool_registry.resolve_names(["mcp"]):
        matched_groups.append("mcp")
        logger.debug(f"步骤 {step.step} 工具路由: 追加 MCP 动态工具组 → {matched_groups}")

    return matched_groups


async def _execute_step(
    step: PlanStep,
    chat_model,
    user_id: str,
    user_message: str,
    system_prompt: str,
    compressed_messages: list,
    db_session_factory,
    note_service,
    review_service,
    email_service,
    ppt_service,
    step_timeout: int,
    previous_results: Dict[int, str],
    attachment_content: Optional[List[dict]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    执行单个计划步骤

    如果步骤需要工具调用 → 创建专用 ReAct Agent（prompt 限定在当前步骤）
    如果不需要工具 → 直接 LLM 流式生成

    Args:
        attachment_content: 附件多模态 content blocks（透传给执行 Agent / 无工具 LLM 路径）
    """
    yield {"type": "plan_step_start", "step": step.step, "action": step.action}

    # 构建步骤专用上下文
    step_context_parts = [f"当前任务步骤：{step.action}"]

    # 注入前置步骤结果
    if step.depends_on:
        for dep_id in step.depends_on:
            if dep_id in previous_results:
                step_context_parts.append(
                    f"步骤 {dep_id} 的结果：{previous_results[dep_id][:500]}"
                )

    step_context = "\n".join(step_context_parts)
    step_system_prompt = (
        system_prompt
        + f"\n\n---\n当前执行步骤上下文：\n{step_context}"
        + "\n\n步骤执行规则（必须遵守）：\n"
        + "1. 只完成当前步骤的任务，不要回答用户的完整问题（完整回答由综合阶段生成）；\n"
        + "2. 步骤结果只输出本步骤需要的数据（如搜索结果、笔记内容、统计数字），\n"
        + "   不要输出'未找到/已找到'等过程性叙述；\n"
        + "3. 搜索无结果时，步骤结果只写'未找到相关笔记'，不要从自身知识补充大段内容。"
    )

    if step.tool and step.tool != "none":
        # 需要工具调用 → 使用 ReAct Agent（含 L2 失败修复轮，Part B §9.3 决策 4）
        from app.ai_service.agent_runner import execute_agent
        from app.ai_service.reflection import build_repair_note

        # 根据 step.tool 反查工具组，跳过关键词路由
        step_tool_groups = _resolve_step_tool_groups(step)

        l2_config = get_reflection_config().get("l2_repair", {})
        l2_enable = l2_config.get("enable", False)
        no_retry_tools = set(l2_config.get("no_retry_tools", ["send_email"]))
        max_repairs = min(int(l2_config.get("max_repair_attempts", 1)), 1)  # 硬顶 1

        accumulated = ""
        repair_note = ""
        for attempt in range(max_repairs + 1):
            failed_tool = None
            error_event = None
            try:
                async for event in execute_agent(
                    chat_model=chat_model,
                    user_id=user_id,
                    # 只给步骤 Agent 精简背景（截断），避免其针对完整问题输出完整回答
                    user_message=(
                        f"用户请求（背景）：{user_message[:200]}\n\n"
                        f"你的任务（只完成这一步，不要回答完整问题）：{step.action}"
                        + repair_note
                    ),
                    system_prompt=step_system_prompt,
                    compressed_messages=compressed_messages,
                    db_session_factory=db_session_factory,
                    note_service=note_service,
                    review_service=review_service,
                    email_service=email_service,
                    ppt_service=ppt_service,
                    timeout=step_timeout,
                    override_groups=step_tool_groups,
                    attachment_content=attachment_content,
                ):
                    event_type = event.get("type", "")
                    # 跟踪流内最后失败的工具（供修复轮定向 + no_retry_tools 判定）
                    if event_type == "tool_end" and event.get("error"):
                        failed_tool = event.get("name")
                    if event_type == "response":
                        # 步骤输出是内部中间结果：只累计供 Synthesize 汇总，
                        # 不直接透传（否则每个步骤都会把完整回答流式输出给用户，
                        # 造成重复内容与 token 浪费）
                        accumulated += event.get("content", "")
                    elif event_type == "error":
                        error_event = event  # 拦截：先决定是否重试
                        break
                    elif event_type in ("tool_start", "tool_end", "tool_file", "thinking"):
                        # 仅透传进度类事件（工具状态/PPT 下载卡片对用户可见）
                        yield event
                    # stream_done 等其余事件不转发（步骤内部信息不外泄）
            except Exception as e:
                logger.error(f"步骤 {step.step} 执行失败: {e}")
                error_event = {"type": "error", "content": str(e)}

            if error_event is None:
                break  # 本轮成功

            # 失败 → L2 判定：关闭 / no_retry_tools / 轮次耗尽 → 透传 error 终止
            if not l2_enable or failed_tool in no_retry_tools or attempt >= max_repairs:
                if failed_tool in no_retry_tools:
                    logger.warning(f"L2 修复跳过: 步骤 {step.step} 工具 {failed_tool} 在 no_retry_tools（避免副作用）")
                if repair_note:
                    # 修复轮已触发但最终失败 → 指标落盘（P2 增强 1）
                    from app.utils.reflection_metrics import record_reflection_metric
                    record_reflection_metric({
                        "task": "plan_step", "l2_repair_triggered": True,
                        "l2_repair_success": False, "failed_tool": failed_tool,
                    })
                yield error_event
                step.result = f"步骤 {step.step} 执行出错"
                return

            # 修复轮：转 repairing 状态事件，回灌失败原因重试本步骤
            logger.info(f"L2 修复: 步骤 {step.step} 失败（tool={failed_tool}），重试 {attempt + 1}/{max_repairs}")
            yield {"type": "reflection", "stage": "repairing", "round": attempt + 1}
            repair_note = build_repair_note(failed_tool, error_event.get("content", ""))
            accumulated = ""  # 丢弃失败尝试的部分输出

        if repair_note:
            # 修复轮已结束且成功 → 清空前端 repairing 状态（步骤内 response 不透传，
            # 前端 onResponse 清空时机不适用，此处显式清空）+ 指标落盘（P2 增强 1）
            from app.utils.reflection_metrics import record_reflection_metric
            record_reflection_metric({
                "task": "plan_step", "l2_repair_triggered": True,
                "l2_repair_success": True,
            })
            yield {"type": "reflection", "stage": "", "round": 0}

        step.result = accumulated[:500] if accumulated else f"步骤 {step.step} 完成"
    else:
        # 不需要工具 → 直接 LLM 生成（输出仅作为内部步骤结果，不透传，
        # 最终回答由 Synthesize 阶段统一生成）
        try:
            messages = list(compressed_messages or [])
            step_prompt = (
                f"用户请求（背景）：{user_message[:200]}\n\n"
                f"你的任务（只完成这一步，不要回答完整问题）：{step.action}"
            )
            if attachment_content:
                content = [{
                    "type": "text",
                    "text": step_prompt,
                }]
                content.extend(attachment_content)
                messages.append(HumanMessage(content=content))
            else:
                messages.append(HumanMessage(content=step_prompt))

            accumulated = ""
            async with asyncio.timeout(step_timeout):
                async for chunk in chat_model.astream(messages):
                    if hasattr(chunk, "content") and chunk.content:
                        accumulated += chunk.content

            step.result = accumulated[:500] if accumulated else f"步骤 {step.step} 完成"

        except asyncio.TimeoutError:
            step.result = f"步骤 {step.step} 超时"
            yield {"type": "response", "content": f"\n[步骤 {step.step} 超时]\n"}
        except Exception as e:
            step.result = f"步骤 {step.step} 失败: {str(e)}"
            yield {"type": "response", "content": f"\n[步骤 {step.step} 异常: {str(e)}]\n"}

    yield {"type": "plan_step_end", "step": step.step, "result": step.result[:200]}


async def _execute_batch(
    batch: List[PlanStep],
    chat_model,
    user_id: str,
    user_message: str,
    system_prompt: str,
    compressed_messages: list,
    db_session_factory,
    note_service,
    review_service,
    email_service,
    ppt_service,
    config: dict,
    previous_results: Dict[int, str],
    attachment_content: Optional[List[dict]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    执行一个批次（批次内步骤无依赖，可并行）

    并行模式使用 asyncio.Queue 实时转发事件，确保前端能即时收到每个步骤的 token。
    """
    enable_parallel = config.get("enable_parallel", True)
    step_timeout = config.get("step_timeout", 90)  # 与 agent.yaml 对齐（P2-1 技术债务）

    if enable_parallel and len(batch) > 1:
        # 并行执行（asyncio.Queue 实时多路复用）
        max_parallel = config.get("max_parallel_steps", 3)
        queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = "DONE"

        async def _consume_step(step: PlanStep):
            """消费单个步骤的生成器，将事件放入队列"""
            try:
                async for event in _execute_step(
                    step, chat_model, user_id, user_message, system_prompt,
                    compressed_messages, db_session_factory, note_service,
                    review_service, email_service, ppt_service, step_timeout,
                    previous_results,
                    attachment_content=attachment_content,
                ):
                    await queue.put(event)
            except Exception as e:
                logger.error(f"并行步骤 {step.step} 异常: {e}")
                await queue.put({"type": "error", "content": f"步骤 {step.step} 异常: {e}"})
            finally:
                previous_results[step.step] = step.result
                await queue.put(_SENTINEL)

        for i in range(0, len(batch), max_parallel):
            sub_batch = batch[i:i + max_parallel]
            tasks = [asyncio.create_task(_consume_step(step)) for step in sub_batch]

            # 实时从队列读取事件，直到所有步骤完成
            completed_count = 0
            while completed_count < len(tasks):
                item = await queue.get()
                if item is _SENTINEL:
                    completed_count += 1
                else:
                    yield item

            # 确保所有任务完成
            await asyncio.gather(*tasks, return_exceptions=True)
    else:
        # 顺序执行
        for step in batch:
            async for event in _execute_step(
                step, chat_model, user_id, user_message, system_prompt,
                compressed_messages, db_session_factory, note_service,
                review_service, email_service, ppt_service, step_timeout,
                previous_results,
                attachment_content=attachment_content,
            ):
                yield event
            previous_results[step.step] = step.result


# ============================================================
# Phase 3: Synthesize（综合阶段）+ Phase 4: Reflect（反思阶段）
# ============================================================

def _dedupe_step_results(step_results: List[tuple]) -> List[tuple]:
    """
    步骤结果代码级去重（修复：Plan-Execute 重复输出问题）

    多个步骤 Agent 可能各自输出相同的大段内容（如都复述了完整回答），
    去重后再交给 Synthesize，避免最终回答重复。策略：
    1. 归一化（去空白/换行）后完全相同的只保留首次出现的；
    2. 文本存在包含关系时只保留较长者（较短者通常为重复片段/过程性碎片）。

    Args:
        step_results: [(PlanStep, result_text), ...]（保持计划步骤顺序）

    Returns:
        去重后的 [(PlanStep, result_text), ...]（保留原步骤对象与顺序）
    """
    kept: List[tuple] = []          # 已保留的 (step, result_text)
    kept_norms: List[str] = []      # 对应归一化文本

    for step, result_text in step_results:
        norm = "".join(result_text.split())
        if not norm:
            continue

        dup_idx = None
        for j, kept_norm in enumerate(kept_norms):
            if norm == kept_norm or norm in kept_norm or kept_norm in norm:
                dup_idx = j
                break

        if dup_idx is None:
            kept.append((step, result_text))
            kept_norms.append(norm)
        elif len(norm) > len(kept_norms[dup_idx]):
            # 当前文本更长 → 替换已保留的较短碎片
            kept[dup_idx] = (step, result_text)
            kept_norms[dup_idx] = norm

    return kept


def _build_synthesize_prompt(
    plan: ExecutionPlan,
    results: Dict[int, str],
    user_message: str,
    critique: Optional[str] = None,
    previous_draft: Optional[str] = None,
) -> str:
    """
    构建 Synthesize 阶段的 HumanMessage 内容（仅 prompt 文本，不含 SystemMessage——
    由调用方独立构造，与 _synthesize 现有消息分离模式一致，审查 N-2）

    步骤结果先代码级去重（多个步骤可能输出相同的大段内容，重复交给 Synthesize
    会导致最终回答重复——修复：重复输出问题），每步截断 300 字符。

    Args:
        plan: 执行计划
        results: 步骤结果 {step: result_text}
        user_message: 用户消息
        critique: 非空时追加"评审意见"段（反思修正轮）
        previous_draft: 非空时追加"上一版回答"段（供修正定位，审查 P1-3）

    Returns:
        填充后的综合提示词文本
    """
    from app.utils.prompt_loader import load_prompt

    plan_summary = plan.goal
    deduped = _dedupe_step_results([
        (step, results.get(step.step, "无结果")) for step in plan.steps
    ])
    step_results_parts = []
    for step, result_text in deduped:
        step_results_parts.append(f"步骤 {step.step}（{step.action}）：{result_text[:300]}")
    step_results_text = "\n\n".join(step_results_parts)

    prompt_template = load_prompt("plan_synthesize")
    prompt = prompt_template.replace("{user_message}", user_message[:500])
    prompt = prompt.replace("{plan_summary}", plan_summary)
    prompt = prompt.replace("{step_results}", step_results_text)

    if previous_draft:
        prompt += (
            "\n\n---\n以下为上一版回答（请根据评审意见修正："
            "严格按评审意见修改问题点，无关内容保持原样）：\n"
            + previous_draft
        )
    if critique:
        prompt += f"\n\n---\n评审意见（必须修正的问题）：\n{critique}"

    return prompt


async def _generate_answer_buffered(
    chat_model,
    plan: ExecutionPlan,
    results: Dict[int, str],
    user_message: str,
    system_prompt: str,
    config: dict,
    critique: Optional[str] = None,
    previous_draft: Optional[str] = None,
) -> str:
    """
    非流式生成完整回答文本（反思路径专用）

    消息构造与 _synthesize 一致：SystemMessage（system_prompt[:2000]）独立、
    HumanMessage 承载 prompt（审查 N-2）。本函数**不发出任何 SSE 事件**
    （纯文本生成，三轮审查 #4：降级时 plan_synthesize 由 _synthesize 延迟发出，
    不存在"重复事件"）。

    Args:
        critique: 评审意见（修正轮注入）
        previous_draft: 上一版回答（修正轮注入，供定位）

    Returns:
        完整回答文本

    Raises:
        asyncio.TimeoutError: 生成超时（调用方负责降级兜底）
    """
    synthesize_timeout = config.get("synthesize_timeout", 60)  # 与 agent.yaml 对齐（P2-1 技术债务）

    prompt = _build_synthesize_prompt(
        plan, results, user_message,
        critique=critique, previous_draft=previous_draft,
    )
    messages = [
        SystemMessage(content=system_prompt[:2000]),
        HumanMessage(content=prompt),
    ]
    async with asyncio.timeout(synthesize_timeout):
        response = await chat_model.ainvoke(messages)

    content = getattr(response, "content", "") or ""
    return str(content)


async def _synthesize(
    chat_model,
    plan: ExecutionPlan,
    results: Dict[int, str],
    user_message: str,
    system_prompt: str,
    config: dict,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    汇总所有步骤结果，生成最终回答（原流式路径，行为与反思引入前一致）
    """
    yield {"type": "plan_synthesize", "content": "正在汇总结果..."}

    synthesize_timeout = config.get("synthesize_timeout", 60)  # 与 agent.yaml 对齐（P2-1 技术债务）

    prompt = _build_synthesize_prompt(plan, results, user_message)

    try:
        async with asyncio.timeout(synthesize_timeout):
            # 使用流式输出
            messages = [
                SystemMessage(content=system_prompt[:2000]),
                HumanMessage(content=prompt),
            ]

            accumulated = ""
            async for chunk in chat_model.astream(messages):
                if hasattr(chunk, "content") and chunk.content:
                    accumulated += chunk.content
                    yield {"type": "response", "content": chunk.content}

    except asyncio.TimeoutError:
        logger.warning(f"Synthesize 阶段超时 ({synthesize_timeout}s)")
        yield {"type": "response", "content": "\n\n[结果汇总超时，以上为已完成步骤的部分结果]"}
    except Exception as e:
        logger.error(f"Synthesize 阶段失败: {e}")
        yield {"type": "response", "content": f"\n\n[结果汇总失败: {str(e)}]"}


async def _synthesize_with_reflection(
    chat_model,
    plan_model,
    plan: ExecutionPlan,
    results: Dict[int, str],
    user_message: str,
    system_prompt: str,
    config: dict,
    reflection_config: dict,
    start_time: float,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Phase 3 + Phase 4：缓冲生成草稿 → 批判 →（必要时修正）→ 一次性流出

    设计文档：AI对话Reflection反思机制设计方案.md §2.1/§3.2/§3.7/§3.9。
    失败兜底贯穿始终：
    - 初始草稿超时/失败 → 降级 _synthesize 流式（P0-2 防空回答）
    - 短回答（< min_answer_chars）→ 不批判直接流出
    - 预算检查：已用 > total_timeout × 0.8 → 跳过反思（P0-1）
    - 修正草稿超时/失败 → 保留上一版草稿继续
    - 批判失败 → 视为通过（reflection.critique_answer 内 fail-safe）

    Args:
        plan_model: 轻量模型（critic_model="light" 时的批判模型）
        start_time: execute_plan_agent 入口的 time.monotonic()（预算检查基准）
    """
    from app.ai_service.reflection import critique_answer

    yield {"type": "plan_synthesize", "content": "正在汇总结果..."}

    total_timeout = config.get("total_timeout", 300)
    max_refine_rounds = min(reflection_config.get("max_refine_rounds", 1), 2)  # 硬顶 2（Part B 决策 3）
    min_answer_chars = reflection_config.get("trigger", {}).get("min_answer_chars", 500)
    critic_timeout = reflection_config.get("critic_timeout", 15)

    # 批判模型选择：light → 复用 plan_model；main → 主模型（不新增模型实例，Part B 决策 1）
    critic_model = (
        plan_model
        if reflection_config.get("critic_model", "light") == "light"
        else chat_model
    )

    # ===== 草稿①：缓冲生成（不流式，不发出 SSE 事件）=====
    try:
        draft = await _generate_answer_buffered(
            chat_model, plan, results, user_message, system_prompt, config,
        )
    except asyncio.TimeoutError:
        logger.warning("反思路径：初始草稿生成超时，降级 _synthesize 流式")
        async for event in _synthesize(
            chat_model=chat_model, plan=plan, results=results,
            user_message=user_message, system_prompt=system_prompt, config=config,
        ):
            yield event
        return
    except Exception as e:
        logger.error(f"反思路径：初始草稿生成失败，降级 _synthesize 流式: {e}")
        async for event in _synthesize(
            chat_model=chat_model, plan=plan, results=results,
            user_message=user_message, system_prompt=system_prompt, config=config,
        ):
            yield event
        return

    # 触发门槛：短回答不批判（零额外调用）
    if len(draft) < min_answer_chars:
        from app.utils.reflection_metrics import record_reflection_metric
        metric = {
            "task": "plan_execute", "triggered": False,
            "draft_chars": len(draft), "reason": "short",
        }
        logger.info(f"reflection_metric | task=plan_execute | triggered=false | draft_chars={len(draft)} | reason=short")
        record_reflection_metric(metric)
        yield {"type": "response", "content": draft}
        return

    # 预算检查（P0-1）：已用 > 80% 预算 → 跳过反思直接流出
    elapsed = time.monotonic() - start_time
    if elapsed > total_timeout * 0.8:
        logger.warning(f"反思路径：已用 {elapsed:.0f}s > 80% 预算，跳过反思")
        yield {"type": "response", "content": draft}
        return

    # 步骤结果摘要（与 Synthesize 同口径：去重 + 每步 300 截断，供批判核对事实）
    deduped = _dedupe_step_results([
        (step, results.get(step.step, "无结果")) for step in plan.steps
    ])
    step_results_text = "\n\n".join(
        f"步骤 {step.step}（{step.action}）：{result_text[:300]}"
        for step, result_text in deduped
    )

    # ===== 批判 + 修正循环 =====
    yield {"type": "reflection", "stage": "checking", "round": 0}
    refine_rounds = 0
    critique_latencies: List[int] = []
    last_action = "critique"  # 循环退出原因：passed / refined / refine_failed

    for r in range(1, max_refine_rounds + 1):
        t0 = time.monotonic()
        verdict = await critique_answer(
            critic_model, user_message, plan.goal, step_results_text, draft,
            timeout=critic_timeout,
        )
        critique_latencies.append(int((time.monotonic() - t0) * 1000))

        if verdict.passed:
            last_action = "passed"
            break

        # 不合格 → 修正（注入评审意见 + 上一版草稿供定位）
        yield {"type": "reflection", "stage": "refining", "round": r}
        previous_draft = draft
        try:
            draft = await _generate_answer_buffered(
                chat_model, plan, results, user_message, system_prompt, config,
                critique=verdict.issues, previous_draft=previous_draft,
            )
            refine_rounds += 1
            last_action = "refined"
        except asyncio.TimeoutError:
            logger.warning(f"反思路径：第 {r} 轮修正超时，保留上一版草稿")
            draft = previous_draft
            last_action = "refine_failed"
            break
        except Exception as e:
            logger.error(f"反思路径：第 {r} 轮修正失败，保留上一版草稿: {e}")
            draft = previous_draft
            last_action = "refine_failed"
            break

    # check_final：仅当最终草稿未被"通过"判定覆盖时补一次批判（纯度量，
    # 不触发额外修正；一次通过路径的该次批判已记录通过结论，不重复花 15s，
    # 审查三轮 #1）
    final_passed = last_action == "passed"
    if last_action != "passed":
        t0 = time.monotonic()
        final_verdict = await critique_answer(
            critic_model, user_message, plan.goal, step_results_text, draft,
            timeout=critic_timeout,
        )
        critique_latencies.append(int((time.monotonic() - t0) * 1000))
        final_passed = final_verdict.passed

    logger.info(
        f"reflection_metric | task=plan_execute | triggered=true "
        f"| draft_chars={len(draft)} | refine_rounds={refine_rounds} "
        f"| final_passed={final_passed} | latency_ms={sum(critique_latencies)} "
        f"| critique_latency_ms={critique_latencies}"
    )
    # P2 增强 1：结构化指标落盘（scripts/reflection_stats.py 消费）
    from app.utils.reflection_metrics import record_reflection_metric
    record_reflection_metric({
        "task": "plan_execute", "triggered": True,
        "draft_chars": len(draft), "refine_rounds": refine_rounds,
        "final_passed": final_passed,
        "latency_ms": sum(critique_latencies),
        "critique_latency_ms": critique_latencies,
    })

    # 一次性流出最终回答（无打字机效果，复杂任务可接受，设计 §3.5）
    yield {"type": "response", "content": draft}


# ============================================================
# 主入口：execute_plan_agent
# ============================================================

async def execute_plan_agent(
    chat_model,
    plan_model,
    user_id: str,
    user_message: str,
    system_prompt: str,
    compressed_messages: Optional[list] = None,
    db_session_factory=None,
    note_service=None,
    review_service=None,
    email_service=None,
    ppt_service=None,
    timeout: int = 120,
    attachment_content: Optional[List[dict]] = None,
    attachment_names: Optional[List[str]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Plan-and-Execute Agent 执行器

    阶段：Plan → Execute → Synthesize（→ Reflect，reflection.enable 开启时）

    Args:
        chat_model: 主模型（用于 Execute + Synthesize）
        plan_model: 轻量模型（用于 Plan）
        user_id: 用户 ID
        user_message: 用户消息
        system_prompt: 系统提示词（含 RAG 上下文）
        compressed_messages: 压缩后的历史消息
        db_session_factory: 数据库会话工厂
        note_service: 笔记服务实例
        review_service: 回顾服务实例
        email_service: 邮件发送服务实例（供 send_email 工具使用）
        timeout: 总超时秒数
        attachment_content: 附件多模态 content blocks（执行阶段透传）
        attachment_names: 附件文件名列表（plan_model 无多模态能力，以摘要注入）

    Yields:
        SSE 事件字典
    """
    config = get_plan_execute_config()
    reflection_config = get_reflection_config()
    total_timeout = config.get("total_timeout", timeout)

    # 仅发附件（空消息）时提供兜底文本，避免 plan/synthesize 提示词空洞
    effective_message = user_message.strip() or "请分析我发送的附件"

    try:
        async with asyncio.timeout(total_timeout):
            start_time = time.monotonic()  # 反思预算检查基准（P0-1）
            # ===== Phase 1: Plan =====
            try:
                plan = await _generate_plan(
                    plan_model, effective_message, system_prompt,
                    attachment_names=attachment_names,
                )
            except (ValueError, Exception) as e:
                # Plan 生成失败 → 降级为 ReAct
                logger.warning(f"Plan 生成失败，降级为 ReAct: {e}")
                yield {
                    "type": "plan_fallback",
                    "reason": str(e),
                }
                return

            # 推送计划事件
            yield {
                "type": "plan_start",
                "goal": plan.goal,
                "total_steps": len(plan.steps),
            }
            for step in plan.steps:
                yield {
                    "type": "plan_step",
                    "step": step.step,
                    "action": step.action,
                    "status": "pending",
                }

            # ===== Phase 2: Execute ===== 
            previous_results: Dict[int, str] = {}      # 记录已完成步骤的结果
            batches = _topological_batches(plan.steps) # 拓扑排序批次

            completed_steps = 0
            for batch in batches:               # 逐批执行
                async for event in _execute_batch(             # 异步执行批次
                    batch=batch,
                    chat_model=chat_model,
                    user_id=user_id,
                    user_message=effective_message,
                    system_prompt=system_prompt,
                    compressed_messages=compressed_messages,   # 压缩后历史消息
                    db_session_factory=db_session_factory,     # 数据库会话工厂
                    note_service=note_service,                 # 笔记服务实例
                    review_service=review_service,             # 回顾服务实例
                    email_service=email_service,               # 邮件发送服务实例
                    ppt_service=ppt_service,                   # PPT 生成服务实例
                    config=config,                             # 配置字典
                    previous_results=previous_results,         # 已完成步骤结果
                    attachment_content=attachment_content,     # 附件多模态 blocks
                ):
                    yield event
                    if event.get("type") == "plan_step_end":
                        completed_steps += 1

            # ===== Phase 3+4: Synthesize + Reflect =====    汇总结果（+ 综合后自检 L1）
            if reflection_config.get("enable", False):
                async for event in _synthesize_with_reflection(
                    chat_model=chat_model,
                    plan_model=plan_model,
                    plan=plan,
                    results=previous_results,
                    user_message=effective_message,
                    system_prompt=system_prompt,
                    config=config,
                    reflection_config=reflection_config,
                    start_time=start_time,
                ):
                    yield event
            else:
                # 反思关闭：原流式路径（行为与改动前一致）
                async for event in _synthesize(
                    chat_model=chat_model,
                    plan=plan,
                    results=previous_results,
                    user_message=effective_message,
                    system_prompt=system_prompt,
                    config=config,
                ):
                    yield event

            # 完成事件
            yield {
                "type": "plan_complete",
                "total_steps": len(plan.steps),
                "completed_steps": completed_steps,
            }

    except asyncio.TimeoutError:
        logger.warning(f"Plan-and-Execute 总超时 ({total_timeout}s)")
        yield {"type": "error", "content": f"复杂任务执行超时（{total_timeout}秒），请简化请求后重试"}
    except Exception as e:
        logger.error(f"Plan-and-Execute 执行异常: {type(e).__name__}: {e}", exc_info=True)
        yield {"type": "error", "content": f"执行失败: {str(e)}"}
