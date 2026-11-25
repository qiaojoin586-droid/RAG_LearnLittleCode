"""Exercise actual MCP tools, event streams, audit correlation and ASGI errors."""
from contextvars import ContextVar
import importlib.util
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel
import pytest
import sqlalchemy as sa

from app.ai_service import agent_middleware as audit
from app.ai_service.mcp_manager import _wrap_ssrf_guard
from app.ai_service.stream import run_agent_stream
from app.ai_service.tool_results import normalize_tool_result
from app.core.failed_response import BusinessError
from app.core.failed_response_register import register_exception_handlers
from app.core.model_trace import new_request_id
from app.core.success_response import success_response
from app.utils.ssrf_guard import is_blocked_ip
from app.model_config.permissions import PERMISSIONS, ROLE_PERMISSIONS, ROUTES, allows_config_action, required_config_permission


class UrlInput(BaseModel):
    url: str


@pytest.mark.asyncio
@pytest.mark.parametrize("schema", [UrlInput, {"type": "object", "properties": {
    "url": {"type": "string"}}, "required": ["url"]}])
async def test_real_mcp_tools_guard_pydantic_and_json_schema(schema):
    called = []

    async def fetch(**kwargs):
        called.append(kwargs)
        return "fetched"

    tool = _wrap_ssrf_guard(StructuredTool.from_function(
        coroutine=fetch, name="fetch", description="Fetch a URL", args_schema=schema,
    ))
    result = await tool.ainvoke({"url": "http://127.0.0.1/private"})
    assert "拒绝" in result and called == []
    assert await tool.ainvoke({"url": "https://8.8.8.8/public"}) == "fetched"
    assert len(called) == 1


@pytest.mark.asyncio
async def test_url_arrays_are_all_checked_before_any_tool_execution():
    called = []

    async def fetch(**kwargs):
        called.append(kwargs)
        return "fetched"

    schema = {"type": "object", "properties": {"urls": {
        "type": "array", "items": {"type": "string"}}}, "required": ["urls"]}
    tool = _wrap_ssrf_guard(StructuredTool.from_function(
        coroutine=fetch, name="batch_fetch", description="Fetch URLs", args_schema=schema,
    ))
    result = await tool.ainvoke({"urls": ["https://8.8.8.8", "http://169.254.169.254/latest"]})
    assert "拒绝" in result and called == []


@pytest.mark.parametrize("ip", ["::ffff:127.0.0.1", "::ffff:10.0.0.1", "2001:db8::1"])
def test_ipv4_mapped_and_reserved_addresses_are_blocked(ip):
    assert is_blocked_ip(ip)


@pytest.mark.parametrize("result,success", [
    ("邮件发送失败：无收件人", False), ("PPT 服务未初始化，请稍后再试", False),
    ("拒绝抓取不安全地址", False), ("未找到相关笔记", True),
    ("数据库未初始化", False), ("未找到笔记或无权访问", False),
    ("Wolfram Alpha 无法理解查询: x", False), ("当前用户 ID: u（用户信息获取失败）", False),
    ('{"success":false}', False), ({"status": "failed"}, False),
    ({"status": {"info": "unexpected"}}, True),
    (ToolMessage(content="no", tool_call_id="c", status="error"), False),
    (ToolMessage(content="success", tool_call_id="c"), True),
])
def test_tool_failures_are_not_reported_as_success(result, success):
    assert normalize_tool_result(result).success is success


@pytest.mark.asyncio
async def test_audit_pairs_same_name_tools_by_run_id_and_handles_queue_full(monkeypatch):
    monkeypatch.setattr(audit, "_tool_pending", ContextVar("test_pending", default=None))
    write = AsyncMock()
    monkeypatch.setattr(audit, "_write_audit", write)

    def reject(coro, **kwargs):
        coro.close()
        return None

    monkeypatch.setattr(audit, "spawn_background_task", reject)
    await audit._audit_before_tool({"run_id": "one", "tool_name": "same", "input": "params-one"})
    await audit._audit_before_tool({"run_id": "two", "tool_name": "same", "input": "params-two"})
    await audit._audit_after_tool({"run_id": "two", "tool_call_id": "call-two", "output": "result-two"})
    await audit._audit_after_tool({"run_id": "one", "tool_call_id": "call-one", "output": "result-one"})
    rows = [call.args[0] for call in write.await_args_list]
    assert [(row["run_id"], row["params_json"], row["tool_call_id"]) for row in rows] == [
        ("two", "params-two", "call-two"), ("one", "params-one", "call-one"),
    ]


@pytest.mark.asyncio
async def test_stream_propagates_business_tool_error_and_text_blocks():
    seen = []
    middleware = audit.AgentMiddleware()
    middleware.register_hook("after_tool", lambda context: seen.append(context))

    async def events(*args, **kwargs):
        yield {"event": "on_tool_start", "name": "send_email", "run_id": "one", "data": {"input": {}}}
        yield {"event": "on_tool_start", "name": "send_email", "run_id": "two", "data": {"input": {}}}
        for run, text in [("two", "邮件发送失败：错误"), ("one", "已发送")]:
            yield {"event": "on_tool_end", "name": "send_email", "run_id": run, "data": {
                "output": ToolMessage(content=text, tool_call_id="call-" + run),
            }}
        yield {"event": "on_chat_model_stream", "data": {"chunk": SimpleNamespace(content=[
            {"type": "text", "text": "answer"}, {"type": "reasoning", "text": "private"},
        ])}}

    result = [event async for event in run_agent_stream(SimpleNamespace(astream_events=events),
              {"messages": []}, {}, middleware=middleware)]
    assert seen[0]["success"] is False and seen[0]["run_id"] == "two"
    assert seen[1]["success"] is True and seen[1]["run_id"] == "one"
    assert next(event for event in result if event["type"] == "tool_end")["error"] is True
    assert result[-1]["full_response"] == "answer"


class Body(BaseModel):
    value: int


@pytest.mark.parametrize("path,status,code", [
    ("/ok", 200, 0), ("/business", 409, 40920), ("/limited", 429, 42902),
    ("/unexpected", 500, 50001), ("/body", 422, 40002),
])
def test_response_header_envelope_and_model_trace_share_request_id(path, status, code):
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/ok")
    def ok():
        return success_response({"trace_id": new_request_id()})

    @app.get("/business")
    def business():
        raise BusinessError(40920, http_status=409, data={"reason": "HEAD_VERSION_CONFLICT"})

    @app.get("/limited")
    def limited():
        raise HTTPException(429, detail={"code": 42902, "message": "limited"})

    @app.get("/unexpected")
    def unexpected():
        raise RuntimeError("must-not-leak-secret")

    @app.post("/body")
    def body(value: Body):
        return success_response(value.model_dump())

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(path, json={}) if path == "/body" else client.get(path)
    payload = response.json()
    assert response.status_code == status and payload["code"] == code
    assert len(payload["request_id"]) == 32
    assert payload["request_id"] == response.headers["x-request-id"]
    assert "must-not-leak-secret" not in response.text
    if path == "/ok":
        assert payload["data"]["trace_id"] == payload["request_id"]
    else:
        assert "data" in payload and "detail" in payload


def load_audit_migration():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/f1c2d3e4a5b6_tool_audit_correlation.py"
    spec = importlib.util.spec_from_file_location("audit_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_audit_incremental_migration_preserves_existing_rows_and_can_repeat(monkeypatch):
    migration = load_audit_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE tool_call_audit (id INTEGER PRIMARY KEY, tool_name VARCHAR(50))")
        connection.exec_driver_sql("INSERT INTO tool_call_audit VALUES (1, 'keep-me')")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
        migration.upgrade()
        migration.upgrade()
        assert set(migration.FIELDS).issubset({column["name"] for column in sa.inspect(connection).get_columns("tool_call_audit")})
        migration.downgrade()
        assert connection.exec_driver_sql("SELECT * FROM tool_call_audit").all() == [(1, "keep-me")]
        migration.upgrade()
    engine.dispose()


def test_audit_migration_generates_mysql_additive_sql(monkeypatch):
    migration = load_audit_migration()
    output = StringIO()
    context = MigrationContext.configure(dialect_name="mysql", opts={"as_sql": True, "output_buffer": output})
    monkeypatch.setattr(migration, "op", Operations(context))
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: True)
    migration.upgrade()
    assert output.getvalue().count("ALTER TABLE tool_call_audit ADD COLUMN") == 5


@pytest.mark.parametrize("role", ["config_reader", "config_operator", "config_admin", "unknown", None])
@pytest.mark.parametrize("permission", ["config.read", "config.edit", "config.publish", "grant.manage", "unknown"])
def test_configuration_policy_is_explicit_and_default_deny(role, permission):
    assert allows_config_action(role, permission) == (permission in ROLE_PERMISSIONS.get(role, set()))


def test_every_enabled_management_route_has_a_known_permission():
    assert len(ROUTES) == 39
    for route in ROUTES:
        path = route["path"].replace("{id}", "fixture").replace("{key}", "fixture").replace("{user_id}", "fixture")
        permission = required_config_permission(route["method"], path)
        assert permission == (route["permission"] if route["enabled"] else None)
        if route["enabled"]:
            assert permission in PERMISSIONS
    assert required_config_permission("POST", "/api/v1/model-config/unknown") is None
    catch_up = next(route for route in ROUTES if route['path'] == '/api/v1/model-config/releases/{id}/index-catch-up')
    assert catch_up['method'] == 'POST' and catch_up['permission'] == 'config.restore' and catch_up['reauth'] == 'required'
    assert not allows_config_action("config_admin", None)
