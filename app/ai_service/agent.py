"""
Agent 工厂模块

使用工厂模式创建 Agent（基于 LangGraph CompiledStateGraph）：
- 每次调用创建全新实例，避免全局状态污染
- 集成 LangChain create_agent（langchain 1.3+ API）
- 支持系统提示词和工具集
"""

from typing import List, Optional

from langchain.agents.middleware import wrap_tool_call
from langchain_core.callbacks.manager import adispatch_custom_event

from app.ai_service.tool_approvals import approval_workflow
from app.ai_service.tool_operations import authorize_turn, run_artifact_tool, tool_turn
from app.ai_service.tool_results import ARTIFACT_TOOLS
from app.core.logger_handler import logger
from app.model_config.snapshots import inference_actor
from app.model_config.task_budget import current_model_budget
from app.rag.citations import authorize_turn_evidence, current_citations
from app.utils.config import get_agent_config


@wrap_tool_call
async def guard_tool_call(request, handler):
    if request.tool is not None:
        await authorize_turn_evidence()
        workflow, turn = approval_workflow.get(), tool_turn.get()
        if workflow is not None and turn is not None:
            actor = inference_actor.get() or {}
            async with workflow.session_factory() as db:
                await authorize_turn(db, actor.get('user_id'), turn)
            await workflow.assert_active()
        budget = current_model_budget.get()
        if budget is not None:
            await budget.reserve_tool()
        if workflow is not None and turn is not None and request.tool.name in ARTIFACT_TOOLS:
            async def on_reserved(operation_id):
                await adispatch_custom_event('artifact_operation_reserved', {
                    'operation_id': operation_id, 'tool_call_id': request.tool_call['id'],
                }, config=request.runtime.config)

            output = await run_artifact_tool(workflow, actor.get('user_id'), turn, request.tool_call,
                                             lambda: handler(request), on_reserved=on_reserved)
            artifact = getattr(output, 'artifact', None)
            if isinstance(artifact, dict) and artifact.get('file'):
                await adispatch_custom_event('committed_tool_artifact', artifact['file'], config=request.runtime.config)
            return output
    return await handler(request)


class AgentFactory:
    """
    Agent 工厂

    每次调用 create_agent() 创建全新的 Agent 实例，
    避免多用户并发时的全局状态污染。
    """

    @staticmethod
    def create_agent(
        chat_model,
        tools: list,
        system_prompt: str = "",
        max_iterations: int = 10,
    ):
        """
        创建 Agent 实例

        Args:
            chat_model: LangChain Chat 模型
            tools: Agent 可用的工具列表
            system_prompt: 系统提示词
            max_iterations: 最大迭代次数（递归限制）

        Returns:
            CompiledStateGraph 实例（可通过 astream/ainvoke 调用）
        """
        from langchain.agents import create_agent

        budget = current_model_budget.get()
        # 构建 Agent（langchain 1.3+ API）
        agent = create_agent(
            model=chat_model,
            tools=tools or [],
            middleware=[guard_tool_call] if approval_workflow.get() is not None or current_citations.get() is not None
            or (budget is not None and budget.tool_limited) else [],
            system_prompt=system_prompt or "你是一个智能笔记助手，帮助用户管理知识、检索信息和回答问题。",
        )

        logger.debug(f"Agent 创建完成: tools={len(tools)}, max_iter={max_iterations}")
        return agent, max_iterations
