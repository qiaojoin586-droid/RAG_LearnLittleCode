"""Validate optional search expressions before dispatching any extra retrieval."""
import json
import re
import unicodedata
from typing import Protocol
from app.rag.ranking import FUSION_POLICY


POLICY_REVISION = 'adaptive-lexical-v1'
MAX_QUERIES = 2
MAX_CHARS = 512
MAX_OUTPUT_TOKENS = 384
GENERATION_SECONDS = FUSION_POLICY.generation_seconds


class ExpansionGenerator(Protocol):
    async def __call__(self, question: str, *, max_output_tokens: int) -> str: ...


def published_generator() -> ExpansionGenerator | None:
    """Resolve only the expansion role from the current request's immutable release."""
    from app.model_config.snapshots import current_snapshot
    from app.model_config.runtime import model_for, template_for
    from langchain_core.messages import HumanMessage

    role = 'rag_query_expand'
    snapshot = current_snapshot()
    if snapshot is None or snapshot.manifest.get('schema_version') != 3:
        return None
    if not snapshot.manifest['role_bindings_raw'].get(role, {}).get('enabled'):
        return None

    async def generate(question: str, *, max_output_tokens: int) -> str:
        model = model_for(role)
        message = template_for(role, '', query=question)
        reply = await model.ainvoke([HumanMessage(content=message)], max_output_tokens=max_output_tokens)
        return reply.content

    return generate

# Only paraphrase known equivalents. Unmatched words, operators and clause
# order remain literal, so a number or negation cannot migrate to another item.
_EQUIVALENTS = (
    ('如何', '怎样', '怎么'), ('配置', '设置'), ('排查', '诊断'),
    ('连接测试', '连通性测试'), ('对比', '比较'), ('差异', '区别'),
    ('优点', '优势'), ('缺点', '劣势'), ('注意事项', '注意点'),
    ('配置方法', '设置方法'), ('安装步骤', '安装流程'), ('维护方法', '维护方式'),
    ('故障排查', '故障诊断'), ('优缺点', '优势和劣势'),
    ('troubleshoot', 'diagnose'), ('compare', 'contrast'), ('configure', 'set up'),
)
_ALIASES = {term: group[0] for group in _EQUIVALENTS for term in group}
_TERMS = '|'.join(re.escape(s) for s in sorted(_ALIASES, key=len, reverse=True) if not s.isascii())
_QUOTED = r'''[\x60][^\x60]*[\x60]|"[^"]*"|'[^']*'|“[^”]*”|‘[^’]*’|「[^」]*」'''
_TOKEN = re.compile(
    _QUOTED + r'|(?<![A-Za-z0-9_])set up(?![A-Za-z0-9_])|'
    r'[A-Za-z0-9_]+(?:[./:+-][A-Za-z0-9_]+)*|' + _TERMS + r'|\s+|[^\s]'
)
_TRIGGER = re.compile(r'以及|并且|同时|分别|区别|差异|比较|对比|又称|也叫|或者|和|与|并|'
                      r'\b(and|versus|vs|compare|contrast|also called|also known as)\b', re.I)
_STRUCTURED = re.compile(r'<\s*/?\s*[A-Za-z][\w:-]*(?:\s|/?>)|以下是用户引用的笔记|'
                         r'://|(?:^|\s)(?:www\.|file:|data:)|[{}\[\]]|'
                         r'\b(?:user_id|owner_id|tenant_id|acl|filters?|tools?|knowledge_base_id)\s*[:=]', re.I)
# A shared subject with two independent, named aspects can be searched one
# aspect at a time. Conditions elsewhere in the sentence preclude this form.
_ASPECT = r'安装步骤|安装流程|配置方法|设置方法|连接测试|连通性测试|故障排查|故障诊断|维护方法|维护方式|优点|优势|缺点|劣势|注意事项|注意点|适用场景'
_ASPECT_PAIR = re.compile(rf'(?P<subject>[^，,。；;？！?!]+的)(?P<a>{_ASPECT})(?:和|与|以及)(?P<b>{_ASPECT})(?:有哪些|是什么)?[？?。]?$')
_IDENTIFIER = re.compile(r'[A-Za-z0-9_]+(?:[./:+-][A-Za-z0-9_]+)*|' + _QUOTED)
_OPAQUE = re.compile(_QUOTED + r'|[A-Za-z0-9_]+(?:[./:+-][A-Za-z0-9_]+)+')
_TOPIC = re.compile(r'(?:[A-Za-z][A-Za-z0-9_.+-]{0,63}|[\u4e00-\u9fff]{1,18}(?:泵|阀|电机|模块|设备|系统|服务|接口|数据库))的')
_QUALIFIED = re.compile(r'不|未|无|非|仅|只|除|禁|限|如果|当|须|应|以前|以后|之前|之后|比较|对比|分析|考虑|和|与|并|或')


class InvalidExpansion(ValueError):
    """A closed reason code, suitable for diagnostics without model text."""


def _plain(text):
    return (not _STRUCTURED.search(text)
            and all(c.isprintable() or c == ' ' for c in text))


def skip_reason(question):
    if not question.strip() or len(question) > MAX_CHARS or not _plain(question):
        return 'input_ineligible'
    if _IDENTIFIER.fullmatch(question.strip().rstrip('？?。')) or not _TRIGGER.search(_OPAQUE.sub(' ', question)):
        return 'no_adaptive_trigger'
    return None


def _signature(text):
    tokens = _TOKEN.findall(text.strip().rstrip('？?。'))
    return tuple(' ' if token.isspace() else _ALIASES.get(token, token) for token in tokens)


def _unique_object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise InvalidExpansion('invalid_output')
    return obj


def _identity(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split()).rstrip('?.。')


def validated_queries(question: str, reply: str) -> list[str]:
    # Bound parsing even when the transport returns malformed or escaped JSON.
    if not isinstance(reply, str) or len(reply) > 16384:
        raise InvalidExpansion('invalid_output')
    try:
        obj = json.loads(reply, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise InvalidExpansion('invalid_output') from None
    if not isinstance(obj, dict) or obj.keys() != {'queries'} or not isinstance(obj['queries'], list):
        raise InvalidExpansion('invalid_output')
    if len(obj['queries']) > MAX_QUERIES:
        raise InvalidExpansion('too_many_queries')
    allowed = {_signature(question)}
    aspects = _ASPECT_PAIR.fullmatch(question.strip())
    if aspects and _TOPIC.fullmatch(aspects['subject']) and not _QUALIFIED.search(aspects['subject']):
        allowed.update(_signature(aspects['subject'] + aspects[name]) for name in ('a', 'b'))
    seen, queries = {_identity(question)}, []
    for item in obj['queries']:
        if not isinstance(item, str) or len(item) > MAX_CHARS:
            raise InvalidExpansion('invalid_output')
        if not item.strip():
            continue
        if not _plain(item):
            raise InvalidExpansion('invalid_output')
        item = item.strip()
        if _signature(item) not in allowed:
            raise InvalidExpansion('constraint_changed')
        key = _identity(item)
        if key not in seen:
            seen.add(key)
            queries.append(item)
    if not queries:
        raise InvalidExpansion('no_distinct_query')
    return queries
