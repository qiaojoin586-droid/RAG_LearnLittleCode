"""Failure injection for admission, planning, cancellation and tool selection (M0)."""
import asyncio
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import SystemMessage

from app.ai_service import chat_graph, plan_execute_agent as plans
from app.ai_service.agent_runner import AgentFactory
from app.ai_service.chat_route import ChatRouteContext
from app.ai_service.agent_tools import create_agent_tools
from app.core import task_runner
from app.core.model_trace import get_trace_context, set_trace_context, set_trace_stage, trace_scope


@pytest.mark.asyncio
async def test_react_chat_consumes_and_closes_model_stream(monkeypatch):
    closed = asyncio.Event()

    async def model_events(*args, **kwargs):
        try:
            yield {'event': 'on_chat_model_stream', 'data': {'chunk': SimpleNamespace(content='你好！')}}
        finally:
            closed.set()

    monkeypatch.setattr(AgentFactory, 'create_agent', lambda **kwargs: (SimpleNamespace(astream_events=model_events), 5))
    monkeypatch.setattr(chat_graph, '_compiled_graph', None)
    ctx = ChatRouteContext(None, 'user', '你好', '', [], None)
    async with asyncio.timeout(2):
        events = [event async for event in chat_graph.stream_chat_graph(ctx)]
    assert events == [{'type': 'response', 'content': '你好！'}]
    assert closed.is_set()


@pytest.mark.asyncio
async def test_rejected_coroutine_is_closed_and_graph_returns_instead_of_waiting(monkeypatch):
    monkeypatch.setattr(task_runner, "MAX_BACKGROUND_TASKS", 0)

    async def work():
        raise AssertionError("Rejected work must not start")

    coro = work()
    assert task_runner.spawn_background_task(coro) is None
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
    async with asyncio.timeout(1):
        events = [event async for event in chat_graph.stream_chat_graph(SimpleNamespace())]
    assert len(events) == 1
    assert events[0]["type"] == "error" and events[0]["retryable"] is True


@pytest.mark.asyncio
async def test_closing_full_graph_queue_cancels_and_joins_producer(monkeypatch):
    finished = asyncio.Event()

    async def run(state):
        try:
            for _ in range(2000):
                await state["event_queue"].put({"type": "response", "content": "x"})
        finally:
            finished.set()

    monkeypatch.setattr(chat_graph, "build_chat_graph", lambda: SimpleNamespace(ainvoke=run))
    stream = chat_graph.stream_chat_graph(SimpleNamespace())
    await anext(stream)
    async with asyncio.timeout(1):
        await stream.aclose()
    assert finished.is_set()


@pytest.mark.parametrize("steps", [
    [plans.PlanStep(1, "a", "none"), plans.PlanStep(1, "b", "none")],
    [plans.PlanStep(1, "a", "none", [2]), plans.PlanStep(2, "b", "none", [1])],
    [plans.PlanStep(1, "a", "none", [99])],
    [plans.PlanStep(1, "a", "none", [1])],
    [plans.PlanStep(True, "a", "none")],
    [plans.PlanStep(1, "", "none")],
    [plans.PlanStep(1, "a", "none", "1")],
    [plans.PlanStep(1, "a", "none", [2, 2]), plans.PlanStep(2, "b", "none")],
])
def test_invalid_dag_is_rejected(steps):
    with pytest.raises(ValueError):
        plans._topological_batches(steps)


def test_valid_dag_keeps_all_steps():
    steps = [plans.PlanStep(1, "a", "none"), plans.PlanStep(2, "b", "none"),
             plans.PlanStep(3, "c", "none", [1, 2])]
    assert [[step.step for step in batch] for batch in plans._topological_batches(steps)] == [[1, 2], [3]]


@pytest.mark.asyncio
async def test_oversize_generated_plan_is_not_silently_truncated(monkeypatch):
    from app.utils import prompt_loader
    monkeypatch.setattr(prompt_loader, "load_prompt", lambda key: "{user_message} {tool_list}")
    monkeypatch.setattr(plans, "get_plan_execute_config", lambda: {"max_steps": 2})
    response = {"steps": [{"step": i, "action": "a", "tool": "none"} for i in range(1, 4)]}
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(response))))
    with pytest.raises(ValueError, match="最大步骤"):
        await plans._generate_plan(model, "question", "policy")


@pytest.mark.asyncio
async def test_no_tool_step_receives_policy_dependencies_and_text_blocks():
    captured = []

    async def stream(messages):
        captured.extend(messages)
        assert get_trace_context()["step_id"] == "2"
        yield SimpleNamespace(content=[{"type": "text", "text": "result"}])

    step = plans.PlanStep(2, "Summarize", "none", [1])
    events = [event async for event in plans._execute_step(
        step, SimpleNamespace(astream=stream), "u", "question", "protected-policy", [],
        None, None, None, None, None, 1, {1: "prior-evidence"},
    )]
    assert isinstance(captured[0], SystemMessage)
    assert "protected-policy" in captured[0].content
    assert "prior-evidence" not in captured[0].content
    assert "prior-evidence" in captured[-1].content
    assert step.result == "result" and events[-1]["type"] == "plan_step_end"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,expected_peak", [("none", 2), ("update_note_tool", 1)])
async def test_write_batches_are_serial_but_reads_can_run_in_parallel(monkeypatch, tool, expected_peak):
    active, peak = 0, 0

    async def execute(step, *args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        try:
            yield {"type": "start"}
            await asyncio.sleep(0.01)
        finally:
            active -= 1

    monkeypatch.setattr(plans, "_execute_step", execute)
    steps = [plans.PlanStep(i, "a", tool) for i in (1, 2)]
    await _consume_batch(steps)
    assert peak == expected_peak and active == 0


async def _consume_batch(steps):
    return [event async for event in plans._execute_batch(
        steps, None, "u", "q", "p", [], None, None, None, None, None,
        {"enable_parallel": True, "max_parallel_steps": 2}, {},
    )]


@pytest.mark.asyncio
async def test_batch_close_awaits_all_child_cleanup(monkeypatch):
    active = set()
    writes = []

    async def execute(step, *args, **kwargs):
        active.add(step.step)
        try:
            yield {"type": "start"}
            await asyncio.sleep(10)
            writes.append(step.step)
        finally:
            active.remove(step.step)

    monkeypatch.setattr(plans, "_execute_step", execute)
    stream = plans._execute_batch(
        [plans.PlanStep(i, "a", "none") for i in (1, 2)], None, "u", "q", "p", [],
        None, None, None, None, None, {"enable_parallel": True}, {},
    )
    await anext(stream)
    await stream.aclose()
    assert active == set() and writes == []


def test_explicit_empty_unknown_and_unknown_planned_tools_do_not_grant_tools():
    assert create_agent_tools("u", groups=[]) == []
    assert create_agent_tools("u", groups=["unknown-group"]) == []
    with pytest.raises(ValueError, match="未知工具"):
        plans._resolve_step_tool_groups(plans.PlanStep(1, "a", "unknown-tool"))


@pytest.mark.asyncio
async def test_trace_stage_changes_do_not_mutate_siblings():
    set_trace_context("a" * 32, stage="parent")

    async def child(stage):
        with trace_scope(step_id=stage):
            set_trace_stage(stage)
            await asyncio.sleep(0)
            return dict(get_trace_context())

    first, second = await asyncio.gather(child("one"), child("two"))
    assert first["stage"] == first["step_id"] == "one"
    assert second["stage"] == second["step_id"] == "two"
    assert get_trace_context()["stage"] == "parent"


@pytest.mark.asyncio
async def test_closing_parent_plan_joins_live_steps_before_return(monkeypatch):
    active = set()
    plan = plans.ExecutionPlan("goal", [plans.PlanStep(i, "a", "none") for i in (1, 2)])
    monkeypatch.setattr(plans, "_generate_plan", AsyncMock(return_value=plan))
    monkeypatch.setattr(plans, "get_plan_execute_config", lambda: {"enable_parallel": True})
    monkeypatch.setattr(plans, "get_reflection_config", lambda: {"enable": False})

    async def execute(step, *args, **kwargs):
        active.add(step.step)
        try:
            yield {"type": "plan_step_start"}
            await asyncio.sleep(10)
        finally:
            active.remove(step.step)

    monkeypatch.setattr(plans, "_execute_step", execute)
    stream = plans.execute_plan_agent(None, None, "u", "q", "policy")
    async for event in stream:
        if event["type"] == "plan_step_start":
            break
    async with asyncio.timeout(1):
        await stream.aclose()
    assert active == set()
