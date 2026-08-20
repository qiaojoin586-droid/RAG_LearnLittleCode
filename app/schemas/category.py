"""
分类相关 Pydantic Schema

定义分类树的请求和响应模型（见 docs/笔记分类管理功能设计方案.md v4 §3.2）。
"""

from datetime import datetime
from typing import Optional, List, Literal

from pydantic import BaseModel, Field, model_validator


class CategoryCreate(BaseModel):
    """创建分类请求"""
    name: str = Field(..., min_length=1, max_length=100, description="分类名称")
    parent_id: Optional[str] = Field(None, description="父分类 ID，None 表示顶级")
    icon: Optional[str] = Field(None, max_length=50, description="分类图标")
    color: Optional[str] = Field(None, max_length=20, description="分类颜色")


class CategoryUpdate(BaseModel):
    """更新分类请求

    注意：parent_id 不在本 schema —— 改父级走 /category/{id}/move（CategoryMove）；
    sort_order 不在本 schema —— 排序走 /category/reorder（CategoryReorder）。
    """
    name: Optional[str] = Field(None, min_length=1, max_length=100, description="分类名称")
    icon: Optional[str] = Field(None, max_length=50, description="分类图标")
    color: Optional[str] = Field(None, max_length=20, description="分类颜色")


class CategoryMove(BaseModel):
    """移动分类请求（改父级）"""
    parent_id: Optional[str] = Field(None, description="新父级分类 ID，NULL = 移到顶级")


class CategoryReorder(BaseModel):
    """批量重排同级分类顺序请求"""
    parent_id: Optional[str] = Field(None, description="父级分类 ID（NULL = 顶级）")
    ordered_ids: List[str] = Field(..., min_length=1, description="该父级下所有分类 ID（按新顺序）")


class CategoryBatchOperation(BaseModel):
    """分类批量操作请求"""
    category_ids: List[str] = Field(..., min_length=1, description="分类 ID 列表")
    operation: Literal['delete', 'merge'] = Field(..., description="操作类型")
    merge_target_id: Optional[str] = Field(None, description="合并目标分类 ID（merge 时必填）")

    @model_validator(mode='after')
    def validate_merge(self):
        """merge 操作必须指定 merge_target_id，且目标不能是待操作分类自身"""
        if self.operation == 'merge' and not self.merge_target_id:
            raise ValueError("merge 操作必须指定 merge_target_id")
        if self.operation == 'merge' and self.merge_target_id in self.category_ids:
            raise ValueError("合并目标不能是待操作分类自身")
        return self


class CategoryResponse(BaseModel):
    """分类响应（树形节点）"""
    id: str
    # user_id 不暴露给前端 —— 所有分类均属于当前用户，前端无需感知
    parent_id: Optional[str]
    name: str
    icon: Optional[str]
    color: Optional[str]
    sort_order: int
    created_at: datetime
    updated_at: datetime
    children: List["CategoryResponse"] = []
    note_count: int = 0  # 含子孙分类下的笔记总数，排除软删除笔记


class CategoryTreeResponse(BaseModel):
    """分类树响应"""
    categories: List[CategoryResponse]
    uncategorized_count: int = 0  # category_id IS NULL 的笔记数（排除软删除）


class DeletedCategoryResponse(BaseModel):
    """回收站分类响应（附带剩余清理天数）"""
    id: str
    name: str
    icon: Optional[str] = None
    color: Optional[str] = None
    deleted_at: datetime
    days_remaining: int = Field(..., description="距离自动彻底删除的剩余天数")
    descendant_count: int = Field(0, description="一并软删除的子孙分类数量")


class DeletedCategoryListResponse(BaseModel):
    """回收站分类列表响应"""
    categories: List[DeletedCategoryResponse]
    total: int
