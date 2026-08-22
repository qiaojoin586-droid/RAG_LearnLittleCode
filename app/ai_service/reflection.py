"""
Reflection 反思机制（L1 综合后自检）

设计文档：docs/AI对话Reflection反思机制设计方案.md（源自 MCP Part B §8~§10 的
L1 结果自检：生成 → 批判 → 修正，单轮闭环）。

职责边界：本模块不感知 Plan/Execute 内部结构，只接收"要评判的文本 + 上下文摘要"
——未来 ReAct 路径接入（stream_done 后 L1）可原样复用，无需重构。

核心原则（fail-safe）：反思是增强不是关卡。批判解析失败/超时/异常一律返回
passed=True，绝不阻塞回答输出。
"""

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Optional

from langchain_core.messages import HumanMessage

from app.core.logger_handler import logger


@dataclass
class ReflectionVerdict:
    """批判判定结果"""
    passed: bool
    issues: str = ""


def parse_critique_response(content: str) -> ReflectionVerdict:
    """
    解析批判模型的 JSON 响应（纯函数）

    复用 _generate_plan 的 codeblock 剥离模式；解析失败/字段缺失返回
    passed=True（fail-safe，不阻塞回答）。

    Args:
        content: 批判模型原始输出文本

    Returns:
        ReflectionVerdict；任何解析异常 → passed=True
    """
    text = re.sub(r"```json\s*", "", content or "")
    text = re.sub(r"```\s*", "", text)
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"批判 JSON 解析失败（视为通过）: {e}, raw={text[:200]}")
        return ReflectionVerdict(passed=True)

    passed = bool(data.get("pass", True))
    issues = str(data.get("issues", "") or "").strip()
    if not passed and not issues:
        # 判不合格但无修改指令 → 无法修正，视为通过
        logger.warning("批判判定不合格但 issues 为空（视为通过）")
        return ReflectionVerdict(passed=True)
    return ReflectionVerdict(passed=passed, issues=issues)


def build_critique_prompt(
    user_message: str,
    plan_summary: str,
    step_results_text: str,
    draft: str,
) -> str:
    """
    构建批判提示词

    占位符替换而非 str.format——模板含 JSON 花括号示例（与 plan_generation 同模式）。

    Args:
        user_message: 用户问题
        plan_summary: 计划目标
        step_results_text: 步骤结果摘要（与 Synthesize 同口径：去重 + 截断）
        draft: 待评审回答草稿

    Returns:
        填充后的批判提示词
    """
    from app.utils.prompt_loader import load_prompt

    prompt = load_prompt("reflection_critique")
    prompt = prompt.replace("{user_message}", user_message[:1000])
    prompt = prompt.replace("{plan_summary}", plan_summary)
    prompt = prompt.replace("{step_results}", step_results_text)
    prompt = prompt.replace("{answer}", draft)
    return prompt


async def critique_answer(
    plan_model,
    user_message: str,
    plan_summary: str,
    step_results_text: str,
    draft: str,
    timeout: int = 15,
) -> ReflectionVerdict:
    """
    批判一次回答草稿

    本函数永不抛异常：超时/模型异常/解析失败一律返回 passed=True
    （fail-safe：坏判定不得阻塞回答，反思是增强不是关卡）。

    Args:
        plan_model: 批判模型（轻量；由调用方按 reflection.critic_model 配置选择）
        user_message: 用户问题
        plan_summary: 计划目标
        step_results_text: 步骤结果摘要（与 Synthesize 同口径）
        draft: 待评审回答草稿
        timeout: 批判调用超时（秒），独立预算

    Returns:
        ReflectionVerdict；任何异常 → passed=True
    """
    try:
        prompt = build_critique_prompt(user_message, plan_summary, step_results_text, draft)
        async with asyncio.timeout(timeout):
            response = await plan_model.ainvoke([HumanMessage(content=prompt)])
            content = getattr(response, "content", "") or ""
            content = str(content).strip()
    except asyncio.TimeoutError:
        logger.warning(f"批判调用超时 ({timeout}s)，视为通过")
        return ReflectionVerdict(passed=True)
    except Exception as e:
        logger.warning(f"批判调用异常（视为通过）: {type(e).__name__}: {e}")
        return ReflectionVerdict(passed=True)

    return parse_critique_response(content)


def build_repair_note(failed_tool: Optional[str], error_content: str) -> str:
    """
    L2 修复轮指令（Part B §9.3 决策 4）

    回灌给重试轮的 user_message 追加段：告知上次失败的工具与原因，
    约束"最多再尝试 1 次工具调用、无法确定修复方式就基于已有信息回答"，
    防止"重试 → 再失败 → 再重试"放大。

    Args:
        failed_tool: 流内最后失败的工具名（无则 None）
        error_content: 错误信息（截断）

    Returns:
        追加到 user_message 的修复指令文本
    """
    tool_part = f"上次调用工具 `{failed_tool}` 失败" if failed_tool else "上次执行失败"
    return (
        f"\n\n[系统提示：{tool_part}（原因：{str(error_content)[:150]}）。"
        "请分析失败原因，修正参数或更换策略，最多再尝试 1 次工具调用；"
        "若无法确定修复方式，请直接基于已有信息回答，不要反复重试。]"
    )
