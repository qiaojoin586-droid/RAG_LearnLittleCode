"""Normalize legacy text/JSON/ToolMessage results for events and audit decisions."""
from __future__ import annotations

from contextvars import ContextVar
import json
from pathlib import Path
import re
from dataclasses import dataclass
from typing import Any

ARTIFACT_TOOLS = {'generate_ppt_tool': 'ppt', 'text_to_speech': 'tts'}
artifact_output_id: ContextVar[str | None] = ContextVar('artifact_output_id', default=None)


def artifact_file_event(name, reference, operation_id):
    route = ARTIFACT_TOOLS.get(name) if isinstance(name, str) else None
    file_id = reference.get('file_id') if isinstance(reference, dict) else None
    if (route is None or not isinstance(file_id, str) or not re.fullmatch('[a-f0-9]{32}', file_id)
            or not isinstance(operation_id, str) or not re.fullmatch('[a-f0-9-]{36}', operation_id)):
        return None
    url_key = 'download_url' if route == 'ppt' else 'audio_url'
    event = {'type': 'tool_file', 'name': name, 'file_id': file_id, 'operation_id': operation_id,
             url_key: f'/api/v1/{route}/{file_id}'}
    if type(reference.get('slide_count')) is int and 0 < reference['slide_count'] <= 1000:
        event['slide_count'] = reference['slide_count']
    return event


def artifact_result(name, reference, operation_id):
    event = artifact_file_event(name, reference, operation_id)
    message = '文件已生成，可通过下载卡片查看。' if event is not None else '文件生成结果待核对，未继续发起生成。'
    return f'{name}：{message}', {'operation_id': operation_id, 'file': event, 'replayed': True}


def find_completed_artifact(name, user_id, operation_id):
    """Check only the owner-scoped paths assigned before dispatch."""
    file_id = operation_id.replace('-', '')
    if not re.fullmatch('[a-f0-9]{32}', file_id):
        return None
    try:
        if name == 'text_to_speech':
            from app.routers.tts_router import TTS_FILE_ROOT

            path = Path(TTS_FILE_ROOT) / user_id / f'{file_id}.mp3'
            return {'file_id': file_id} if path.stat().st_size > 0 else None
        from app.services.ppt_service import PPT_FILE_ROOT

        directory = Path(PPT_FILE_ROOT) / user_id
        meta = json.loads((directory / f'{file_id}.json').read_text(encoding='utf-8'))
        size = (directory / f'{file_id}.pptx').stat().st_size
        if size > 0 and meta.get('size') == size:
            return {'file_id': file_id, 'slide_count': meta.get('slide_count')}
    except (OSError, ValueError, AttributeError):
        return None
    return None


@dataclass(frozen=True)
class ToolOutcome:
    success: bool
    error_code: str | None = None


_FAILURE = re.compile(
    r"^(?:拒绝|无权|未授权|未提供|参数错误|错误[：:]|Error[：:]|"
    r"(?:邮件发送|笔记创建|笔记更新|创建笔记|更新笔记|PPT 生成|语音生成|翻译|计算)失败|"
    r"邮件发送过于频繁|未找到笔记或无权访问|Wolfram Alpha 无法理解查询|"
    r"当前用户 ID:.+（用户信息获取失败）|"
    r"(?:.{0,24}服务|数据库会话|数据库|工具)(?:未初始化|未就绪|不可用|未配置)|"
    r"(?:文本内容|翻译结果)为空)", re.IGNORECASE,
)


def normalize_tool_result(output: Any) -> ToolOutcome:
    if getattr(output, "status", None) == "error":
        return ToolOutcome(False, "TOOL_REPORTED_ERROR")
    content = getattr(output, "content", output)
    if isinstance(content, list):
        content = "\n".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in content if isinstance(item, (str, dict))
        )
    if isinstance(content, str):
        text = content.strip()
        try:
            structured = json.loads(text)
        except (ValueError, TypeError):
            structured = None
        if isinstance(structured, dict):
            content = structured
        elif _FAILURE.match(text):
            return ToolOutcome(False, "TOOL_REPORTED_ERROR")
    if isinstance(content, dict):
        if (content.get("success") is False or content.get("status") in ("error", "failed")
                or bool(content.get("error"))):
            return ToolOutcome(False, "TOOL_REPORTED_ERROR")
        code = content.get("code")
        if isinstance(code, int) and code != 0:
            return ToolOutcome(False, "TOOL_REPORTED_ERROR")
    return ToolOutcome(True)


def text_content(content: Any) -> str:
    """Only display text blocks; tool/reasoning/image blocks are not response text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else item.get("text", "")
            for item in content if isinstance(item, str) or
            (isinstance(item, dict) and item.get("type") in {"text", "output_text"}
             and isinstance(item.get("text"), str))
        )
    return ""
