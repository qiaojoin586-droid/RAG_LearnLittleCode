"""
笔记相关 Pydantic Schema

定义笔记 CRUD、语义搜索、自动标签、写作辅助等接口的请求和响应模型。
"""

from datetime import datetime
from typing import Optional, List, Literal

from pydantic import BaseModel, Field, model_validator


class NoteCreate(BaseModel):
    """创建笔记请求"""
    title: str = Field(..., min_length=1, max_length=500, description="笔记标题")
    content: str = Field(..., min_length=1, description="笔记内容（Markdown）")
    tags: Optional[List[str]] = Field(None, description="标签列表")
    category: Optional[str] = Field(None, max_length=100, description="分类")
    category_id: Optional[str] = Field(None, max_length=36, description="分类 ID（双写期间与 category 并行，优先使用）")
    is_pinned: bool = Field(False, description="是否置顶")
    # 文档类型：md = Markdown（默认）/ txt = 纯文本；仅新建时写入
    format: Optional[str] = Field(
        'md', pattern='^(md|txt)$', description="文档类型"
    )


class NoteUpdate(BaseModel):
    """更新笔记请求"""
    title: Optional[str] = Field(None, min_length=1, max_length=500, description="笔记标题")
    content: Optional[str] = Field(None, min_length=1, description="笔记内容")
    tags: Optional[List[str]] = Field(None, description="标签列表")
    category: Optional[str] = Field(None, max_length=100, description="分类")
    category_id: Optional[str] = Field(None, max_length=36, description="分类 ID（显式传 None 表示移为未分类）")
    is_pinned: Optional[bool] = Field(None, description="是否置顶")


class NoteResponse(BaseModel):
    """笔记响应"""
    id: str
    user_id: str
    title: str
    content: str
    tags: Optional[List[str]] = None
    category: Optional[str] = None
    category_id: Optional[str] = None  # 分类 ID（双写期间与 category 并行返回）
    is_pinned: bool
    format: str = 'md'  # 文档类型（md/txt；旧数据兼容默认 md）
    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime] = None   # 删除时间（回收站展示用，正常笔记为 None）

    model_config = {"from_attributes": True}


class DeletedNoteResponse(NoteResponse):
    """回收站笔记响应（继承 NoteResponse，附带剩余清理天数）"""
    days_remaining: int = Field(..., description="距离自动彻底删除的剩余天数")


class DeletedNoteListResponse(BaseModel):
    """回收站列表响应（分页）"""
    notes: List[DeletedNoteResponse]
    total: int
    page: int
    page_size: int


class NoteListResponse(BaseModel):
    """笔记列表响应（分页）"""
    notes: List[NoteResponse]
    total: int
    page: int
    page_size: int


class NoteSearchRequest(BaseModel):
    """语义搜索请求"""
    query: str = Field(..., min_length=1, description="搜索关键词")
    top_k: int = Field(5, ge=1, le=20, description="返回结果数量")


class NoteSearchResult(BaseModel):
    """语义搜索结果项"""
    note: NoteResponse
    score: float = Field(..., description="相似度分数")


class NoteSearchResponse(BaseModel):
    """语义搜索响应"""
    results: List[NoteSearchResult]
    query: str


class AutocompleteRequest(BaseModel):
    """AI 内联补全请求"""
    content: str = Field(..., description="当前笔记内容")
    cursor_position: int = Field(0, ge=0, description="光标位置")


class WriteAssistantRequest(BaseModel):
    """AI 写作辅助请求"""
    content: str = Field(..., description="笔记内容")
    mode: str = Field("continue", description="模式：continue(续写) / expand(扩写) / summary(摘要)")


class BatchOperation(BaseModel):
    """批量操作请求"""
    note_ids: List[str] = Field(..., min_length=1, description="笔记 ID 列表")
    operation: Literal['delete', 'pin', 'unpin', 'move', 'permanent_delete', 'restore'] = Field(
        ..., description="操作类型"
    )
    target_category: Optional[str] = Field(None, description="目标分类字符串（move 操作时使用，旧逻辑向后兼容）")
    target_category_id: Optional[str] = Field(None, description="目标分类 ID（move 操作时使用，优先）")

    @model_validator(mode='after')
    def validate_move_category(self):
        """move 操作必须提供 target_category 或 target_category_id"""
        if self.operation == 'move' and not self.target_category and not self.target_category_id:
            raise ValueError("move 操作必须提供 target_category 或 target_category_id")
        return self

    @model_validator(mode='after')
    def validate_operation_limits(self):
        """批量操作安全校验"""
        # ① 批量彻底删除上限 50 条（防止误操作大规模删除）
        if self.operation == 'permanent_delete' and len(self.note_ids) > 50:
            raise ValueError("单次批量彻底删除最多 50 条笔记")
        # ② 批量恢复上限 100 条
        if self.operation == 'restore' and len(self.note_ids) > 100:
            raise ValueError("单次批量恢复最多 100 条笔记")
        return self
