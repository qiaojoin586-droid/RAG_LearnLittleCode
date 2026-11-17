"""
MCP 客户端生命周期管理（MCP 专项方案 Part A / 实施文档 §5）

职责：
- 启动阶段：读取 agent.yaml `mcp_servers` 配置，经 langchain-mcp-adapters 拉起各 MCP
  Server（stdio 子进程），工具白名单过滤后注册进 tool_registry（group="mcp"）
- 降级策略：单个 server 连接失败 → 跳过并记日志，Agent 主流程不受影响（实施文档 §7 风险 #5）
- 关闭阶段：langchain-mcp-adapters 0.3.x 的工具为无状态对象（每次调用自建 stdio 会话并
  自动回收），客户端无持久连接，close 仅作状态清理（为未来持久会话预留接口）

注意（0.3.x API 变更，实测 2026-08-08）：
- `load_servers()` 已移除，改为 `await client.get_tools(server_name=...)`
- 连接配置格式不变：{"transport": "stdio", "command": ..., "args": ..., "env": ...}
- 工具执行错误默认以 ToolMessage(status="error") 返回给模型（handle_tool_errors=True），
  由 Agent 自纠错而非中断运行
"""

import os
import asyncio
from functools import wraps
from typing import Dict, List

from app.ai_service.tool_registry import init_tool_registry, tool_registry
from app.core.logger_handler import logger
from app.utils.config import get_mcp_servers_config
from app.utils.ssrf_guard import validate_public_url


class MCPManager:
    """MCP 客户端生命周期管理（全局单例，仅启动期注册，运行期只读）"""

    def __init__(self):
        self._server_configs: Dict[str, dict] = {}   # server 名 -> 原始配置（含 tools_include 等自定义键）
        self._client = None                          # MultiServerMCPClient 实例（0.3.x 无持久连接）
        self._registered: List[str] = []             # 已注册的 MCP 工具名
        self._started = False

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        """
        启动所有启用的 MCP Server 并注册工具（任一 server 失败仅降级跳过，不抛异常）

        顺序保证：必须先 init_tool_registry() —— 若 MCP 工具先注册，
        register_tool 自动创建的 "mcp" 组会使 init_tool_registry 的幂等守卫
        提前返回，内置工具组（base/note_read 等）将永远不会注册。
        """
        servers_config = get_mcp_servers_config()
        enabled = {
            name: cfg for name, cfg in servers_config.items()
            if cfg.get("enabled", False)
        }
        if not enabled:
            logger.info("[mcp] 未启用任何 MCP Server，跳过初始化")
            return

        # 内置工具组必须先于 mcp 组注册（见 docstring 顺序说明）
        init_tool_registry()

        from langchain_mcp_adapters.client import MultiServerMCPClient

        # 连接配置必须只含 stdio 参数：MultiServerMCPClient 会将整个连接字典
        # 作为 **kwargs 传给 _create_stdio_session()，自定义键（tools_include/enabled）
        # 会导致 TypeError（实测 2026-08-08）。自定义键保存在 _server_configs 供白名单过滤。
        # 环境变量插值：config.py 用 yaml.safe_load 加载，不会展开 ${VAR}（实施文档 §5.4）
        stdio_keys = ("transport", "command", "args", "env", "cwd")
        clean_connections = {}
        for name, cfg in enabled.items():
            self._server_configs[name] = dict(cfg)
            conn = {k: v for k, v in cfg.items() if k in stdio_keys}
            env = conn.get("env") or {}
            expanded = {k: os.path.expandvars(str(v)) for k, v in env.items()}
            conn["env"] = expanded
            # 告警：需要 API Key 的 server 未配置时提示（tavily-mcp 会退化为 keyless 模式，
            # 功能可用但可能消耗匿名额度，运维上尽早发现）
            if name == "tavily" and not expanded.get("TAVILY_API_KEY", "").strip():
                logger.warning(
                    "[mcp] tavily 未配置 TAVILY_API_KEY，将运行在 keyless 模式"
                    "（搜索可用，但 crawl/map/research 不可用；建议在 .env 配置）"
                )
            clean_connections[name] = conn

        self._client = MultiServerMCPClient(clean_connections)
        for server_name in enabled:
            await self._load_server(server_name)
        self._started = True
        logger.info(f"[mcp] 初始化完成，已注册工具: {self._registered}")

    async def close(self) -> None:
        """
        关闭 MCP 客户端（幂等）

        0.3.x 工具每次调用自建 stdio 会话并自动回收，无持久连接需关闭；
        保留此方法作为生命周期接口，为未来持久会话方案预留。
        """
        self._client = None
        self._started = False
        logger.info("[mcp] 客户端已关闭")

    # ---------- 内部 ----------

    async def _load_server(self, server_name: str) -> None:
        """加载单个 MCP Server 的工具并注册（失败仅降级跳过，不影响其他 server）"""
        cfg = self._server_configs[server_name]
        include = set(cfg.get("tools_include") or [])
        try:
            tools = await self._client.get_tools(server_name=server_name)
        except Exception as e:
            logger.error(
                f"[mcp] server '{server_name}' 连接失败，跳过（Agent 主流程不受影响）: {e}"
            )
            return

        for tool in tools:
            name = getattr(tool, "name", "")
            if include and name not in include:
                logger.info(f"[mcp] {server_name}: 工具 '{name}' 不在白名单（tools_include）内，跳过")
                continue
            try:
                tool = _wrap_ssrf_guard(tool)
                tool_registry.register_tool(name, tool, group="mcp")
                self._registered.append(name)
                logger.info(f"[mcp] 工具注册: {name} ← server '{server_name}'")
            except ValueError as e:
                logger.warning(f"[mcp] {server_name}: 工具注册被拒绝（跳过）: {e}")


def _wrap_ssrf_guard(tool):
    """
    对 URL 抓取类 MCP 工具加 SSRF 地址守卫（安全审查 P0-7）

    通过 args_schema 判定工具是否接收 url 参数（fetch / tavily_extract /
    tavily_crawl / tavily_map 等），是则包装 _run/_arun：调用前校验 URL 只能
    指向公网 http/https 地址，私网/回环/云元数据地址直接拒绝。

    被拦截时返回错误文案而非抛异常：与 langchain-mcp-adapters 的
    handle_tool_errors=True 语义一致，LLM 以普通工具结果看到"拒绝原因"，
    可自纠错（提示用户更换合法外网 URL），不会中断 Agent 运行。
    """
    from app.utils.ssrf_guard import extract_url_args

    args_schema = getattr(tool, "args_schema", None)
    if isinstance(args_schema, dict):
        schema = args_schema
    elif hasattr(args_schema, "model_json_schema"):
        schema = args_schema.model_json_schema()
    else:
        schema = {}
    url_fields = set()

    def scan(node):
        if isinstance(node, dict):
            for name, definition in node.get("properties", {}).items():
                lowered = name.lower()
                if lowered in {"url", "urls"} or lowered.endswith(("_url", "_urls")):
                    url_fields.add(name)
                elif isinstance(definition, dict) and definition.get("format") in {"uri", "url"}:
                    url_fields.add(name)
            for item in node.values():
                scan(item)
        elif isinstance(node, list):
            for item in node:
                scan(item)

    scan(schema)
    if not url_fields:
        return tool  # 无 url 参数的工具（如 tavily_search）无需守卫

    original_run = getattr(tool, "_run", None)
    original_arun = getattr(tool, "_arun", None)

    def _guard_error(url, err: Exception) -> str:
        return (
            f"拒绝抓取不安全地址: {str(url)[:200]} — {err}。"
            "仅允许公网 http/https 地址，请提示用户更换为合法外网 URL。"
        )

    if original_run is not None:
        @wraps(original_run)
        def _guarded_run(*args, **kwargs):
            try:
                for url in extract_url_args(args, kwargs, url_fields):
                    validate_public_url(url)
            except ValueError as e:
                return _guard_error("", e)
            return original_run(*args, **kwargs)

        tool._run = _guarded_run

    if original_arun is not None:
        @wraps(original_arun)
        async def _guarded_arun(*args, **kwargs):
            try:
                for url in extract_url_args(args, kwargs, url_fields):
                    await asyncio.to_thread(validate_public_url, url)
            except ValueError as e:
                return _guard_error("", e)
            return await original_arun(*args, **kwargs)

        tool._arun = _guarded_arun
    logger.info(f"[mcp] 工具已加 SSRF 地址守卫: {getattr(tool, 'name', '')}")
    return tool


# 全局单例
mcp_manager = MCPManager()
