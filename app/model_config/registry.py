"""Stable role registry for configuration and binding compilation."""
from app.model_config.contracts import SCHEMA

ROLE_NAMES = {
    "chat": "主对话",
    "chat_thinking": "深度思考",
    "classifier": "查询分类",
    "planner": "计划生成",
    "vision": "视觉理解",
    "autocomplete": "联机补全",
    "ppt_outline": "PPT 大纲",
    "embedding": "文档与笔记向量",
    "session_title": "会话标题",
    "auto_tag": "自动标签",
    "write_assistant": "写作助手",
    "review_question": "回顾题",
    "memory_summary": "历史记忆摘要",
    "rag_summarize": "RAG 摘要",
    "synthesize": "结果综合",
    "reflection": "结果反思",
    "memory_extraction": "个人记忆提取",
    "rag_query_expand": "检索查询扩展",
}

PROMPT_KEYS = {
    "chat":"main","chat_thinking":"main","classifier":"classify_complexity","planner":"plan_generation",
    "vision":"main","autocomplete":"autocomplete","ppt_outline":"ppt_outline","embedding":None,
    "session_title":"session_title","auto_tag":"auto_tag","write_assistant":"write_assistant",
    "review_question":"review_question","memory_summary":"memory_summary","rag_summarize":"rag_summarize",
    "synthesize":"plan_synthesize","reflection":"reflection_critique","memory_extraction":"memory_extraction",
    "rag_query_expand":"rag_query_expand",
}


def role_views():
    from app.model_config.prompt_contracts import VARIABLES,default_template
    return [{"role_key":key,"display_name":ROLE_NAMES[key],"group":"basic" if index<8 else "advanced",
             "model_kind":"embedding" if key=="embedding" else "chat","prompt_key":PROMPT_KEYS[key],
             "template_variables":list(VARIABLES.get(key,())),"default_template":default_template(key)}
            for index,key in enumerate(SCHEMA['$defs']['RoleKeyV3']['enum'])]

