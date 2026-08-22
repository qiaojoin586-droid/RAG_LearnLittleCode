"""
Reflection 反思机制单测（L1 综合后自检）

覆盖设计文档 AI对话Reflection反思机制设计方案.md §5.1：
- parse_critique_response 纯函数（fail-safe）
- critique_answer stub 模型（判定 / 超时 / 异常）
- execute_plan_agent 端到端事件流（stub 双模型，无 LLM/DB/网络依赖）

运行: python -m pytest tests/test_reflection.py -v
"""

import asyncio
import json
from pathlib import Path

import pytest

from app.ai_service import agent_runner, chat_route, plan_execute_agent
from app.ai_service.plan_execute_agent import execute_plan_agent
from app.ai_service.reflection import (
    build_repair_note,
    critique_answer,
    parse_critique_response,
)
from app.utils import reflection_metrics


PLAN_JSON = (
    '{"goal": "测试目标", "steps": ['
    '{"step": 1, "action": "生成一段内容", "tool": "none", "depends_on": []}'
    "]}"
)
CRITIQUE_PASS = '{"pass": true, "issues": ""}'
CRITIQUE_FAIL = '{"pass": false, "issues": "回答未正面回应用户问题，请重写开头段"}'
LONG_DRAFT = "这是最终回答。" * 80  # 560 字符，超过 min_answer_chars=500


class _Msg:
    """模拟 LangChain 消息对象"""

    def __init__(self, content):
        self.content = content


class StubModel:
    """可编程 stub 模型：ainvoke 按结果队列依次返回；astream 返回固定 chunk 序列"""

    def __init__(self, invoke_results=None, stream_chunks=(), on_invoke=None):
        self.invoke_results = list(invoke_results or [])
        self.stream_chunks = list(stream_chunks)
        self.on_invoke = on_invoke  # 每次 ainvoke 后回调（测试挂钩，如推进伪造时钟）
        self.invoke_calls = 0
        self.astream_calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.invoke_calls += 1
        if self.on_invoke:
            self.on_invoke(self.invoke_calls)
        if not self.invoke_results:
            return _Msg("")
        result = self.invoke_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return _Msg(result)

    async def astream(self, messages, **kwargs):
        self.astream_calls += 1
        for chunk in self.stream_chunks:
            yield _Msg(chunk)


class _FakeMonotonic:
    """伪造时钟：初始 0，advance() 跳到任意值（配合 StubModel.on_invoke 挂钩推进）"""

    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, value):
        self.value = value


async def _collect(agen):
    """收集异步生成器的全部事件"""
    events = []
    async for event in agen:
        events.append(event)
    return events


# ========== 纯函数：parse_critique_response ==========

class TestParseCritiqueResponse:
    """批判 JSON 解析：合法判定 / fail-safe 兜底"""

    def test_pass(self):
        verdict = parse_critique_response(CRITIQUE_PASS)
        assert verdict.passed is True
        assert verdict.issues == ""

    def test_fail_with_issues(self):
        verdict = parse_critique_response(CRITIQUE_FAIL)
        assert verdict.passed is False
        assert "重写" in verdict.issues

    def test_fail_without_issues_falls_back_to_pass(self):
        # 判不合格但无修改指令 → 无法修正，视为通过
        verdict = parse_critique_response('{"pass": false, "issues": ""}')
        assert verdict.passed is True

    def test_markdown_codeblock(self):
        verdict = parse_critique_response(f"```json\n{CRITIQUE_FAIL}\n```")
        assert verdict.passed is False

    @pytest.mark.parametrize("bad", ["", "not json", '{"pass": "maybe"}', None])
    def test_invalid_json_falls_back_to_pass(self, bad):
        verdict = parse_critique_response(bad)
        assert verdict.passed is True


# ========== critique_answer（stub 模型）==========

class TestCritiqueAnswer:
    """批判调用：判定透传 + 超时/异常 fail-safe（永不抛异常）"""

    @pytest.mark.asyncio
    async def test_pass_verdict(self):
        model = StubModel(invoke_results=[CRITIQUE_PASS])
        verdict = await critique_answer(model, "问题", "目标", "步骤结果", LONG_DRAFT)
        assert verdict.passed is True

    @pytest.mark.asyncio
    async def test_fail_verdict(self):
        model = StubModel(invoke_results=[CRITIQUE_FAIL])
        verdict = await critique_answer(model, "问题", "目标", "步骤结果", LONG_DRAFT)
        assert verdict.passed is False
        assert verdict.issues

    @pytest.mark.asyncio
    async def test_timeout_falls_back_to_pass(self):
        model = StubModel(invoke_results=[asyncio.TimeoutError()])
        verdict = await critique_answer(model, "问题", "目标", "步骤结果", LONG_DRAFT, timeout=1)
        assert verdict.passed is True

    @pytest.mark.asyncio
    async def test_exception_falls_back_to_pass(self):
        model = StubModel(invoke_results=[RuntimeError("boom")])
        verdict = await critique_answer(model, "问题", "目标", "步骤结果", LONG_DRAFT, timeout=1)
        assert verdict.passed is True


# ========== execute_plan_agent 端到端（stub 双模型）==========

@pytest.fixture
def configs(monkeypatch):
    """注入可控配置：并行关闭、超时短、反思默认开启（lambda 捕获 dict 引用，测试内可改）"""
    plan_cfg = {
        "max_steps": 5, "plan_timeout": 5, "step_timeout": 5,
        "synthesize_timeout": 5, "total_timeout": 30,
        "enable_parallel": False, "max_parallel_steps": 3,
    }
    refl_cfg = {
        "enable": True, "critic_model": "light", "critic_timeout": 5,
        "max_refine_rounds": 1, "trigger": {"min_answer_chars": 500},
    }
    monkeypatch.setattr(plan_execute_agent, "get_plan_execute_config", lambda: plan_cfg)
    monkeypatch.setattr(plan_execute_agent, "get_reflection_config", lambda: refl_cfg)
    # 测试隔离：指标落盘转 no-op（不写真实 logs/ 文件）
    monkeypatch.setattr(reflection_metrics, "record_reflection_metric", lambda m: None)
    return plan_cfg, refl_cfg


@pytest.fixture
def no_metrics(monkeypatch):
    """测试隔离：指标落盘转 no-op（chat_route 顶部导入的引用同样替换）"""
    monkeypatch.setattr(reflection_metrics, "record_reflection_metric", lambda m: None)
    monkeypatch.setattr(chat_route, "record_reflection_metric", lambda m: None)


def _run(chat_model, plan_model):
    """构造 execute_plan_agent 调用"""
    return execute_plan_agent(
        chat_model=chat_model, plan_model=plan_model,
        user_id="u1", user_message="请完成这个复杂任务", system_prompt="sys",
    )


class TestExecutePlanAgentReflection:
    """端到端事件流（最小化计划：单步骤 tool=none）"""

    @pytest.mark.asyncio
    async def test_pass_path_streams_draft_once(self, configs):
        # 批判一次通过 → checking 事件 + 草稿一次性流出；无 check_final（不重复批判）
        plan_model = StubModel(invoke_results=[PLAN_JSON, CRITIQUE_PASS])
        chat_model = StubModel(invoke_results=[LONG_DRAFT], stream_chunks=["步骤内容"])

        events = await _collect(_run(chat_model, plan_model))

        stages = [e["stage"] for e in events if e.get("type") == "reflection"]
        assert stages == ["checking"]
        responses = [e for e in events if e["type"] == "response"]
        assert len(responses) == 1
        assert responses[0]["content"] == LONG_DRAFT
        # plan 1 次 + 批判 1 次，恰好 2 次（无 check_final）
        assert plan_model.invoke_calls == 2

    @pytest.mark.asyncio
    async def test_fail_then_refine_streams_corrected_draft(self, configs):
        # 批判失败 → refining + 修正草稿流出；修正后 check_final 通过
        plan_model = StubModel(invoke_results=[PLAN_JSON, CRITIQUE_FAIL, CRITIQUE_PASS])
        chat_model = StubModel(
            invoke_results=[LONG_DRAFT, "修正后的回答。" * 60],
            stream_chunks=["步骤内容"],
        )

        events = await _collect(_run(chat_model, plan_model))

        stages = [e["stage"] for e in events if e.get("type") == "reflection"]
        assert stages == ["checking", "refining"]
        responses = [e for e in events if e["type"] == "response"]
        assert len(responses) == 1
        assert responses[0]["content"].startswith("修正后的回答")
        # plan + 批判① + check_final 共 3 次
        assert plan_model.invoke_calls == 3

    @pytest.mark.asyncio
    async def test_check_final_does_not_trigger_extra_refine(self, configs):
        # 修正后 check_final 仍不合格 → 绝不产生第三轮 refining（收敛边界）
        plan_model = StubModel(invoke_results=[PLAN_JSON, CRITIQUE_FAIL, CRITIQUE_FAIL])
        chat_model = StubModel(
            invoke_results=[LONG_DRAFT, "修正后的回答。" * 60],
            stream_chunks=["步骤内容"],
        )

        events = await _collect(_run(chat_model, plan_model))

        stages = [e["stage"] for e in events if e.get("type") == "reflection"]
        assert stages == ["checking", "refining"]
        responses = [e for e in events if e["type"] == "response"]
        assert responses[0]["content"].startswith("修正后的回答")

    @pytest.mark.asyncio
    async def test_refine_timeout_keeps_previous_draft(self, configs):
        # 修正草稿超时 → 保留上一版草稿（三轮审查 #5）
        plan_model = StubModel(invoke_results=[PLAN_JSON, CRITIQUE_FAIL, CRITIQUE_PASS])
        chat_model = StubModel(
            invoke_results=[LONG_DRAFT, asyncio.TimeoutError()],
            stream_chunks=["步骤内容"],
        )

        events = await _collect(_run(chat_model, plan_model))

        responses = [e for e in events if e["type"] == "response"]
        assert len(responses) == 1
        assert responses[0]["content"] == LONG_DRAFT

    @pytest.mark.asyncio
    async def test_initial_draft_timeout_falls_back_to_streaming(self, configs):
        # 初始草稿超时 → 降级 _synthesize 流式（P0-2 防空回答），无 reflection 事件
        plan_model = StubModel(invoke_results=[PLAN_JSON])
        chat_model = StubModel(
            invoke_results=[asyncio.TimeoutError()],
            stream_chunks=["草稿兜底"],
        )

        events = await _collect(_run(chat_model, plan_model))

        assert "reflection" not in [e.get("type") for e in events]
        responses = [e for e in events if e["type"] == "response"]
        assert [r["content"] for r in responses] == ["草稿兜底"]

    @pytest.mark.asyncio
    async def test_budget_exceeded_skips_reflection(self, configs, monkeypatch):
        # 已用时间 > 80% 预算 → 跳过反思直接流出草稿（P0-1）
        fake = _FakeMonotonic()
        monkeypatch.setattr(plan_execute_agent.time, "monotonic", fake)
        plan_model = StubModel(invoke_results=[PLAN_JSON])
        # 草稿生成（第一次 ainvoke）后把时钟推进到 25s（> 30×0.8=24s）
        chat_model = StubModel(
            invoke_results=[LONG_DRAFT], stream_chunks=["步骤内容"],
            on_invoke=lambda calls: fake.advance(25.0),
        )

        events = await _collect(_run(chat_model, plan_model))

        assert "reflection" not in [e.get("type") for e in events]
        responses = [e for e in events if e["type"] == "response"]
        assert [r["content"] for r in responses] == [LONG_DRAFT]
        assert plan_model.invoke_calls == 1  # 只有 Plan，无批判

    @pytest.mark.asyncio
    async def test_short_draft_skips_critique(self, configs):
        # 短回答 < min_answer_chars → 不批判直接流出
        plan_model = StubModel(invoke_results=[PLAN_JSON])
        chat_model = StubModel(invoke_results=["短回答"], stream_chunks=["步骤内容"])

        events = await _collect(_run(chat_model, plan_model))

        assert "reflection" not in [e.get("type") for e in events]
        responses = [e for e in events if e["type"] == "response"]
        assert [r["content"] for r in responses] == ["短回答"]
        assert plan_model.invoke_calls == 1

    @pytest.mark.asyncio
    async def test_reflection_disabled_keeps_streaming(self, configs):
        # 开关关闭 → 与改动前一致：逐 chunk 流式、无 reflection 事件
        configs[1]["enable"] = False
        plan_model = StubModel(invoke_results=[PLAN_JSON])
        chat_model = StubModel(stream_chunks=["汇", "总", "结", "果"])

        events = await _collect(_run(chat_model, plan_model))

        assert "reflection" not in [e.get("type") for e in events]
        responses = [e for e in events if e["type"] == "response"]
        assert [r["content"] for r in responses] == ["汇", "总", "结", "果"]
        assert chat_model.invoke_calls == 0  # 流式路径无 ainvoke


# ========== L2 工具失败修复（Self-Repair，Part B 决策 4）==========

class _AgentStub:
    """模拟 execute_agent 的异步生成器：按预设脚本逐次执行（每次一轮完整流）"""

    def __init__(self, scripts):
        """
        scripts: 列表，每项为一次执行的事件序列（列表）。
                 序列可包含事件 dict 或 BaseException（执行到该处时抛出）。
        """
        self.scripts = list(scripts)
        self.runs = 0
        self.calls: list = []  # 记录每次执行的 kwargs（供断言修复指令注入）

    async def __call__(self, **kwargs):
        self.runs += 1
        self.calls.append(kwargs)
        script = self.scripts.pop(0) if self.scripts else []
        for item in script:
            if isinstance(item, BaseException):
                raise item
            yield item


def _l2_reflection_cfg(enable=True, no_retry_tools=("send_email",)):
    return {
        "enable": enable, "critic_model": "light", "critic_timeout": 5,
        "max_refine_rounds": 1, "trigger": {"min_answer_chars": 500},
        "l2_repair": {
            "enable": True, "max_repair_attempts": 1,
            "no_retry_tools": list(no_retry_tools),
        },
    }


class TestBuildRepairNote:
    """L2 修复指令纯函数"""

    def test_with_tool(self):
        note = build_repair_note("tavily_search", "连接超时")
        assert "`tavily_search`" in note
        assert "连接超时" in note
        assert "最多再尝试 1 次" in note

    def test_without_tool(self):
        note = build_repair_note(None, "boom")
        assert "上次执行失败" in note


class TestReactEventsL2:
    """ReAct 路径 L2：error 拦截 → repairing → 重跑；send_email 例外"""

    def _ctx(self):
        return chat_route.ChatRouteContext(
            agent_model=None, user_id="u1", user_message="问题",
            system_prompt="sys", compressed_messages=[], db_session_factory=None,
        )

    @pytest.mark.asyncio
    async def test_repair_succeeds_after_first_error(self, monkeypatch, no_metrics):
        monkeypatch.setattr(chat_route, "get_reflection_config", lambda: _l2_reflection_cfg())
        stub = _AgentStub([
            # 第一次执行：工具失败 → error
            [{"type": "tool_start", "name": "tavily_search"},
             {"type": "tool_end", "name": "tavily_search", "error": True},
             {"type": "error", "content": "超时"}],
            # 修复轮：成功
            [{"type": "tool_start", "name": "tavily_search"},
             {"type": "tool_end", "name": "tavily_search", "duration_ms": 100},
             {"type": "response", "content": "修复后回答"}],
        ])
        monkeypatch.setattr(chat_route, "execute_agent", stub)

        events = await _collect(chat_route.react_events(self._ctx(), timeout=5))

        types = [e.get("type") for e in events]
        assert "error" not in types  # 第一次 error 被拦截，不透传
        repairing = [e for e in events if e.get("type") == "reflection"]
        assert repairing == [{"type": "reflection", "stage": "repairing", "round": 1}]
        responses = [e for e in events if e["type"] == "response"]
        assert [r["content"] for r in responses] == ["修复后回答"]
        assert stub.runs == 2
        # 修复指令注入：第二次执行的 user_message 含失败工具与修复说明
        assert "上次调用工具 `tavily_search` 失败" in stub.calls[1]["user_message"]
        assert "最多再尝试 1 次" in stub.calls[1]["user_message"]

    @pytest.mark.asyncio
    async def test_send_email_error_not_retried(self, monkeypatch, no_metrics):
        monkeypatch.setattr(chat_route, "get_reflection_config", lambda: _l2_reflection_cfg())
        stub = _AgentStub([
            [{"type": "tool_start", "name": "send_email"},
             {"type": "tool_end", "name": "send_email", "error": True},
             {"type": "error", "content": "邮件发送失败"}],
        ])
        monkeypatch.setattr(chat_route, "execute_agent", stub)

        events = await _collect(chat_route.react_events(self._ctx(), timeout=5))

        types = [e.get("type") for e in events]
        assert "error" in types  # error 直接透传
        assert "reflection" not in types  # 无修复轮
        assert stub.runs == 1

    @pytest.mark.asyncio
    async def test_repair_fails_again_transmits_second_error(self, monkeypatch, no_metrics):
        monkeypatch.setattr(chat_route, "get_reflection_config", lambda: _l2_reflection_cfg())
        stub = _AgentStub([
            [{"type": "error", "content": "第一次失败"}],
            [{"type": "error", "content": "第二次失败"}],
        ])
        monkeypatch.setattr(chat_route, "execute_agent", stub)

        events = await _collect(chat_route.react_events(self._ctx(), timeout=5))

        errors = [e for e in events if e["type"] == "error"]
        assert len(errors) == 1  # 只透传最终 error
        assert errors[0]["content"] == "第二次失败"
        assert stub.runs == 2

    @pytest.mark.asyncio
    async def test_l2_disabled_transmits_error_directly(self, monkeypatch, no_metrics):
        cfg = _l2_reflection_cfg()
        cfg["l2_repair"]["enable"] = False
        monkeypatch.setattr(chat_route, "get_reflection_config", lambda: cfg)
        stub = _AgentStub([[{"type": "error", "content": "失败"}]])
        monkeypatch.setattr(chat_route, "execute_agent", stub)

        events = await _collect(chat_route.react_events(self._ctx(), timeout=5))

        assert [e["type"] for e in events] == ["error"]
        assert stub.runs == 1  # 行为与改动前一致


class TestStepLevelL2:
    """Plan-Execute 步骤级 L2：步骤失败 → 重试 1 次"""

    def _step(self, tool="tavily_search"):
        return plan_execute_agent.PlanStep(step=1, action="搜索", tool=tool, depends_on=[])

    @pytest.mark.asyncio
    async def test_step_repair_succeeds(self, monkeypatch, no_metrics):
        # 注意：_execute_step 内为函数内延迟导入 execute_agent，
        # 必须 patch agent_runner 模块属性（而非 plan_execute_agent 的引用）
        monkeypatch.setattr(plan_execute_agent, "get_reflection_config", lambda: _l2_reflection_cfg())
        stub = _AgentStub([
            [{"type": "tool_end", "name": "tavily_search", "error": True},
             {"type": "error", "content": "失败"}],
            [{"type": "response", "content": "重试成功的结果"}],
        ])
        monkeypatch.setattr(agent_runner, "execute_agent", stub)
        step = self._step()

        events = await _collect(plan_execute_agent._execute_step(
            step=step, chat_model=None, user_id="u1",
            user_message="任务", system_prompt="sys", compressed_messages=[],
            db_session_factory=None, note_service=None, review_service=None,
            email_service=None, ppt_service=None, step_timeout=5,
            previous_results={},
        ))

        types = [e.get("type") for e in events]
        assert "reflection" in types
        repairing = [e for e in events if e.get("type") == "reflection" and e.get("stage") == "repairing"]
        assert repairing
        # 修复成功后清空 repairing 状态
        clear = [e for e in events if e.get("type") == "reflection" and e.get("stage") == ""]
        assert clear
        assert stub.runs == 2
        # 修复指令注入
        assert "上次调用工具 `tavily_search` 失败" in stub.calls[1]["user_message"]

    @pytest.mark.asyncio
    async def test_step_repair_result_recorded(self, monkeypatch, no_metrics):
        # 验证重试成功后 step.result 正确填充（复用同一 step 对象）
        monkeypatch.setattr(plan_execute_agent, "get_reflection_config", lambda: _l2_reflection_cfg())
        stub = _AgentStub([
            [{"type": "error", "content": "boom"}],
            [{"type": "response", "content": "OK 结果"}],
        ])
        monkeypatch.setattr(agent_runner, "execute_agent", stub)
        step = self._step()

        events = await _collect(plan_execute_agent._execute_step(
            step=step, chat_model=None, user_id="u1",
            user_message="任务", system_prompt="sys", compressed_messages=[],
            db_session_factory=None, note_service=None, review_service=None,
            email_service=None, ppt_service=None, step_timeout=5,
            previous_results={},
        ))

        assert step.result == "OK 结果"
        assert [e["type"] for e in events][-1] == "plan_step_end"

    @pytest.mark.asyncio
    async def test_step_send_email_not_retried(self, monkeypatch, no_metrics):
        monkeypatch.setattr(plan_execute_agent, "get_reflection_config", lambda: _l2_reflection_cfg())
        stub = _AgentStub([
            [{"type": "tool_end", "name": "send_email", "error": True},
             {"type": "error", "content": "邮件失败"}],
        ])
        monkeypatch.setattr(agent_runner, "execute_agent", stub)
        step = self._step(tool="send_email")

        events = await _collect(plan_execute_agent._execute_step(
            step=step, chat_model=None, user_id="u1",
            user_message="任务", system_prompt="sys", compressed_messages=[],
            db_session_factory=None, note_service=None, review_service=None,
            email_service=None, ppt_service=None, step_timeout=5,
            previous_results={},
        ))

        assert stub.runs == 1  # 不重试
        assert any(e["type"] == "error" for e in events)  # error 透传
        assert "步骤 1 执行出错" in step.result

    @pytest.mark.asyncio
    async def test_step_exception_triggers_repair(self, monkeypatch, no_metrics):
        # execute_agent 整体抛异常 → 同样触发修复轮
        monkeypatch.setattr(plan_execute_agent, "get_reflection_config", lambda: _l2_reflection_cfg())
        stub = _AgentStub([
            [RuntimeError("stream broken")],
            [{"type": "response", "content": "恢复后的结果"}],
        ])
        monkeypatch.setattr(agent_runner, "execute_agent", stub)
        step = self._step()

        events = await _collect(plan_execute_agent._execute_step(
            step=step, chat_model=None, user_id="u1",
            user_message="任务", system_prompt="sys", compressed_messages=[],
            db_session_factory=None, note_service=None, review_service=None,
            email_service=None, ppt_service=None, step_timeout=5,
            previous_results={},
        ))

        assert stub.runs == 2
        assert step.result == "恢复后的结果"
        assert [e["type"] for e in events][-1] == "plan_step_end"

    @pytest.mark.asyncio
    async def test_step_l2_disabled_transmits_error(self, monkeypatch, no_metrics):
        cfg = _l2_reflection_cfg()
        cfg["l2_repair"]["enable"] = False
        monkeypatch.setattr(plan_execute_agent, "get_reflection_config", lambda: cfg)
        stub = _AgentStub([[{"type": "error", "content": "失败"}]])
        monkeypatch.setattr(agent_runner, "execute_agent", stub)
        step = self._step()

        await _collect(plan_execute_agent._execute_step(
            step=step, chat_model=None, user_id="u1",
            user_message="任务", system_prompt="sys", compressed_messages=[],
            db_session_factory=None, note_service=None, review_service=None,
            email_service=None, ppt_service=None, step_timeout=5,
            previous_results={},
        ))

        assert stub.runs == 1  # 行为与改动前一致


# ========== ReAct 路径 L1 结果自检（P2 增强 3：分段流式）==========

def _react_l1_cfg(enable=True):
    cfg = _l2_reflection_cfg()
    cfg["react_l1_enable"] = enable
    return cfg


class TestReactEventsL1:
    """ReAct stream_done 后自检：checking / refining + 修正版第二段流式续接"""

    def _ctx(self):
        return chat_route.ChatRouteContext(
            agent_model=None, user_id="u1", user_message="请详细回答这个问题",
            system_prompt="sys", compressed_messages=[], db_session_factory=None,
        )

    @pytest.mark.asyncio
    async def test_pass_keeps_single_response(self, monkeypatch, no_metrics):
        monkeypatch.setattr(chat_route, "get_reflection_config", lambda: _react_l1_cfg())
        ctx = self._ctx()
        ctx.plan_model = StubModel(invoke_results=[CRITIQUE_PASS])
        stub = _AgentStub([[{"type": "response", "content": LONG_DRAFT},
                            {"type": "stream_done", "full_response": LONG_DRAFT}]])
        monkeypatch.setattr(chat_route, "execute_agent", stub)

        events = await _collect(chat_route.react_events(ctx, timeout=5))

        stages = [e["stage"] for e in events if e.get("type") == "reflection"]
        assert stages == ["checking"]  # 一次通过，无 refining
        assert ctx.plan_model.invoke_calls == 1

    @pytest.mark.asyncio
    async def test_fail_appends_refined_stream(self, monkeypatch, no_metrics):
        # 草稿已流出 → 批判失败 → refining + 修正版第二段流式续接（Part B 决策 5 原案）
        monkeypatch.setattr(chat_route, "get_reflection_config", lambda: _react_l1_cfg())
        ctx = self._ctx()
        ctx.plan_model = StubModel(invoke_results=[CRITIQUE_FAIL])
        ctx.agent_model = StubModel(stream_chunks=["修正", "版"])
        stub = _AgentStub([[{"type": "response", "content": LONG_DRAFT},
                            {"type": "stream_done", "full_response": LONG_DRAFT}]])
        monkeypatch.setattr(chat_route, "execute_agent", stub)

        events = await _collect(chat_route.react_events(ctx, timeout=5))

        stages = [e["stage"] for e in events if e.get("type") == "reflection"]
        assert stages == ["checking", "refining"]
        responses = [e for e in events if e["type"] == "response"]
        # 第一段草稿 + 第二段修正版（续接）
        assert responses[0]["content"] == LONG_DRAFT
        assert [r["content"] for r in responses[1:]] == ["修正", "版"]

    @pytest.mark.asyncio
    async def test_short_answer_skipped(self, monkeypatch, no_metrics):
        monkeypatch.setattr(chat_route, "get_reflection_config", lambda: _react_l1_cfg())
        ctx = self._ctx()
        ctx.plan_model = StubModel(invoke_results=[CRITIQUE_PASS])
        stub = _AgentStub([[{"type": "response", "content": "短"},
                            {"type": "stream_done", "full_response": "短"}]])
        monkeypatch.setattr(chat_route, "execute_agent", stub)

        events = await _collect(chat_route.react_events(ctx, timeout=5))

        assert "reflection" not in [e.get("type") for e in events]
        assert ctx.plan_model.invoke_calls == 0  # 零额外调用

    @pytest.mark.asyncio
    async def test_disabled_skips_l1(self, monkeypatch, no_metrics):
        monkeypatch.setattr(chat_route, "get_reflection_config", lambda: _react_l1_cfg(enable=False))
        ctx = self._ctx()
        ctx.plan_model = StubModel(invoke_results=[CRITIQUE_PASS])
        stub = _AgentStub([[{"type": "response", "content": LONG_DRAFT},
                            {"type": "stream_done", "full_response": LONG_DRAFT}]])
        monkeypatch.setattr(chat_route, "execute_agent", stub)

        events = await _collect(chat_route.react_events(ctx, timeout=5))

        assert "reflection" not in [e.get("type") for e in events]
        assert ctx.plan_model.invoke_calls == 0

    @pytest.mark.asyncio
    async def test_no_plan_model_skips_l1(self, monkeypatch, no_metrics):
        monkeypatch.setattr(chat_route, "get_reflection_config", lambda: _react_l1_cfg())
        ctx = self._ctx()
        ctx.plan_model = None  # plan_model 不可用（如模型初始化失败）
        stub = _AgentStub([[{"type": "response", "content": LONG_DRAFT},
                            {"type": "stream_done", "full_response": LONG_DRAFT}]])
        monkeypatch.setattr(chat_route, "execute_agent", stub)

        events = await _collect(chat_route.react_events(ctx, timeout=5))

        assert "reflection" not in [e.get("type") for e in events]  # 不崩、跳过

    @pytest.mark.asyncio
    async def test_l1_after_l2_repair_success(self, monkeypatch, no_metrics):
        # L2 修复成功后仍进入 L1 自检（两机制协同）
        monkeypatch.setattr(chat_route, "get_reflection_config", lambda: _react_l1_cfg())
        ctx = self._ctx()
        ctx.plan_model = StubModel(invoke_results=[CRITIQUE_PASS])
        stub = _AgentStub([
            [{"type": "error", "content": "第一次失败"}],
            [{"type": "response", "content": LONG_DRAFT},
             {"type": "stream_done", "full_response": LONG_DRAFT}],
        ])
        monkeypatch.setattr(chat_route, "execute_agent", stub)

        events = await _collect(chat_route.react_events(ctx, timeout=5))

        stages = [e["stage"] for e in events if e.get("type") == "reflection"]
        assert stages == ["repairing", "checking"]  # L2 修复 → L1 自检


# ========== P2 增强 1：指标落盘 + 统计报表 ==========

class TestRecordReflectionMetric:
    """record_reflection_metric：JSONL 追加 + 失败静默"""

    def test_record_writes_jsonl(self, monkeypatch, tmp_path):
        target = tmp_path / "metrics.jsonl"
        monkeypatch.setattr(reflection_metrics, "METRICS_FILE", target)
        reflection_metrics.record_reflection_metric({"task": "react", "triggered": True})
        reflection_metrics.record_reflection_metric({"task": "plan_step", "l2_repair_success": True})

        rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 2
        assert rows[0]["task"] == "react" and rows[0]["triggered"] is True
        assert "ts" in rows[0]  # 自动附加时间戳

    def test_record_failure_is_silent(self, monkeypatch):
        # 非法路径（如不存在的盘符）→ 只告警不抛异常
        monkeypatch.setattr(reflection_metrics, "METRICS_FILE", Path("Z:/nonexistent/x.jsonl"))
        reflection_metrics.record_reflection_metric({})  # 不应抛异常


class TestStatsReport:
    """scripts/reflection_stats.py 的 load_metrics / report（动态加载，scripts 无 __init__）"""

    @staticmethod
    def _load():
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "reflection_stats",
            str(Path(__file__).resolve().parent.parent / "scripts" / "reflection_stats.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_report_counts(self, tmp_path):
        stats = self._load()
        f = tmp_path / "m.jsonl"
        f.write_text(
            "\n".join([
                json.dumps({"ts": "2026-08-17T03:00:00", "task": "react", "triggered": True,
                            "refine_rounds": 0, "final_passed": True, "critique_latency_ms": [3200]},
                           ensure_ascii=False),
                json.dumps({"ts": "2026-08-17T04:00:00", "task": "plan_execute", "triggered": True,
                            "refine_rounds": 1, "final_passed": True, "critique_latency_ms": [3000, 2900]},
                           ensure_ascii=False),
                json.dumps({"ts": "2026-08-17T05:00:00", "task": "react", "l2_repair_triggered": True,
                            "l2_repair_success": True},
                           ensure_ascii=False),
            ]),
            encoding="utf-8",
        )
        rows = stats.load_metrics(f)
        out = stats.report(rows)

        assert "触发批判: 2" in out and "100.0%" in out  # 触发率 2/2
        assert "发生修正: 1" in out                        # 修正率 1/2 = 50.0%
        assert "修复成功: 1" in out                        # L2 成功率 1/1

    def test_report_empty(self, tmp_path):
        stats = self._load()
        out = stats.report([])
        assert "总记录数: 0" in out
