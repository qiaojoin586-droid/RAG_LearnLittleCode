"""
工具注册表（P2-6 工具注册动态化）

职责：
- 组定义：从 agent.yaml `tool_groups` 加载 5 个内置工具组
- 冲突检测：工具名全局唯一，重复注册直接报错
- 动态注册：MCP 等外部工具经 `register_tool` 注册后参与组解析
  （MCP 专项方案 Phase 1 的 mcp_manager 使用本注册表）
- 组装：按组名列表解析工具（内置工具实例 + 动态工具对象）

内置工具是按用户上下文创建的闭包（agent_tools.create_agent_tools 内部定义），
注册表只登记其名称与元数据，实例化仍在 agent_tools 内完成；
动态工具（MCP）为全局 Tool 对象/工厂，注册表直接持有。
"""

from typing import Callable, Dict, List, Optional

from app.core.logger_handler import logger



class ToolRegistry:
    """工具注册表（全局单例，线程安全要求低：仅启动期注册，运行期只读）"""

    def __init__(self):
        self._groups: Dict[str, List[str]] = {}        # 组名 -> 工具名（有序）
        self._builtin_meta: Dict[str, dict] = {}       # 内置工具名 -> 元数据（冲突检测基准）
        self._dynamic_tools: Dict[str, Callable] = {}  # 动态工具名 -> Tool 对象/工厂

    # ---------- 注册 ----------

    def register_group(self, group: str, tool_names: List[str]) -> None:
        """注册工具组（重复组名覆盖并告警）"""
        if group in self._groups:
            logger.warning(f"工具组重复注册: {group}，覆盖旧定义")
        self._groups[group] = list(tool_names)

    def register_builtin(self, name: str, meta: Optional[dict] = None) -> None:
        """登记内置工具名（冲突检测基准；重名报错）"""
        if name in self._builtin_meta or name in self._dynamic_tools:
            raise ValueError(f"工具名冲突: '{name}' 已注册（内置或动态）")
        self._builtin_meta[name] = meta or {}

    def register_tool(self, name: str, tool, group: Optional[str] = None) -> None:
        """
        动态注册工具（MCP 等外部来源）

        Args:
            name: 工具名（全局唯一）
            tool: Tool 对象或返回 Tool 的工厂
            group: 所属组（缺省挂到 "mcp" 组，组不存在时自动创建）

        Raises:
            ValueError: 工具名与已注册工具冲突（内置或动态）
        """
        if name in self._builtin_meta or name in self._dynamic_tools:
            raise ValueError(
                f"工具名冲突: '{name}' 已注册（内置或动态），拒绝动态注册"
            )
        group = group or "mcp"
        if group not in self._groups:
            logger.info(f"动态注册创建新工具组: {group}")
            self.register_group(group, [])
        self._dynamic_tools[name] = tool
        self._groups[group].append(name)
        logger.info(f"动态工具注册: {name} → 组 '{group}'")

    # ---------- 解析 ----------

    def resolve_names(self, groups: Optional[List[str]] = None) -> List[str]:
        """
        组名列表 -> 工具名列表（保持组内顺序、跨组去重）

        Args:
            groups: 组名列表；None 表示全部组

        Returns:
            工具名列表（未注册的组返回空贡献）
        """
        if groups is None:
            groups = list(self._groups.keys())
        seen: set = set()
        names: List[str] = []
        for g in groups:
            for name in self._groups.get(g, []):
                if name not in seen:
                    seen.add(name)
                    names.append(name)
        return names

    def is_registered(self, name: str) -> bool:
        """工具名是否已注册（内置或动态）"""
        return name in self._builtin_meta or name in self._dynamic_tools

    def get_dynamic(self, name: str):
        """获取动态工具对象（未注册返回 None）"""
        return self._dynamic_tools.get(name)

    def all_groups(self) -> List[str]:
        return list(self._groups.keys())

    def builtin_names(self) -> List[str]:
        return list(self._builtin_meta.keys())


# 全局单例
tool_registry = ToolRegistry()


def init_tool_registry() -> None:
    """
    从 agent.yaml `tool_groups` 初始化内置工具组（幂等，仅首次生效）

    动态工具（MCP）由 MCP 专项方案的 mcp_manager 在启动阶段经 register_tool 注册，
    注册表同时承担冲突检测与分组解析。
    """
    if tool_registry._groups:
        return  # 已初始化

    from app.utils.config import get_tool_groups_config

    groups = get_tool_groups_config()
    for group, names in groups.items():
        tool_registry.register_group(group, names)
        for name in names:
            if not tool_registry.is_registered(name):
                tool_registry.register_builtin(name, {"group": group})
    # 无条件注册空 mcp 组：MCP server 全部未启用/连接失败时，消除
    # create_agent_tools 每轮的 "工具组 'mcp' 未在 agent.yaml 中定义" warning
    # （实施文档 §5.4 日志噪音消除方案；MCP 工具注册时 register_tool 会直接挂到此组）
    if "mcp" not in tool_registry.all_groups():
        tool_registry.register_group("mcp", [])
    logger.info(
        f"工具注册表初始化完成: {len(tool_registry._groups)} 组 / "
        f"{len(tool_registry._builtin_meta)} 个内置工具"
    )
