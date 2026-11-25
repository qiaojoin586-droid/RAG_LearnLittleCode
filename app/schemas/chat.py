"""
聊天相关 Pydantic Schema

定义 Agent 对话、RAG 查询、会话管理等接口的请求和响应模型。
"""

from datetime import datetime
from typing import Literal, Optional, List, TypedDict

from pydantic import BaseModel, Field, field_serializer, model_validator

from app.utils.time_utils import to_utc_iso


class QueryRequest(BaseModel):
    """
    Agent 对话请求

    ⚠️ Breaking change：message 由必填改为可选（允许仅发附件），
    但必须同时满足：message 为空时 attachment_ids 非空（由 _require_message_or_attachment 兜底）。
    """
    session_id: Optional[str] = Field(None, description="会话 ID（为空则创建新会话）")
    message: str = Field("", max_length=10000, description="用户消息（可空，仅发附件时）")
    idempotency_key: Optional[str] = Field(None, description="幂等键（防重复提交）")
    enable_thinking: bool = Field(False, description="是否开启深度思考（思考模式，质量更高但响应更慢）")
    continue_on_disconnect: bool = Field(False, description="允许对话在原连接断开后继续")
    allow_background_actions: bool = Field(False, description="允许后台创建待确认操作并生成文件")
    memory_mode: Literal['auto','off'] = Field('auto',description='off 时本轮不读取/创建个人记忆，也不触发提取')
    attachment_ids: List[str] = Field(default_factory=list, max_length=8, description="附件 ID 列表（图片 ≤6 张 + 视频 ≤1 个）")

    @model_validator(mode="after")
    def _require_message_or_attachment(self):
        """空消息 + 无附件 → 拒绝（替代原 min_length=1 的兜底）"""
        if not self.message.strip() and not self.attachment_ids:
            raise ValueError("message 为空时必须附带 attachment_ids")
        return self


class RAGRequest(BaseModel):
    """RAG 查询请求"""
    query: str = Field(..., min_length=1, description="查询内容")
    top_k: int = Field(3, ge=1, le=10, description="请求的证据数量，服务端最多返回 5 条")
    use_hyde: bool = Field(False, description="是否额外检索 HyDE 假设回答")


class ApprovalDecision(BaseModel):
    decision: Literal['approve', 'reject']
    preview_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    model_config = {'extra': 'forbid'}


class AttachmentMeta(BaseModel):
    """聊天附件元数据（消息回显，对应 chat_messages.attachments_json）

    v1.6：file_type 增加 "ppt"——PPT 下载信息随 assistant 消息持久化，
    历史回放从消息附件恢复下载卡片（设计方案 §7）。
    v1.7：file_type 增加 "tts"——TTS 语音（text_to_speech）随消息持久化，
    历史回放恢复音频播放/下载卡片（外部 API 工具接入文档 §10 C3）。
    """
    file_id: str
    file_type: Literal["image", "video", "ppt", "tts"]
    original_name: str
    file_size: Optional[int] = None
    mime_type: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration_sec: Optional[float] = None
    # PPT 工具文件（file_type="ppt" 时）
    download_url: Optional[str] = None
    title: Optional[str] = None
    slide_count: Optional[int] = None
    # TTS 音频（file_type="tts" 时）
    duration_estimate: Optional[str] = None


class UploadResponse(BaseModel):
    """聊天附件上传响应"""
    file_id: str
    file_type: Literal["image", "video"]
    mime_type: str
    original_name: str
    file_size: int
    width: Optional[int] = None
    height: Optional[int] = None
    duration_sec: Optional[float] = None
    created_at: datetime

    @field_serializer("created_at")
    def _ser_created_at(self, dt: datetime) -> str:
        return to_utc_iso(dt)


class ChatMessageResponse(BaseModel):
    """聊天消息响应"""
    id: int
    session_id: str
    role: str
    content: str
    token_count: Optional[int] = 0
    created_at: datetime
    attachments: Optional[List[AttachmentMeta]] = None
    citation_ids: list[str] = Field(default_factory=list)

    model_config = {"from_attributes": True}

    # 统一输出带 UTC 偏移的 ISO（数据库存 UTC naive，前端按本地时区展示）
    @field_serializer("created_at")
    def _ser_created_at(self, dt: datetime) -> str:
        return to_utc_iso(dt)


class ChatSessionResponse(BaseModel):
    """聊天会话响应"""
    id: str
    title: str
    metadata: Optional[dict] = None
    created_at: datetime
    updated_at: datetime
    message_count: int = 0

    model_config = {"from_attributes": True}

    # 统一输出带 UTC 偏移的 ISO（前端按本地时区展示北京时间）
    @field_serializer("created_at", "updated_at")
    def _ser_time(self, dt: datetime) -> str:
        return to_utc_iso(dt)


class ChatSessionListResponse(BaseModel):
    """会话列表响应"""
    sessions: List[ChatSessionResponse]


class MessageListResponse(BaseModel):
    """消息列表响应（游标分页）"""
    messages: List[ChatMessageResponse]
    has_more: bool = Field(..., description="是否还有更多消息")
    next_cursor: Optional[str] = Field(None, description="下一页游标（created_at 值）")


class SessionTitleUpdate(BaseModel):
    """会话标题修改请求"""
    title: str = Field(..., min_length=1, max_length=200, description="新标题")


# ============================================================
# Plan-and-Execute SSE 事件类型定义
# ============================================================

class PlanStartEvent(TypedDict):
    """计划开始事件"""
    type: Literal["plan_start"]
    goal: str
    total_steps: int


class PlanStepEvent(TypedDict):
    """单个步骤声明事件"""
    type: Literal["plan_step"]
    step: int
    action: str
    status: Literal["pending", "running", "completed"]


class PlanStepStartEvent(TypedDict):
    """步骤开始执行事件"""
    type: Literal["plan_step_start"]
    step: int
    action: str


class PlanStepEndEvent(TypedDict):
    """步骤执行完成事件"""
    type: Literal["plan_step_end"]
    step: int
    result: str


class PlanSynthesizeEvent(TypedDict):
    """进入综合阶段事件"""
    type: Literal["plan_synthesize"]
    content: str


class PlanCompleteEvent(TypedDict):
    """计划全部完成事件"""
    type: Literal["plan_complete"]
    total_steps: int
    completed_steps: int


class PlanFallbackEvent(TypedDict):
    """Plan 失败降级事件"""
    type: Literal["plan_fallback"]
    reason: str
