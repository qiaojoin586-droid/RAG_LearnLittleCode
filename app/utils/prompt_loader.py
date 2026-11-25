"""
提示词模板加载工具

从 prompts/ 目录加载提示词模板文件。
通过 prompt.yaml 配置映射模板名称到文件路径。
"""

from pathlib import Path
from typing import Optional

from app.core.logger_handler import logger
from app.utils.config import BASE_DIR, get_prompt_config

# Prompts 目录
PROMPTS_DIR = BASE_DIR / "prompts"

# 模板缓存（避免重复读文件）
_prompt_cache: dict[str, str] = {}


def load_prompt(prompt_name: str) -> str:
    """
    加载指定名称的提示词模板
    
    通过 prompt.yaml 中的映射找到对应的 .txt 文件并读取内容。
    支持缓存，避免重复读文件。
    
    Args:
        prompt_name: 提示词名称（如 "main"、"rag_summarize"）
        
    Returns:
        提示词模板文本内容
        
    Raises:
        FileNotFoundError: 提示词文件不存在
        KeyError: prompt_name 未在 prompt.yaml 中配置
    """
    from app.model_config.snapshots import current_snapshot
    if current_snapshot() is not None:
        from app.model_config.registry import PROMPT_KEYS
        from app.model_config.runtime import template_for
        roles=[role for role,key in PROMPT_KEYS.items() if key==prompt_name]
        if roles:return template_for(roles[0],'')
    # 检查缓存
    if prompt_name in _prompt_cache:
        return _prompt_cache[prompt_name]
    
    # 从配置获取文件路径
    config = get_prompt_config()
    
    if prompt_name not in config:
        raise KeyError(f"提示词 '{prompt_name}' 未在 prompt.yaml 中配置")
    
    filepath = BASE_DIR / config[prompt_name]
    
    if not filepath.exists():
        raise FileNotFoundError(f"提示词文件不存在: {filepath}")
    
    # 读取文件
    content = filepath.read_text(encoding="utf-8")
    
    # 写入缓存
    _prompt_cache[prompt_name] = content
    
    logger.debug(f"加载提示词: {prompt_name} ({filepath})")
    return content


def format_prompt(prompt_name: str, **kwargs) -> str:
    """
    加载提示词模板并填充变量
    
    使用 Python str.format() 语法替换模板中的占位符。
    
    Args:
        prompt_name: 提示词名称
        **kwargs: 模板变量键值对
        
    Returns:
        填充变量后的提示词文本
        
    示例：
        format_prompt("auto_tag", note_title="Python笔记", note_content="...")
    """
    template = load_prompt(prompt_name)
    from app.model_config.snapshots import current_snapshot
    if current_snapshot() is not None:
        from app.model_config.registry import PROMPT_KEYS
        from app.model_config.prompt_contracts import render
        role=next(role for role,key in PROMPT_KEYS.items() if key==prompt_name)
        return render(role,template,kwargs)
    return template.format(**kwargs)


def clear_prompt_cache() -> None:
    """清空提示词缓存（开发调试时使用）"""
    _prompt_cache.clear()
