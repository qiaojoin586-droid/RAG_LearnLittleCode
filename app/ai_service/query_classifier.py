"""
查询复杂度分类器

两级分类架构：
- L1: 规则预判（关键词 + 模式匹配，<1ms，零成本）
- L2: LLM 精判（轻量 prompt，~200-500ms，~100-200 tokens）

L1 可确定 simple/complex，不确定时交给 L2 判断。
"""

import json
import re
from dataclasses import dataclass
from typing import List, Literal, Optional

from app.core.logger_handler import logger
from app.utils.config import get_classifier_config


@dataclass
class ClassificationResult:
    """分类结果"""
    complexity: Literal["simple", "complex"]
    source: Literal["rule", "llm"]       # 分类来源
    reason: str                           # 判断理由
    confidence: float = 1.0              # 置信度 (规则=1.0, LLM=0.8)


class QueryClassifier:
    """
    查询复杂度分类器

    L1 规则预判 → L2 LLM 精判的两级分类。
    """

    def __init__(self, llm_model=None):
        """
        Args:
            llm_model: L2 精判用的轻量 Chat 模型（可选，为 None 时仅用规则）
        """
        self.llm_model = llm_model
        config = get_classifier_config()

        # 从配置加载规则
        self.complex_patterns: List[str] = config.get("rule_complex_keywords", [])
        self.simple_patterns: List[str] = config.get("rule_simple_patterns", [])
        self.complex_min_length: int = config.get("rule_complex_min_length", 200)
        self.llm_enabled: bool = config.get("llm_enabled", True)
        # L1 补充规则（迁移自 _rule_classify 的硬编码，默认值与原代码一致）
        self.tool_keywords: List[str] = config.get(
            "tool_keywords", ["搜索", "搜索笔记", "统计", "回顾", "创建", "更新", "推荐", "标记"]
        )
        self.condition_patterns: List[str] = config.get(
            "condition_patterns", ["如果.*否则", "要么.*要么", "根据.*决定"]
        )
        self.tool_intent_keywords: List[str] = config.get(
            "tool_intent_keywords", ["搜索", "统计", "回顾", "创建", "更新", "推荐", "标记", "时间"]
        )
        self.short_msg_length: int = config.get("short_msg_length", 50)

        # 预编译正则
        self._complex_res = [re.compile(p) for p in self.complex_patterns]
        self._simple_res = [re.compile(p) for p in self.simple_patterns]
        self._condition_res = [re.compile(p) for p in self.condition_patterns]

    async def classify(self, user_message: str) -> ClassificationResult:
        """
        分类用户消息的复杂度

        Args:
            user_message: 用户消息文本

        Returns:
            ClassificationResult
        """
        # 1. L1 规则预判
        result = self._rule_classify(user_message)
        if result.complexity != "uncertain":
            logger.debug(
                f"查询分类 [L1 规则]: complexity={result.complexity}, "
                f"reason={result.reason}"
            )
            return result

        # 2. L2 LLM 精判
        if self.llm_enabled and self.llm_model:
            try:
                llm_result = await self._llm_classify(user_message)
                logger.debug(
                    f"查询分类 [L2 LLM]: complexity={llm_result.complexity}, "
                    f"reason={llm_result.reason}, confidence={llm_result.confidence}"
                )
                return llm_result
            except Exception as e:
                logger.warning(f"L2 LLM 分类失败，降级为 simple: {e}")
                return ClassificationResult("simple", "rule", f"llm_fallback: {e}", 0.5)

        # 3. 无 LLM 可用时默认简单
        logger.debug("查询分类: 无 LLM 可用，默认 simple")
        return ClassificationResult("simple", "rule", "no_llm_default", 0.5)

    def _rule_classify(self, msg: str) -> ClassificationResult:
        """
        L1 规则预判

        Returns:
            ClassificationResult（complexity 可能为 "simple"/"complex"/"uncertain"）
        """
        # --- 判定为"复杂" ---

        # 复杂关键词/模式匹配
        for pattern in self._complex_res:
            if pattern.search(msg):
                return ClassificationResult(
                    "complex", "rule",
                    f"匹配复杂模式: {pattern.pattern}", 1.0
                )

        # 长文本 + 多问号
        question_marks = msg.count("？") + msg.count("?")
        if len(msg) > self.complex_min_length and question_marks >= 2:
            return ClassificationResult(
                "complex", "rule",
                f"长文本({len(msg)}字符)+{question_marks}个问号", 1.0
            )

        # 多目标并列（检测 3+ 个工具意图关键词）
        matched_tools = sum(1 for kw in self.tool_keywords if kw in msg)
        if matched_tools >= 3:
            return ClassificationResult(
                "complex", "rule",
                f"多目标并列: 匹配{matched_tools}个工具意图", 1.0
            )

        # 条件分支
        for p in self._condition_res:
            if p.search(msg):
                return ClassificationResult(
                    "complex", "rule",
                    f"条件分支: 匹配 {p.pattern}", 1.0
                )

        # --- 判定为"简单" ---

        # 简单模式匹配
        for pattern in self._simple_res:
            if pattern.search(msg):
                return ClassificationResult(
                    "simple", "rule",
                    f"匹配简单模式: {pattern.pattern}", 1.0
                )

        # 短消息 + 无工具意图
        has_tool_intent = any(kw in msg for kw in self.tool_intent_keywords)
        if len(msg) < self.short_msg_length and not has_tool_intent:
            return ClassificationResult(
                "simple", "rule",
                f"短消息({len(msg)}字符)无工具意图", 1.0
            )

        # 单步操作（只匹配 1 个工具意图）
        if has_tool_intent and matched_tools == 1:
            return ClassificationResult(
                "simple", "rule",
                "单步工具操作", 1.0
            )

        # 以上均不匹配 → 不确定
        return ClassificationResult("uncertain", "rule", "规则无法判定", 0.0)

    async def _llm_classify(self, msg: str) -> ClassificationResult:
        """
        L2 LLM 精判

        使用轻量模型判断复杂度，返回结构化结果。
        """
        from app.utils.prompt_loader import load_prompt

        prompt_template = load_prompt("classify_complexity")
        prompt = prompt_template.replace("{user_message}", msg[:500])

        from langchain_core.messages import HumanMessage
        response = await self.llm_model.ainvoke([HumanMessage(content=prompt)])
        content = response.content.strip()

        # 解析 JSON（兼容 markdown code block）
        content = re.sub(r"```json\s*", "", content)
        content = re.sub(r"```\s*", "", content)
        content = content.strip()

        try:
            data = json.loads(content)
            complexity = data.get("complexity", "simple")
            if complexity not in ("simple", "complex"):
                complexity = "simple"
            return ClassificationResult(
                complexity, "llm",
                data.get("reason", "LLM 判定"),
                0.8
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"L2 JSON 解析失败: {e}, raw={content[:200]}")
            return ClassificationResult("simple", "llm", f"parse_error: {e}", 0.5)
