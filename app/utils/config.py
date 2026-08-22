"""
YAML 配置加载工具

从 config/ 目录加载 YAML 配置文件，提供统一的配置访问接口。
支持模块级缓存，避免每次请求重复读取磁盘。
"""

import os
from pathlib import Path
from typing import Any, Optional

import yaml

from app.core.logger_handler import logger

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 配置目录
CONFIG_DIR = BASE_DIR / "config"

# 配置缓存：{filename: data_dict}
_yaml_cache: dict = {}


def load_yaml(filename: str, use_cache: bool = True) -> dict:
    """
    加载指定的 YAML 配置文件
    
    首次加载后缓存结果，后续调用直接返回缓存（除非 use_cache=False）。
    
    Args:
        filename: 配置文件名（如 "chroma.yaml"）
        use_cache: 是否使用缓存（默认 True）
        
    Returns:
        解析后的字典
        
    Raises:
        FileNotFoundError: 配置文件不存在
    """
    if use_cache and filename in _yaml_cache:
        return _yaml_cache[filename]
    
    filepath = CONFIG_DIR / filename
    
    if not filepath.exists():
        logger.error(f"配置文件不存在: {filepath}")
        raise FileNotFoundError(f"配置文件不存在: {filepath}")
    
    with open(filepath, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    
    logger.debug(f"加载配置文件: {filename}")
    result = data or {}
    
    if use_cache:
        _yaml_cache[filename] = result
    
    return result


def reload_yaml(filename: str) -> dict:
    """
    强制重新加载配置文件（清除缓存）
    
    Args:
        filename: 配置文件名
        
    Returns:
        重新加载后的配置字典
    """
    _yaml_cache.pop(filename, None)
    return load_yaml(filename, use_cache=False)


def get_chroma_config() -> dict:
    """
    获取 ChromaDB 配置
    
    Returns:
        chroma.yaml 中的配置字典
    """
    return load_yaml("chroma.yaml").get("chroma", {})


def get_prompt_config() -> dict:
    """
    获取 Prompt 路径映射配置
    
    Returns:
        prompt.yaml 中的配置字典
    """
    return load_yaml("prompt.yaml").get("prompts", {})


def get_agent_config() -> dict:
    """
    获取 Agent 配置
    
    Returns:
        agent.yaml 中的 agent 配置字典
    """
    return load_yaml("agent.yaml").get("agent", {})


def get_rag_config() -> dict:
    """
    获取 RAG 配置
    
    优先读取环境变量 RAG_ENABLE_SUMMARIZE 覆盖 YAML 配置。
    
    Returns:
        agent.yaml 中的 rag 配置字典
    """
    rag_config = load_yaml("agent.yaml").get("rag", {})
    # 环境变量覆盖
    env_summarize = os.getenv("RAG_ENABLE_SUMMARIZE")
    if env_summarize is not None:
        rag_config["enable_summarize"] = env_summarize.lower() in ("true", "1", "yes")
    return rag_config


def get_token_budget_config() -> dict:
    """
    获取 Token 预算配置
    
    Returns:
        agent.yaml 中的 token_budget 配置字典
    """
    return load_yaml("agent.yaml").get("token_budget", {})


def get_memory_compression_config() -> dict:
    """
    获取记忆压缩配置
    
    Returns:
        agent.yaml 中的 memory_compression 配置字典
    """
    return load_yaml("agent.yaml").get("memory_compression", {})


def get_plan_execute_config() -> dict:
    """
    获取 Plan-and-Execute 配置
    
    Returns:
        agent.yaml 中的 plan_execute 配置字典
    """
    return load_yaml("agent.yaml").get("plan_execute", {})


def get_classifier_config() -> dict:
    """
    获取查询分类器配置

    Returns:
        agent.yaml 中的 classifier 配置字典
    """
    return load_yaml("agent.yaml").get("classifier", {})


def get_reflection_config() -> dict:
    """
    获取 Reflection 反思机制配置（L1 综合后自检）

    Returns:
        agent.yaml 中的 reflection 配置字典
        例: {"enable": true, "critic_model": "light", "critic_timeout": 15,
             "max_refine_rounds": 1, "trigger": {"min_answer_chars": 500}}
    """
    return load_yaml("agent.yaml").get("reflection", {})


def get_tool_groups_config() -> dict:
    """
    获取工具分组定义

    Returns:
        agent.yaml 中的 tool_groups 配置字典
        例: {"base": ["what_time_is_now", ...], "note_read": [...]}
    """
    return load_yaml("agent.yaml").get("tool_groups", {})


def get_tool_routing_config() -> dict:
    """
    获取工具路由规则（关键词 → 工具组映射）

    Returns:
        agent.yaml 中的 tool_routing 配置字典
        例: {"default_groups": [...], "keyword_rules": {"note_write": [...]}}
    """
    return load_yaml("agent.yaml").get("tool_routing", {})


def get_mcp_servers_config() -> dict:
    """
    获取 MCP Server 配置（agent.yaml mcp_servers 段，MCP 专项方案 Part A）

    Returns:
        MCP 服务器配置字典，例:
        {"tavily": {"transport": "stdio", "command": "npx", "args": [...],
                    "env": {...}, "tools_include": [...], "enabled": true}}
    """
    return load_yaml("agent.yaml").get("mcp_servers", {})


def get_pricing_config() -> dict:
    """
    获取模型定价配置（config/pricing.yaml）

    Returns:
        定价配置字典，例: {"models": {"qwen3.8-max": {"input_price_per_1k": 0.02, ...}}}
    """
    return load_yaml("pricing.yaml")


def get_security_config() -> dict:
    """
    获取安全配置（agent.yaml security 段，P2-3）

    Returns:
        agent.yaml 中的 security 配置字典
    """
    return load_yaml("agent.yaml").get("security", {})


def get_ppt_config() -> dict:
    """
    获取 PPT 功能配置（config/ppt.yaml 的 ppt 段，设计方案 §8.2）

    Returns:
        ppt.yaml 中的 ppt 配置字典
        例: {"max_notes": 10, "max_slides": 20, "themes": {...}}
    """
    return load_yaml("ppt.yaml").get("ppt", {})
